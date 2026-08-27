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


class TestRenderPage(unittest.TestCase):
    def test_missing_marker_raises_rather_than_serving_a_dead_page(self):
        """str.replace is a silent no-op, which would serve a page whose
        bootstrap throws: no buttons, no error, server waiting out the timeout."""
        with self.assertRaises(ValueError):
            plantor._render_page("<html>no marker</html>", "# p", "/tmp")

    def test_marker_is_substituted(self):
        out = plantor._render_page(
            "<html>" + plantor.MARKER + "</html>", "# p", "/tmp")
        self.assertIn('id="plantor-data"', out)
        self.assertNotIn(plantor.MARKER, out)


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

    def test_binds_loopback_only(self):
        src = read_src()
        self.assertIn("127.0.0.1", src)
        self.assertNotIn("0.0.0.0", src)


# --------------------------------------------------------------------------
# Live server: security controls + end-to-end
# --------------------------------------------------------------------------


class LiveServerTest(unittest.TestCase):
    """Boots a real review server on a random loopback port for each test."""

    plan = "# Live plan\n\n- do a thing\n"

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

    # -- routing ---------------------------------------------------------

    def test_get_root_with_token_serves_ui(self):
        status, headers, body = self.request("GET", "/?t=" + self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<!doctype html", body.lower())

    def test_unknown_path_is_404(self):
        status, _, _ = self.request("GET", "/../../etc/passwd?t=" + self.token)
        self.assertEqual(status, 404)

    def test_no_static_file_route_exists(self):
        status, _, _ = self.request("GET", "/ui/index.html?t=" + self.token)
        self.assertEqual(status, 404)

    # -- token -----------------------------------------------------------

    def test_missing_token_is_403(self):
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 403)

    def test_wrong_token_is_403(self):
        status, _, _ = self.request("GET", "/?t=wrongtoken")
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

    def test_script_tag_in_plan_cannot_break_out(self):
        review = plantor.Review("# x\n\n</script><script>alert(1)</script>\n", timeout=10)
        review.start()
        self.addCleanup(review.stop)
        conn = http.client.HTTPConnection("127.0.0.1", review.port, timeout=10)
        conn.request(
            "GET", "/?t=" + review.token, headers={"Host": "127.0.0.1:%d" % review.port}
        )
        body = conn.getresponse().read().decode("utf-8")
        conn.close()
        payload = re.search(
            r'<script type="application/json" id="plantor-data">(.*?)</script>',
            body,
            re.S,
        )
        self.assertIsNotNone(payload, "plan data block not found")
        self.assertNotIn("</script>", payload.group(1))
        self.assertNotIn("<script>alert(1)", payload.group(1))
        # and it must still round-trip as the original text
        self.assertIn("alert(1)", json.loads(payload.group(1))["plan"])

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


if __name__ == "__main__":
    unittest.main()
