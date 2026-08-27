#!/usr/bin/env python3
"""plantor — a minimal, fully-local plan review surface for Claude Code.

Fires as a PermissionRequest hook on ExitPlanMode. Renders the plan in a local
browser page, collects per-section comments and a verdict, and hands the result
back to Claude as the hook decision.

Nothing leaves the machine. The only socket is a loopback HTTP listener; there
is no HTTP client anywhere in this file and the served page's CSP forbids every
external origin.

Stdlib only, Python 3.9 compatible.
"""

import hmac
import json
import os
import re
import secrets
import sys
import time
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.1.0"

HOST = "127.0.0.1"
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")

MAX_BODY = 1024 * 1024  # 1 MiB cap on a submission

# Characters of plan text used to locate a comment. Kept short deliberately:
# Claude already has the full plan in context, so quoting the block back at it
# is pure token cost. A section name plus a few words is enough to anchor.
ANCHOR_CHARS = 72

# Where the plan JSON is injected into the page.
MARKER = "<!--PLANTOR_DATA-->"

# Cap on how much transcript we will read looking for the previous plan.
# Transcripts grow without bound; the recent entries are the ones that matter.
MAX_TRANSCRIPT_BYTES = 32 * 1024 * 1024

# Sent as a real header, not only as the page's <meta>: frame-ancestors (and
# sandbox, and report-to) are ignored by browsers when a policy arrives via
# <meta http-equiv>, so the meta tag's frame-ancestors is decorative. The
# header makes it real. X-Frame-Options is kept as well and remains
# load-bearing for any client that ignores CSP.
CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'"
)

# No self-imposed deadline: a review is bounded by the hook's own `timeout`
# setting, not by this process. Cutting a review short while the user is still
# reading it would be worse than waiting.
DEFAULT_TIMEOUT = 4 * 24 * 60 * 60  # 4 days

# Directive framing is deliberate -- Plannotator's source notes this template
# "was tuned to use strong directive framing -- Claude was ignoring softer
# phrasing." Trimmed for length, but the imperatives are load-bearing: keep
# "NOT APPROVED" and "MUST" if you edit this.
DENY_PREAMBLE = (
    "YOUR PLAN WAS NOT APPROVED.\n"
    "\n"
    "You MUST address ALL feedback below before calling ExitPlanMode again. "
    "Do not resubmit the plan unchanged, and do not change its title.\n"
)


def log(message):
    """Diagnostics go to stderr only. stdout is reserved for the decision JSON."""
    sys.stderr.write("plantor: %s\n" % message)


# ---------------------------------------------------------------------------
# Hook payload
# ---------------------------------------------------------------------------


def read_hook_input(stream):
    """Parse the PermissionRequest payload. Never raises; {} means unusable."""
    try:
        raw = stream.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        # RecursionError is a RuntimeError, not a ValueError: deeply nested
        # JSON ("[" * 3000) would otherwise escape this "never raises" promise.
        return {}
    return payload if isinstance(payload, dict) else {}


def extract_plan(payload):
    """Pull the plan markdown out of tool_input.

    Claude Code sends the plan inline. (Gemini CLI sends plan_filename instead,
    which is not our concern.)
    """
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""
    plan = tool_input.get("plan")
    return plan if isinstance(plan, str) else ""


def plan_slug(plan):
    """A readable identifier for the review URL, from the plan's title.

    The URL is otherwise distinguishable only by port number, which makes two
    open reviews indistinguishable in the browser's tab bar and history.
    """
    title = ""
    for line in (plan or "").splitlines():
        match = re.match(r"^#{1,3}\s+(.*\S)\s*$", line)
        if match:
            title = match.group(1)
            break
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:64].rstrip("-")
    return slug or "plan"


def plan_title(plan):
    """The plan's first heading, for the browser tab. "" when it has none."""
    for line in (plan or "").splitlines():
        match = re.match(r"^#{1,3}\s+(.*\S)\s*$", line)
        if match:
            return match.group(1)
    return ""


