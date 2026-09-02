"""Tests for plantor.

Run: python3 -m unittest discover -s tests -v

These are written against the hook contract observed in Plannotator's working
production code (decision.behavior / decision.message), NOT the shape currently
documented at code.claude.com/docs/en/hooks. See the plan's r3 revision note.
"""

import http.client
import io
import json
import os
import re
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import plantor  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
UI_PATH = os.path.join(REPO_ROOT, "ui", "index.html")


def read_ui():
    with open(UI_PATH, encoding="utf-8") as handle:
        return handle.read()


def read_src():
    with open(os.path.join(REPO_ROOT, "plantor.py"), encoding="utf-8") as handle:
        return handle.read()


def hook_payload(plan="# Test plan\n\n- step one\n"):
    """A realistic PermissionRequest payload for ExitPlanMode."""
    return {
        "session_id": "abc123",
        "transcript_path": "/Users/x/.claude/projects/p/t.jsonl",
        "cwd": "/Users/x/projects/p",
        "permission_mode": "plan",
        "hook_event_name": "PermissionRequest",
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": plan},
        "tool_use_id": "toolu_01ABC",
    }


# --------------------------------------------------------------------------
# Hook input parsing
# --------------------------------------------------------------------------


class TestReadHookInput(unittest.TestCase):
    def test_parses_valid_payload(self):
        payload = hook_payload()
        got = plantor.read_hook_input(io.StringIO(json.dumps(payload)))
        self.assertEqual(got["tool_name"], "ExitPlanMode")

    def test_malformed_json_returns_empty_dict(self):
        self.assertEqual(plantor.read_hook_input(io.StringIO("{not json")), {})

    def test_empty_stdin_returns_empty_dict(self):
        self.assertEqual(plantor.read_hook_input(io.StringIO("")), {})

    def test_deeply_nested_json_returns_empty_dict(self):
        """RecursionError is a RuntimeError, so `except ValueError` misses it."""
        payload = "[" * 3000 + "]" * 3000
        self.assertEqual(plantor.read_hook_input(io.StringIO(payload)), {})

    def test_json_scalar_returns_empty_dict(self):
        self.assertEqual(plantor.read_hook_input(io.StringIO("42")), {})


class TestExtractPlan(unittest.TestCase):
    def test_extracts_inline_plan(self):
        self.assertEqual(
            plantor.extract_plan(hook_payload("# Hello\n")), "# Hello\n"
        )

    def test_missing_tool_input_returns_empty(self):
        self.assertEqual(plantor.extract_plan({}), "")

    def test_missing_plan_key_returns_empty(self):
        self.assertEqual(plantor.extract_plan({"tool_input": {}}), "")

    def test_non_string_plan_returns_empty(self):
        self.assertEqual(plantor.extract_plan({"tool_input": {"plan": 42}}), "")


# --------------------------------------------------------------------------
# Feedback formatting
# --------------------------------------------------------------------------


class TestPlanSlug(unittest.TestCase):
    """The review URL is otherwise identified only by a port number, which
    makes two open reviews indistinguishable in the browser."""

    def test_slug_comes_from_the_title(self):
        self.assertEqual(
            plantor.plan_slug("# Add rate limiting to the ingest API\n\nbody"),
            "add-rate-limiting-to-the-ingest-api")

    def test_slug_accepts_a_lower_heading(self):
        self.assertEqual(plantor.plan_slug("## Only an h2\n"), "only-an-h2")

    def test_slug_strips_punctuation_and_collapses_gaps(self):
        self.assertEqual(plantor.plan_slug("# Weird: chars!! *&^ and    spaces"),
                         "weird-chars-and-spaces")

    def test_slug_falls_back_when_there_is_no_heading(self):
        self.assertEqual(plantor.plan_slug("no heading at all"), "plan")
        self.assertEqual(plantor.plan_slug(""), "plan")
        self.assertEqual(plantor.plan_slug(None), "plan")

    def test_slug_is_bounded_and_url_safe(self):
        slug = plantor.plan_slug("# " + ("word " * 80))
        self.assertLessEqual(len(slug), 64)
        self.assertRegex(slug, r"^[a-z0-9-]+$")
        self.assertFalse(slug.endswith("-"))

    def test_slug_of_a_non_ascii_title_still_yields_something_usable(self):
        self.assertEqual(plantor.plan_slug("# 计划 rollout"), "rollout")
        self.assertEqual(plantor.plan_slug("# 计划"), "plan")

    def test_title_is_the_first_heading(self):
        self.assertEqual(plantor.plan_title("# A Plan\n\n## Later\n"), "A Plan")
        self.assertEqual(plantor.plan_title("no heading"), "")

    def test_data_carries_the_title(self):
        self.assertEqual(plantor.Review("# A Plan\n").data()["title"], "A Plan")