def previous_plan(transcript_path, current_plan):
    """The plan from the last ExitPlanMode call, for diffing against this one.

    Read out of the transcript Claude Code already maintains rather than from
    any store of our own: plantor persists nothing, and a revision diff should
    not be the reason that changes.

    Returns "" when there is no earlier plan, the transcript is unreadable, or
    the previous plan is identical to this one -- every case degrades to
    rendering the plan whole, which is what plantor did before this existed.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    try:
        if os.path.getsize(transcript_path) > MAX_TRANSCRIPT_BYTES:
            return ""
        plans = []
        with open(transcript_path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "ExitPlanMode" not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                message = record.get("message")
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") != "tool_use":
                        continue
                    if part.get("name") != "ExitPlanMode":
                        continue
                    plan = (part.get("input") or {}).get("plan")
                    if isinstance(plan, str) and plan.strip():
                        plans.append(plan)
    except OSError:
        return ""

    # Walk backwards to the most recent plan that is not this one. The current
    # call may or may not already be recorded, depending on write ordering.
    for plan in reversed(plans):
        if plan.strip() != (current_plan or "").strip():
            return plan
    return ""


# ---------------------------------------------------------------------------
# Feedback and decisions
# ---------------------------------------------------------------------------


def _truncate(text, limit=ANCHOR_CHARS):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def _anchor(comment):
    """Where a comment points, as briefly as possible.

    Prefers the section heading plus a short excerpt; falls back to `quote` for
    payloads that sent the whole block.
    """
    section = " ".join(str(comment.get("section", "")).split())
    excerpt = _truncate(comment.get("anchor") or comment.get("quote", ""))
    if section and excerpt and excerpt.rstrip("\u2026").strip() == section:
        # The commented block IS the heading: "[Context] \"Context\"" says it twice.
        return "[%s]" % section
    if section and excerpt:
        return '[%s] "%s"' % (section, excerpt)
    if section:
        return "[%s]" % section
    return '"%s"' % excerpt


def format_feedback(comments, notes):
    """Assemble annotations into the message Claude receives on a denial.

    This string enters Claude's context on every rejection, and rejections
    repeat, so it is kept tight: numbered items, a short anchor instead of a
    quoted block, and no headers that earn nothing. Measured by
    tools/token_budget.py.
    """
    parts = [DENY_PREAMBLE]

    for i, comment in enumerate(comments, 1):
        body = str(comment.get("body", "")).strip()
        parts.append("\n%d. %s\n   %s\n" % (i, _anchor(comment), body))

    if str(notes).strip():
        parts.append("\nNotes: %s\n" % " ".join(str(notes).split()))

    return "".join(parts)


def build_decision(result, tool_input):
    """Build the hookSpecificOutput dict, or None meaning "emit nothing".

    Returning None is the safe fallback for every unclear case: a hook that
    prints nothing is a non-blocking error, so Claude Code falls through to its
    own approval dialog. A broken annotator must never silently approve or
    silently block a plan.
    """
    if not isinstance(result, dict):
        return None

    verdict = result.get("verdict")

    if verdict == "approve":
        # updatedInput is mandatory, not decorative: Claude Code >= 2.1.199
        # silently drops an allow for ExitPlanMode when it is absent, falling
        # back to the built-in dialog. Echo the original input verbatim --
        # never a rewritten plan.
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow", "updatedInput": tool_input},
            }
        }

    if verdict == "reject":
        comments = result.get("comments") or []
        message = format_feedback(comments, result.get("notes", ""))
        return {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": message},
            }
        }

    return None


def emit(decision, stream=None):
    """Write the decision as the only thing on stdout. None writes nothing."""
    stream = sys.stdout if stream is None else stream
    if decision is None:
        return
    stream.write(json.dumps(decision))


# ---------------------------------------------------------------------------
# Review server
# ---------------------------------------------------------------------------


def _render_page(template, plan, cwd, previous=""):
    """Inject the plan into the page as inert JSON.

    Escaping '<' means a literal '</script>' inside the plan cannot terminate
    the data block, which is the only way plan text could reach the parser as
    markup rather than data.
    """
    if MARKER not in template:
        # str.replace on a missing marker is a silent no-op, which would serve a
        # page whose bootstrap throws -- a dead review with no visible error and
        # a server waiting out the full timeout.
        raise ValueError("ui template is missing the %s marker" % MARKER)
    data = json.dumps({
        "plan": plan,
        "cwd": cwd,
        "previous": previous or "",
        "title": plan_title(plan),
    })
    data = data.replace("<", "\\u003c")
    block = '<script type="application/json" id="plantor-data">%s</script>' % data
    return template.replace(MARKER, block)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "plantor/" + __version__
    sys_version = ""

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt, *args):
        """Silence the default stderr access log; it is pure noise here."""

    @property
    def review(self):
        return self.server.review

    def _send(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            # Refusals close the connection. Notably we do NOT drain an
            # oversized body first -- reading it is the exact resource
            # exhaustion the 413 exists to prevent.
            self.close_connection = True
            self.send_header("Connection", "close")
        # Never cache plan text to disk.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Deliberately no Access-Control-Allow-Origin, ever.
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_already_decided(self):
        """410 that names the standing verdict.

        A generic failure here is indistinguishable from a network blip, so a
        reviewer whose submission lost a race would retry forever without ever
        learning the plan had already been decided -- and by what.
        """
        review = self.review
        body = json.dumps({
            "error": "already_submitted",
            "verdict": (review.result or {}).get("verdict"),
            "at": review.decided_at,
        }).encode("utf-8")
        self._send(410, body, "application/json")

    # -- security gates --------------------------------------------------

    def _host_ok(self):
        """Reject anything not addressed to our literal loopback authority.

        This is the DNS-rebinding defense. A malicious site that points its own
        hostname at 127.0.0.1 still sends that hostname in Host, so it fails
        here. Only the literal IP is trusted -- not even "localhost".
        """
        return self.headers.get("Host", "") == "%s:%d" % (HOST, self.review.port)

    def _origin_ok(self):
        """A same-origin Origin, or none at all (curl, direct navigation)."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin == "http://%s:%d" % (HOST, self.review.port)

    def _token_ok(self, supplied):
        if not supplied:
            return False
        return hmac.compare_digest(str(supplied), self.review.token)

    def _query_token(self):
        _, _, query = self.path.partition("?")
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == "t":
                return value
        return ""

    def _path_only(self):
        return self.path.split("?", 1)[0]

    # -- routes ----------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden")
        if not self._token_ok(self._query_token()):
            return self._send(403, b"forbidden")
        # Two accepted GET paths, both compared by exact equality against
        # values we generated. No URL is ever mapped to the filesystem, so
        # traversal remains structurally impossible rather than filtered.
        if self._path_only() not in ("/", self.review.path):
            return self._send(404, b"not found")
        body = self.review.page.encode("utf-8")
        self._send(200, body, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._host_ok() or not self._origin_ok():
            return self._send(403, b"forbidden")
        if not self._token_ok(self.headers.get("X-Plantor-Token")):
            return self._send(403, b"forbidden")
        if self._path_only() != "/submit":
            return self._send(404, b"not found")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send(411, b"length required")
        if length <= 0:
            return self._send(411, b"length required")
        if length > MAX_BODY:
            return self._send(413, b"payload too large")

        if self.review.taken:
            return self._send_already_decided()

        try:
            body = self.rfile.read(length)
        except OSError as exc:
            # Connection dropped mid-body (tab closed, machine slept). Nothing
            # was claimed, so the review stays open for a retry.
            log("submission dropped mid-body (%s)" % exc)
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, b"bad json")
        if not isinstance(payload, dict):
            return self._send(400, b"bad json")

        # Claim, answer the browser, and only then release wait(). Setting the
        # event first would let the main thread tear the server down mid-write,
        # since shutdown() does not join handler threads.
        if not self.review.claim(payload):
            return self._send_already_decided()
        self._send(200, b'{"ok":true}', "application/json")
        self.review.commit()