class TestPreviousPlan(unittest.TestCase):
    """Recovering the prior revision from Claude Code's own transcript.

    Deliberately read from the transcript rather than a store of our own:
    plantor persists nothing, and a diff should not be the reason that changes.
    """

    def write_transcript(self, plans):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as handle:
            for plan in plans:
                handle.write(json.dumps({
                    "type": "assistant",
                    "message": {"content": [
                        {"type": "tool_use", "name": "ExitPlanMode",
                         "input": {"plan": plan}}]},
                }) + "\n")
        return path

    def test_returns_the_previous_plan(self):
        path = self.write_transcript(["# v1\n", "# v2\n"])
        self.assertEqual(plantor.previous_plan(path, "# v3\n"), "# v2\n")

    def test_skips_a_plan_identical_to_the_current_one(self):
        """The current call may already be in the transcript when we read it."""
        path = self.write_transcript(["# v1\n", "# v2\n"])
        self.assertEqual(plantor.previous_plan(path, "# v2\n"), "# v1\n")

    def test_no_earlier_plan_returns_empty(self):
        path = self.write_transcript(["# only\n"])
        self.assertEqual(plantor.previous_plan(path, "# only\n"), "")

    def test_missing_transcript_returns_empty(self):
        self.assertEqual(plantor.previous_plan("/nonexistent/x.jsonl", "# p"), "")

    def test_none_path_returns_empty(self):
        self.assertEqual(plantor.previous_plan(None, "# p"), "")

    def test_malformed_lines_are_skipped(self):
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        self.addCleanup(os.unlink, path)
        with os.fdopen(fd, "w") as handle:
            handle.write("not json ExitPlanMode\n")
            handle.write(json.dumps({"message": "ExitPlanMode not a dict"}) + "\n")
            handle.write(json.dumps({"message": {"content": "ExitPlanMode"}}) + "\n")
            handle.write(json.dumps({
                "message": {"content": [
                    {"type": "tool_use", "name": "ExitPlanMode",
                     "input": {"plan": "# good\n"}}]}}) + "\n")
        self.assertEqual(plantor.previous_plan(path, "# current"), "# good\n")

    def test_data_carries_the_previous_plan(self):
        review = plantor.Review("# new", previous="# old")
        self.assertEqual(review.data()["previous"], "# old")


class TestFormatFeedback(unittest.TestCase):
    def test_includes_directive_framing(self):
        out = plantor.format_feedback(
            [{"quote": "Use Redis", "body": "why not Postgres?"}], ""
        )
        self.assertIn("YOUR PLAN WAS NOT APPROVED", out)
        self.assertIn("MUST", out)
        self.assertIn("Do not resubmit the plan unchanged", out)

    def test_includes_every_comment_and_its_quote(self):
        comments = [
            {"quote": "Use Redis", "body": "why not Postgres?"},
            {"quote": "Ship Friday", "body": "too aggressive"},
        ]
        out = plantor.format_feedback(comments, "")
        for c in comments:
            self.assertIn(c["quote"], out)
            self.assertIn(c["body"], out)

    def test_comments_are_numbered_to_match_ui_markers(self):
        out = plantor.format_feedback(
            [{"quote": "a", "body": "first"}, {"quote": "b", "body": "second"}], ""
        )
        self.assertRegex(out, r"1\.")
        self.assertRegex(out, r"2\.")

    def test_includes_overall_notes(self):
        out = plantor.format_feedback([], "Prefer fewer files overall.")
        self.assertIn("Prefer fewer files overall.", out)

    def test_notes_only_is_valid(self):
        out = plantor.format_feedback([], "just this")
        self.assertIn("just this", out)
        self.assertIn("YOUR PLAN WAS NOT APPROVED", out)

    def test_long_quotes_are_truncated(self):
        out = plantor.format_feedback(
            [{"quote": "x" * 500, "body": "b"}], ""
        )
        self.assertIn("…", out)
        self.assertNotIn("x" * (plantor.ANCHOR_CHARS + 1), out)

    def test_anchor_prefers_section_and_short_excerpt(self):
        """The plan is already in Claude's context; re-quoting it wastes tokens."""
        out = plantor.format_feedback(
            [{"section": "Approach",
              "anchor": "Use a token bucket per API key",
              "body": "Why Redis?"}], "")
        self.assertIn("[Approach]", out)
        self.assertIn("Use a token bucket per API key", out)
        self.assertIn("Why Redis?", out)

    def test_anchor_falls_back_to_quote(self):
        out = plantor.format_feedback([{"quote": "some block", "body": "b"}], "")
        self.assertIn("some block", out)

    def test_heading_anchor_is_not_repeated(self):
        """Commenting on a heading makes section == anchor; say it once."""
        out = plantor.format_feedback(
            [{"section": "Context", "anchor": "Context", "body": "b"}], "")
        self.assertIn("[Context]", out)
        self.assertNotIn('[Context] "Context"', out)

    def test_truncated_heading_anchor_is_not_repeated(self):
        out = plantor.format_feedback(
            [{"section": "Context", "anchor": "Context\u2026", "body": "b"}], "")
        self.assertNotIn('"Context', out)

    def test_section_only_anchor_is_valid(self):
        out = plantor.format_feedback([{"section": "Verification", "body": "b"}], "")
        self.assertIn("[Verification]", out)

    def test_feedback_stays_compact(self):
        """Guard against the format quietly regrowing.

        The original quote-the-whole-block format produced ~1200 characters for
        this input. Every rejection pays this cost, and rejections repeat.
        """
        comments = [
            {"section": "Approach", "anchor": "Use a token bucket per API key",
             "body": "Redis becomes a hard dependency of the ingest path."},
            {"section": "Approach", "anchor": "Bucket size: 200 requests",
             "body": "Justify 200 against observed peak traffic."},
            {"section": "Implementation", "anchor": "Write the bucket in Lua",
             "body": "Land it behind a feature flag."},
        ]
        out = plantor.format_feedback(comments, "Split into two changes.")
        self.assertLess(len(out), 700, "feedback format has regrown:\n" + out)


# --------------------------------------------------------------------------
# Decision building  (the r3-corrected contract)
# --------------------------------------------------------------------------


class TestMalformedSubmissions(unittest.TestCase):
    """A bad submission shape must never crash after a human has reviewed.

    Anything holding the token can post to /submit. If a bad shape reached the
    formatter it would raise *after* serve_review returned -- past the guard --
    discarding a completed review and falling back to the built-in dialog with
    no sign a human had decided anything.
    """

    def review(self, comments, notes=""):
        r = plantor.Review("# p\n", timeout=1)
        self.addCleanup(r.stop)
        r.claim({"verdict": "reject", "comments": comments, "notes": notes})
        return r.result

    def test_comments_as_string_is_dropped(self):
        self.assertEqual(self.review("a string")["comments"], [])

    def test_comments_as_dict_is_dropped(self):
        self.assertEqual(self.review({"not": "a list"})["comments"], [])

    def test_non_dict_items_are_filtered_out(self):
        got = self.review(["a string", None, 7, {"body": "keep me"}])
        self.assertEqual(len(got["comments"]), 1)
        self.assertEqual(got["comments"][0]["body"], "keep me")

    def test_notes_must_be_a_string(self):
        self.assertEqual(self.review([], {"not": "a string"})["notes"], "")

    def test_formatting_survives_every_bad_shape(self):
        for bad in ("a string", {"not": "a list"}, ["x"], [None], 7, None):
            got = self.review(bad)
            # Must not raise:
            plantor.format_feedback(got["comments"], got["notes"])
            plantor.build_decision(got, {"plan": "# p"})


class TestShellAndData(unittest.TestCase):
    """The page is a shell; the plan arrives over an authenticated fetch.

    Injecting the plan into the HTML is what forced the document request to
    carry the token, which is what broke reload. It also put plan text one
    escaping bug away from being markup; now it never touches the document.
    """

    def test_the_shell_is_the_ui_file_verbatim(self):
        review = plantor.Review("# p\n")
        self.assertEqual(review.shell, read_ui())

    def test_the_shell_holds_no_plan_text(self):
        review = plantor.Review("# a very distinctive plan heading\n")
        self.assertNotIn("distinctive plan heading", review.shell)

    def test_no_data_block_is_left_in_the_page(self):
        self.assertNotIn('id="plantor-data"', read_ui(),
                         "the page still embeds a server-injected data block")