class Review(object):
    """One plan review: a single-use loopback server plus its result."""

    def __init__(self, plan, cwd="", timeout=DEFAULT_TIMEOUT, ui_path=UI_FILE,
                 previous=""):
        self.plan = plan
        self.cwd = cwd
        self.previous = previous
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.slug = plan_slug(plan)
        self.done = threading.Event()
        self.result = None
        self._claimed = False
        self.decided_at = None
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        with open(ui_path, encoding="utf-8") as handle:
            self.page = _render_page(handle.read(), plan, cwd, previous)

    @property
    def taken(self):
        """True once a submission has been claimed, committed or not."""
        return self._claimed

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else 0

    @property
    def path(self):
        return "/" + self.slug

    @property
    def url(self):
        return "http://%s:%d/%s?t=%s" % (HOST, self.port, self.slug, self.token)

    def start(self):
        # Port 0: the kernel picks, so there is no guessable well-known port.
        self._httpd = ThreadingHTTPServer((HOST, 0), _Handler)
        self._httpd.review = self
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        return self

    def claim(self, payload):
        """Reserve the one submission this server accepts. Does not release wait()."""
        with self._lock:
            if self._claimed:
                return False
            self._claimed = True
            # Validate shape here, at the trust boundary. Anything holding the
            # token can post to /submit, and a bad shape reaching the formatter
            # would crash after a human already completed their review --
            # discarding it silently, which is the one outcome this tool exists
            # to prevent.
            self.decided_at = time.strftime("%H:%M:%S")
            comments = payload.get("comments")
            if not isinstance(comments, list):
                comments = []
            else:
                comments = [c for c in comments if isinstance(c, dict)]
            notes = payload.get("notes")
            self.result = {
                "verdict": payload.get("verdict"),
                "comments": comments,
                "notes": notes if isinstance(notes, str) else "",
            }
            return True

    def commit(self):
        """Release wait(), once the response is on the wire."""
        self.done.set()

    def submit(self, payload):
        """Claim and commit together (tests and non-HTTP callers)."""
        if not self.claim(payload):
            return False
        self.commit()
        return True

    def wait(self):
        """Block until submitted. None means no decision was made."""
        self.done.wait(self.timeout)
        return self.result

    def stop(self):
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def serve_review(plan, cwd="", timeout=DEFAULT_TIMEOUT, open_browser=True,
                 previous=""):
    """Run one review start to finish. Returns the result dict, or None."""
    review = Review(plan, cwd=cwd, timeout=timeout, previous=previous)
    review.start()
    try:
        # The URL carries the token, so keep it out of terminal scrollback
        # unless the user actually needs to paste it. (It is still visible in
        # the browser-opening subprocess's argv -- see README.)
        opened = False
        if open_browser:
            try:
                opened = webbrowser.open(review.url)
            except Exception as exc:
                log("could not open a browser (%s)" % exc)
        if opened:
            log("review open at http://%s:%d%s" % (HOST, review.port, review.path))
        else:
            log("open this to review: %s" % review.url)
        return review.wait()
    finally:
        review.stop()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__)
        return 0
    if argv and argv[0] == "--version":
        sys.stderr.write("plantor %s\n" % __version__)
        return 0

    # --file: annotate any markdown file. For local testing and manual review;
    # prints the feedback to stderr rather than emitting a hook decision.
    if argv and argv[0] == "--file":
        if len(argv) < 2:
            log("--file requires a path")
            return 2
        try:
            with open(argv[1], encoding="utf-8") as handle:
                plan = handle.read()
            prev = ""
            if len(argv) > 3 and argv[2] == "--against":
                with open(argv[3], encoding="utf-8") as handle:
                    prev = handle.read()
            result = serve_review(plan, cwd=os.getcwd(), previous=prev)
        except Exception as exc:
            log("review failed (%s)" % exc)
            return 1
        if result is None:
            log("no decision")
        elif result.get("verdict") == "approve":
            log("approved")
        else:
            log("changes requested:\n" + format_feedback(
                result.get("comments") or [], result.get("notes", "")))
        return 0

    # Hook mode. Every failure path below emits nothing, which Claude Code
    # treats as a non-blocking error and falls through to its own dialog.
    payload = read_hook_input(sys.stdin)
    plan = extract_plan(payload)
    if not plan:
        log("no plan content in hook event; deferring to the normal prompt")
        return 0

    try:
        result = serve_review(
            plan,
            cwd=payload.get("cwd", ""),
            previous=previous_plan(payload.get("transcript_path"), plan),
        )
        decision = build_decision(result, payload.get("tool_input") or {})
    except Exception as exc:  # never let a traceback reach stdout
        log("review failed (%s); deferring to the normal prompt" % exc)
        return 0

    emit(decision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