class TestBuildDecision(unittest.TestCase):
    def setUp(self):
        self.tool_input = {"plan": "# Plan\n"}

    def test_approve_uses_behavior_allow(self):
        d = plantor.build_decision(
            {"verdict": "approve", "comments": [], "notes": ""}, self.tool_input
        )
        self.assertEqual(d["hookSpecificOutput"]["hookEventName"], "PermissionRequest")
        self.assertEqual(d["hookSpecificOutput"]["decision"]["behavior"], "allow")

    def test_approve_must_echo_updated_input(self):
        """Claude Code >= 2.1.199 silently drops an allow without updatedInput."""
        d = plantor.build_decision(
            {"verdict": "approve", "comments": [], "notes": ""}, self.tool_input
        )
        decision = d["hookSpecificOutput"]["decision"]
        self.assertIn("updatedInput", decision)
        self.assertEqual(decision["updatedInput"]["plan"], "# Plan\n")

    def test_approve_echoes_tool_input_verbatim(self):
        """Match Plannotator: updatedInput is the original tool_input, never rewritten.

        An allow decision has no message channel back to Claude, so notes cannot
        ride along on approval. Plannotator hit the same wall and chose not to
        route around it by mutating the plan; neither do we.
        """
        d = plantor.build_decision(
            {"verdict": "approve", "comments": [], "notes": "watch the timeouts"},
            self.tool_input,
        )
        decision = d["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "allow")
        self.assertEqual(decision["updatedInput"], self.tool_input)
        self.assertEqual(decision["updatedInput"]["plan"], "# Plan\n")

    def test_approve_never_mutates_the_plan(self):
        original = {"plan": "# Plan\n", "extra": "preserved"}
        d = plantor.build_decision(
            {"verdict": "approve", "comments": [{"quote": "q", "body": "b"}],
             "notes": "some notes"},
            original,
        )
        self.assertEqual(d["hookSpecificOutput"]["decision"]["updatedInput"], original)

    def test_reject_uses_behavior_deny_with_message(self):
        d = plantor.build_decision(
            {
                "verdict": "reject",
                "comments": [{"quote": "Use Redis", "body": "why not Postgres?"}],
                "notes": "too big",
            },
            self.tool_input,
        )
        decision = d["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        self.assertIn("why not Postgres?", decision["message"])
        self.assertIn("too big", decision["message"])

    def test_no_result_means_no_output(self):
        """Timeout / closed window / error -> print nothing, normal prompt happens."""
        self.assertIsNone(plantor.build_decision(None, self.tool_input))

    def test_unknown_verdict_means_no_output(self):
        self.assertIsNone(
            plantor.build_decision(
                {"verdict": "banana", "comments": [], "notes": ""}, self.tool_input
            )
        )


# --------------------------------------------------------------------------
# Egress and loopback guards
# --------------------------------------------------------------------------


class TestNoEgress(unittest.TestCase):
    def test_ui_has_no_external_references(self):
        html = read_ui()
        # Strip the CSP meta line, which legitimately names no host but may
        # contain scheme-like tokens.
        without_csp = re.sub(r"<meta[^>]*Content-Security-Policy[^>]*>", "", html, flags=re.I)
        offenders = re.findall(r"https?://[^\s\"'<>]+", without_csp)
        self.assertEqual(offenders, [], "external references found: %s" % offenders)

    def test_ui_has_no_unbalanced_html_comments(self):
        """A truncated <!-- leaks its tail onto the page as visible text.

        This actually happened: a rebuild lost an opening marker and the page
        rendered "references apart from the export at the bottom. -->".
        """
        html = read_ui()

        # Every opener must find a closer. Counting both markers instead would
        # be wrong now that a JS comment can legitimately mention a mermaid
        # arrow (`A --> B`), which is not an HTML comment closer at all.
        pos = 0
        while True:
            start = html.find("<!--", pos)
            if start < 0:
                break
            end = html.find("-->", start + 4)
            self.assertGreater(end, 0,
                               "unclosed <!-- at offset %d in ui/index.html" % start)
            pos = end + 3

        # A line beginning with --> is a single-line comment to a JS parser, so
        # one at the start of a line inside a script silently eats that line.
        for n, line in enumerate(html.split("\n"), 1):
            self.assertFalse(line.lstrip().startswith("-->"),
                             "line %d starts with --> in ui/index.html" % n)

    def test_ui_declares_restrictive_csp(self):
        html = read_ui()
        self.assertIn("Content-Security-Policy", html)
        self.assertIn("default-src 'none'", html)
        self.assertIn("connect-src 'self'", html)
        self.assertIn("frame-ancestors 'none'", html)

    def test_ui_loads_no_external_fonts_or_images(self):
        html = read_ui()
        csp = re.search(r"content=\"([^\"]*default-src[^\"]*)\"", html).group(1)
        self.assertNotIn("font-src", csp)
        self.assertNotIn("img-src", csp)

    def test_source_imports_no_http_client(self):
        src = read_src()
        for banned in ("urllib.request", "requests", "http.client", "urlopen"):
            self.assertNotIn(banned, src, "%s must not appear in plantor.py" % banned)

    def test_layout_breakpoint_is_not_duplicated_in_js(self):
        """The JS must ask the DOM whether the rail is visible, not restate the
        breakpoint. A hardcoded matchMedia width silently desyncs when the CSS
        changes, and the failure mode is comments rendered into a display:none
        container: invisible, with no error. This has happened twice."""
        html = read_ui()
        app = html.split('<script id="plantor-md">')[1].split("</script>", 1)[1]
        self.assertNotIn("matchMedia(", app,
                         "app JS restates a breakpoint instead of reading the DOM")
        self.assertNotIn("min-width", app,
                         "app JS hardcodes a breakpoint width")
        self.assertIn("railEl.offsetParent", app)

    def test_content_width_is_single_sourced(self):
        """Header, action bar and document must align to one width.

        They were each given the same literal, and the header was full-bleed on
        top of that -- so on a 34" display the view switcher sat two thousand
        pixels from the plan it acted on. One custom property keeps them
        together, including when the split view widens it.
        """
        css = read_ui().split("</style>")[0]
        self.assertIn("--content:", css, "the content width token is gone")

        # The token may be defined (and redefined for the split view), but no
        # rule may restate its value as a literal.
        defs = re.findall(r"--content:\s*([0-9]+)px", css)
        self.assertTrue(defs, "no --content value found")
        for value in set(defs):
            uses = re.findall(r"(?<!-)\b(?:max-)?width:\s*%spx" % value, css)
            self.assertEqual(uses, [],
                             "%spx is restated as a literal instead of using "
                             "var(--content)" % value)

        self.assertIn("var(--content)", css)

    def test_binds_loopback_only(self):
        src = read_src()
        self.assertIn("127.0.0.1", src)
        self.assertNotIn("0.0.0.0", src)


# --------------------------------------------------------------------------
# Live server: security controls + end-to-end
# --------------------------------------------------------------------------


class _ReviewServer(object):
    """Boots a real review server on a random loopback port for each test.

    A mixin rather than a base test case: subclassing a TestCase re-runs every
    one of its tests against the subclass, which doubled the suite's runtime
    for no extra coverage.
    """

    plan = "# Live plan\n\n- do a thing\n"   # slug: live-plan

    def setUp(self):
        self.review = plantor.Review(self.plan, timeout=10)
        self.review.start()
        self.addCleanup(self.review.stop)
        self.port = self.review.port
        self.token = self.review.token
        self.host = "127.0.0.1:%d" % self.port

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        h.setdefault("Host", host or self.host)
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def submit(self, payload, headers=None):
        h = {"Content-Type": "application/json", "X-Plantor-Token": self.token}
        h.update(headers or {})
        return self.request("POST", "/submit", json.dumps(payload), h)


class LiveServerTest(_ReviewServer, unittest.TestCase):

    # -- routing ---------------------------------------------------------

    def test_get_root_with_token_serves_ui(self):
        status, headers, body = self.request("GET", "/?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_named_path_serves_the_ui(self):
        status, _, body = self.request("GET", self.review.path + "?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_url_carries_the_plan_name(self):
        self.assertIn("/live-plan", self.review.url)

    def test_root_still_serves_the_ui(self):
        status, _, _ = self.request("GET", "/?t=" + self.token)
        self.assertEqual(status, 200)

    def test_a_different_name_is_404(self):
        status, _, _ = self.request("GET", "/some-other-plan?t=" + self.token)
        self.assertEqual(status, 404)

    def test_unknown_path_is_404(self):
        status, _, _ = self.request("GET", "/../../etc/passwd?t=" + self.token)
        self.assertEqual(status, 404)

    def test_no_static_file_route_exists(self):
        status, _, _ = self.request("GET", "/ui/index.html?t=" + self.token)
        self.assertEqual(status, 404)

    # -- token -----------------------------------------------------------

    def test_missing_token_is_403(self):
        """The token gates the plan, not the document. The document holds no
        plan text, and has to be fetchable without a token or reload dies."""
        status, _, _ = self.request("GET", "/data")
        self.assertEqual(status, 403)

    def test_wrong_token_is_403(self):
        status, _, _ = self.request(
            "GET", "/data", headers={"X-Plantor-Token": "wrongtoken"})
        self.assertEqual(status, 403)

    def test_submit_without_token_is_403(self):
        status, _, _ = self.submit({"verdict": "approve"}, {"X-Plantor-Token": "nope"})
        self.assertEqual(status, 403)

    # -- DNS rebinding / CSRF --------------------------------------------

    def test_foreign_host_header_is_rejected(self):
        """DNS rebinding: attacker's hostname resolving to 127.0.0.1."""
        status, _, _ = self.request(
            "GET", "/?t=" + self.token, host="evil.example.com:%d" % self.port
        )
        self.assertEqual(status, 403)

    def test_localhost_hostname_is_rejected(self):
        status, _, _ = self.request(
            "GET", "/?t=" + self.token, host="localhost:%d" % self.port
        )
        self.assertEqual(status, 403)

    def test_cross_origin_post_is_rejected(self):
        status, _, _ = self.submit(
            {"verdict": "approve"}, {"Origin": "https://evil.example.com"}
        )
        self.assertEqual(status, 403)

    def test_same_origin_post_is_accepted(self):
        status, _, _ = self.submit(
            {"verdict": "approve", "comments": [], "notes": ""},
            {"Origin": "http://" + self.host},
        )
        self.assertEqual(status, 200)

    # -- headers ---------------------------------------------------------

    def test_security_headers_present(self):
        _, headers, _ = self.request("GET", "/?t=" + self.token)
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    # -- limits ----------------------------------------------------------

    def test_oversized_body_is_rejected(self):
        """An oversized submission must be refused and have no effect.

        The server answers 413 and closes without draining the body -- reading
        it would be the very resource exhaustion the cap prevents -- so the
        client may see the close as a broken pipe instead of a response. Either
        outcome is a rejection; what must hold is that nothing was recorded.
        """
        payload = {"verdict": "reject", "comments": [],
                   "notes": "x" * (2 * 1024 * 1024)}
        try:
            status, _, _ = self.submit(payload)
            self.assertEqual(status, 413)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.assertFalse(self.review.done.is_set())
        self.assertIsNone(self.review.result)

    def test_missing_content_length_is_rejected(self):
        status, _, _ = self.request(
            "POST", "/submit", None,
            {"X-Plantor-Token": self.token, "Content-Length": "0"},
        )
        self.assertEqual(status, 411)

    def test_claim_without_commit_still_blocks_a_second_submit(self):
        """The response is written before wait() is released, so the window
        between claim and commit must already be closed to further posts."""
        self.assertTrue(self.review.claim({"verdict": "approve"}))
        self.assertTrue(self.review.taken)
        self.assertFalse(self.review.done.is_set())
        status, _, _ = self.submit({"verdict": "approve", "comments": [], "notes": ""})
        self.assertEqual(status, 410)

    def test_commit_releases_wait(self):
        self.review.claim({"verdict": "approve", "comments": [], "notes": ""})
        self.assertFalse(self.review.done.is_set())
        self.review.commit()
        self.assertTrue(self.review.done.is_set())
        self.assertEqual(self.review.wait()["verdict"], "approve")

    def test_second_submit_is_gone(self):
        s1, _, _ = self.submit({"verdict": "approve", "comments": [], "notes": ""})
        self.assertEqual(s1, 200)
        s2, _, _ = self.submit({"verdict": "approve", "comments": [], "notes": ""})
        self.assertEqual(s2, 410)

    # -- plan injection safety -------------------------------------------

    def test_a_script_tag_in_the_plan_never_reaches_the_document(self):
        """Previously the plan was embedded in the HTML and `<` was escaped so
        a literal </script> could not terminate the data block. The plan is no
        longer in the document at all, so assert that -- the stronger claim."""
        hostile = "# x\n\n</script><script>alert(1)</script>\n"
        review = plantor.Review(hostile, timeout=10)
        review.start()
        self.addCleanup(review.stop)
        host = {"Host": "127.0.0.1:%d" % review.port}

        conn = http.client.HTTPConnection("127.0.0.1", review.port, timeout=10)
        conn.request("GET", "/", headers=host)
        document = conn.getresponse().read().decode("utf-8")
        conn.close()
        self.assertNotIn("alert(1)", document)

        # and it must still round-trip verbatim over the data route
        conn = http.client.HTTPConnection("127.0.0.1", review.port, timeout=10)
        headers = dict(host)
        headers["X-Plantor-Token"] = review.token
        conn.request("GET", "/data", headers=headers)
        payload = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        self.assertEqual(payload["plan"], hostile)

    # -- end to end ------------------------------------------------------

    def test_end_to_end_approve(self):
        result = {}

        def wait():
            result["value"] = self.review.wait()

        t = threading.Thread(target=wait)
        t.start()
        self.submit({"verdict": "approve", "comments": [], "notes": ""})
        t.join(timeout=5)
        self.assertEqual(result["value"]["verdict"], "approve")

    def test_end_to_end_reject_carries_comments(self):
        result = {}

        def wait():
            result["value"] = self.review.wait()

        t = threading.Thread(target=wait)
        t.start()
        self.submit(
            {
                "verdict": "reject",
                "comments": [{"quote": "Use Redis", "body": "why not Postgres?"}],
                "notes": "scope too big",
            }
        )
        t.join(timeout=5)
        got = result["value"]
        self.assertEqual(got["verdict"], "reject")
        self.assertEqual(got["comments"][0]["body"], "why not Postgres?")
        self.assertEqual(got["notes"], "scope too big")

    def test_timeout_returns_none(self):
        review = plantor.Review("# x\n", timeout=0.3)
        review.start()
        self.addCleanup(review.stop)
        self.assertIsNone(review.wait())

    def test_default_wait_is_effectively_unbounded(self):
        """Plannotator sets a 4-day hook timeout and never self-cancels a review.

        A short internal deadline would abandon a review while the user is still
        reading it. The ceiling belongs in the hook config, not in this code.
        """
        review = plantor.Review("# x\n")
        self.addCleanup(review.stop)
        self.assertGreaterEqual(review.timeout, 86400)


# --------------------------------------------------------------------------
# stdout discipline
# --------------------------------------------------------------------------


class TestMarkdownJS(unittest.TestCase):
    """Run the node-based markdown tests as part of the one suite.

    The parser lives inline in ui/index.html so the page stays self-contained;
    node is a dev-only dependency and the test skips without it.
    """

    def test_markdown_suite_passes(self):
        import shutil
        import subprocess

        if shutil.which("node") is None:
            self.skipTest("node not installed; markdown tests skipped")
        script = os.path.join(REPO_ROOT, "tests", "test_markdown.js")
        proc = subprocess.run(
            ["node", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT
        )
        self.assertEqual(
            proc.returncode, 0, proc.stdout.decode("utf-8", "replace")
        )


class TestStdoutDiscipline(unittest.TestCase):
    def test_emit_writes_only_json_to_stdout(self):
        buf = io.StringIO()
        plantor.emit({"hookSpecificOutput": {"hookEventName": "PermissionRequest"}}, buf)
        json.loads(buf.getvalue())  # must parse as a single JSON document

    def test_emit_none_writes_nothing(self):
        buf = io.StringIO()
        plantor.emit(None, buf)
        self.assertEqual(buf.getvalue(), "")


# --------------------------------------------------------------------------
# Reloadable URLs: a token-less shell plus an authenticated data fetch
#
# A refresh is a plain document navigation -- no ?t=, no X-Plantor-Token, and
# the page's JS never runs, so no client-side store can rescue it. The document
# therefore has to be servable without a token, which means it must carry no
# plan text. These tests pin both halves of that.
# --------------------------------------------------------------------------


class TestReloadableShell(_ReviewServer, unittest.TestCase):
    plan = "# Live plan\n\n- a distinctive sentinel phrase\n"

    def test_shell_is_served_without_a_token(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_shell_carries_no_plan_text(self):
        _, _, body = self.request("GET", "/")
        self.assertNotIn(b"distinctive sentinel phrase", body,
                         "plan text is embedded in the token-less document")

    def test_reload_of_the_scrubbed_url_still_serves_the_page(self):
        """The exact bug: the page strips ?t= from the URL, so a refresh sends
        no token. That used to be a blank 403 with the token gone from both the
        address bar and the terminal -- an unrecoverable review."""
        status, _, body = self.request("GET", self.review.path)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_data_requires_the_token(self):
        status, _, _ = self.request("GET", "/data")
        self.assertEqual(status, 403)

    def test_data_with_the_token_returns_the_plan(self):
        status, headers, body = self.request(
            "GET", "/data", headers={"X-Plantor-Token": self.token})
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("Content-Type", ""))
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["plan"], self.plan)
        self.assertEqual(payload["mode"], "review")

    def test_data_rejects_a_wrong_token(self):
        status, _, _ = self.request(
            "GET", "/data", headers={"X-Plantor-Token": "nope"})
        self.assertEqual(status, 403)

    def test_data_rejects_a_foreign_host(self):
        status, _, _ = self.request(
            "GET", "/data", headers={"X-Plantor-Token": self.token},
            host="evil.example.com:%d" % self.port)
        self.assertEqual(status, 403)

    def test_data_after_a_verdict_reports_it_rather_than_the_plan(self):
        self.submit({"verdict": "approve", "comments": [], "notes": ""})
        status, _, body = self.request(
            "GET", "/data", headers={"X-Plantor-Token": self.token})
        self.assertEqual(status, 410)
        self.assertEqual(json.loads(body.decode("utf-8"))["verdict"], "approve")


# --------------------------------------------------------------------------
# Finding past plans in Claude Code's own transcripts
# --------------------------------------------------------------------------


class TestUnquote(unittest.TestCase):
    """Percent-decoding is hand-rolled to keep the URL library out of the file.

    A malformed escape must be left alone rather than half-consumed: "a%2" once
    decoded to "a\x02" because the bounds check was off by one.
    """

    def test_decodes_escapes(self):
        self.assertEqual(plantor._unquote("%2Fplan-abc"), "/plan-abc")
        self.assertEqual(plantor._unquote("a%2Fb%2Fc"), "a/b/c")

    def test_leaves_plain_text_alone(self):
        self.assertEqual(plantor._unquote("/plan-abc"), "/plan-abc")
        self.assertEqual(plantor._unquote(""), "")

    def test_a_truncated_escape_is_left_as_written(self):
        for bad in ("a%2", "%", "%2", "%zz", "%g0"):
            self.assertEqual(plantor._unquote(bad), bad)

    def test_a_decoded_traversal_is_still_only_ever_compared(self):
        """Decoding cannot open a path: the result is matched by equality
        against generated values, so the worst case is a 404."""
        self.assertEqual(plantor._unquote("%2E%2E%2Fetc%2Fpasswd"), "../etc/passwd")


class TestScanPlans(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self.config = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.config, True)
        self.cwd = "/Users/x/projects/p"
        self.dir = os.path.join(self.config, "projects", "-Users-x-projects-p")
        os.makedirs(self.dir)

    def write(self, name, records):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as handle:
            for rec in records:
                handle.write(json.dumps(rec) + "\n")
        return path

    def record(self, plan, when, cwd=None, uuid="u1"):
        return {
            "uuid": uuid,
            "timestamp": when,
            "cwd": cwd or self.cwd,
            "message": {"content": [
                {"type": "tool_use", "name": "ExitPlanMode", "input": {"plan": plan}}
            ]},
        }

    def scan(self):
        return plantor.scan_plans(self.cwd, config_dir=self.config)

    def test_transcript_dir_mangles_the_cwd(self):
        self.assertEqual(
            plantor.transcript_dir(self.cwd, config_dir=self.config), self.dir)

    def test_finds_every_plan_newest_first(self):
        self.write("s1.jsonl", [
            self.record("# One\n", "2026-01-01T00:00:00Z", uuid="a"),
            self.record("# Two\n", "2026-01-02T00:00:00Z", uuid="b"),
        ])
        got = self.scan()
        self.assertEqual([p["title"] for p in got], ["Two", "One"])

    def test_spans_multiple_sessions(self):
        self.write("s1.jsonl", [self.record("# One\n", "2026-01-01T00:00:00Z", uuid="a")])
        self.write("s2.jsonl", [self.record("# Two\n", "2026-01-03T00:00:00Z", uuid="b")])
        self.assertEqual([p["title"] for p in self.scan()], ["Two", "One"])

    def test_ignores_records_from_another_project(self):
        """The directory name is derived from the cwd, so a collision or a
        change in Claude Code's mangling must not leak another project's plans."""
        self.write("s1.jsonl", [
            self.record("# Mine\n", "2026-01-01T00:00:00Z", uuid="a"),
            self.record("# Theirs\n", "2026-01-02T00:00:00Z",
                        cwd="/Users/x/projects/other", uuid="b"),
        ])
        self.assertEqual([p["title"] for p in self.scan()], ["Mine"])

    def test_each_plan_gets_a_unique_path_even_with_the_same_title(self):
        self.write("s1.jsonl", [
            self.record("# Same title\n\nfirst\n", "2026-01-01T00:00:00Z", uuid="a"),
            self.record("# Same title\n\nsecond\n", "2026-01-02T00:00:00Z", uuid="b"),
        ])
        paths = [p["path"] for p in self.scan()]
        self.assertEqual(len(set(paths)), 2, "paths collide: %s" % paths)
        for path in paths:
            self.assertTrue(path.startswith("/"), path)
            self.assertNotIn("..", path)

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(plantor.scan_plans("/nope/nowhere", config_dir=self.config), [])

    def test_malformed_lines_are_skipped(self):
        path = os.path.join(self.dir, "s1.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("not json\n")
            handle.write(json.dumps(self.record("# Good\n", "2026-01-01T00:00:00Z")) + "\n")
        self.assertEqual([p["title"] for p in self.scan()], ["Good"])


# --------------------------------------------------------------------------
# The read-only viewer
# --------------------------------------------------------------------------


class LiveViewerTest(unittest.TestCase):
    def setUp(self):
        import tempfile, shutil
        self.config = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.config, True)
        self.cwd = "/Users/x/projects/p"
        d = os.path.join(self.config, "projects", "-Users-x-projects-p")
        os.makedirs(d)
        with open(os.path.join(d, "s.jsonl"), "w", encoding="utf-8") as handle:
            for uuid, when, plan in (
                ("a", "2026-01-01T00:00:00Z", "# Rate limiting\n\nUse Redis.\n"),
                ("b", "2026-01-02T00:00:00Z", "# Rate limiting\n\nUse Postgres.\n"),
            ):
                handle.write(json.dumps({
                    "uuid": uuid, "timestamp": when, "cwd": self.cwd,
                    "message": {"content": [{"type": "tool_use",
                                             "name": "ExitPlanMode",
                                             "input": {"plan": plan}}]},
                }) + "\n")

        self.viewer = plantor.Viewer(cwd=self.cwd, config_dir=self.config, port=0)
        self.viewer.start()
        self.addCleanup(self.viewer.stop)
        self.port = self.viewer.port
        self.token = self.viewer.token
        self.host = "127.0.0.1:%d" % self.port

    def request(self, method, path, body=None, headers=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        h.setdefault("Host", host or self.host)
        try:
            conn.request(method, path, body=body, headers=h)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()

    def auth(self, path):
        return self.request("GET", path, headers={"X-Plantor-Token": self.token})

    def test_index_shell_needs_no_token(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_index_shell_carries_no_plan_text(self):
        _, _, body = self.request("GET", "/")
        self.assertNotIn(b"Use Postgres", body)

    def test_plans_lists_newest_first(self):
        status, _, body = self.auth("/plans")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["mode"], "index")
        self.assertEqual(len(payload["plans"]), 2)
        self.assertEqual(payload["plans"][0]["when"], "2026-01-02T00:00:00Z")

    def test_plans_omits_the_plan_bodies(self):
        _, _, body = self.auth("/plans")
        self.assertNotIn(b"Use Postgres", body,
                         "the index should carry metadata, not every plan")

    def test_plans_requires_a_token(self):
        status, _, _ = self.request("GET", "/plans")
        self.assertEqual(status, 403)

    def test_a_plan_path_serves_the_shell_without_a_token(self):
        plans = json.loads(self.auth("/plans")[2].decode("utf-8"))["plans"]
        status, _, body = self.request("GET", plans[0]["path"])
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_data_returns_the_plan_and_its_predecessor(self):
        plans = json.loads(self.auth("/plans")[2].decode("utf-8"))["plans"]
        status, _, body = self.auth("/data?p=" + plans[0]["path"])
        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["mode"], "view")
        self.assertIn("Use Postgres", payload["plan"])
        self.assertIn("Use Redis", payload["previous"])

    def test_the_oldest_plan_has_no_predecessor(self):
        plans = json.loads(self.auth("/plans")[2].decode("utf-8"))["plans"]
        payload = json.loads(self.auth("/data?p=" + plans[1]["path"])[2].decode("utf-8"))
        self.assertEqual(payload["previous"], "")

    def test_an_unknown_path_is_404(self):
        self.assertEqual(self.auth("/data?p=/nope")[0], 404)

    def test_a_traversal_attempt_is_404_not_a_file_read(self):
        for probe in ("/data?p=/../../etc/passwd", "/data?p=" + UI_PATH,
                      "/../../etc/passwd"):
            status, _, body = self.auth(probe)
            self.assertIn(status, (404, 403), probe)
            self.assertNotIn(b"root:", body, probe)

    def test_the_viewer_has_no_submit_route(self):
        """Read-only is enforced by the route table, not by hiding buttons."""
        status, _, _ = self.request(
            "POST", "/submit", json.dumps({"verdict": "approve"}),
            {"Content-Type": "application/json", "X-Plantor-Token": self.token})
        self.assertEqual(status, 404)

    def test_the_viewer_carries_the_same_security_headers(self):
        _, headers, _ = self.request("GET", "/")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("no-store", headers.get("Cache-Control", ""))
        self.assertIn("default-src 'none'", headers.get("Content-Security-Policy", ""))
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_the_viewer_rejects_a_rebinding_host(self):
        status, _, _ = self.request("GET", "/", host="evil.example.com:%d" % self.port)
        self.assertEqual(status, 403)

    def test_the_viewer_survives_more_than_one_request(self):
        """Unlike a review, the viewer is long-lived: it must not latch shut."""
        for _ in range(3):
            self.assertEqual(self.auth("/plans")[0], 200)


class TestViewerCli(unittest.TestCase):
    def test_a_busy_port_reports_a_readable_error(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        self.addCleanup(sock.close)
        busy = sock.getsockname()[1]

        err = io.StringIO()
        code = plantor.main(["--view", "--port", str(busy)], stderr=err)
        self.assertEqual(code, 1)
        self.assertIn(str(busy), err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_a_bad_port_is_rejected(self):
        err = io.StringIO()
        self.assertEqual(plantor.main(["--view", "--port", "notanumber"], stderr=err), 2)
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
