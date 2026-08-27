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
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

__version__ = "0.1.0"

HOST = "127.0.0.1"
UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html")

MAX_BODY = 1024 * 1024  # 1 MiB cap on a submission
MAX_QUOTE = 240  # characters of quoted plan text per comment

# No self-imposed deadline: a review is bounded by the hook's own `timeout`
# setting, not by this process. Cutting a review short while the user is still
# reading it would be worse than waiting.
DEFAULT_TIMEOUT = 4 * 24 * 60 * 60  # 4 days

DENY_PREAMBLE = (
    "YOUR PLAN WAS NOT APPROVED.\n"
    "\n"
    "You MUST revise the plan to address ALL of the feedback below before "
    "calling ExitPlanMode again.\n"
    "\n"
    "Rules:\n"
    "- Do not resubmit the same plan unchanged.\n"
    "- Do NOT change the plan title (first # heading) unless the user "
    "explicitly asks you to.\n"
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
    except ValueError:
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


# ---------------------------------------------------------------------------
# Feedback and decisions
# ---------------------------------------------------------------------------


def _truncate(text, limit=MAX_QUOTE):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_feedback(comments, notes):
    """Assemble annotations into the message Claude receives on a denial.

    The directive framing is deliberate and inherited from Plannotator, whose
    source notes the template "was tuned to use strong directive framing —
    Claude was ignoring softer phrasing."
    """
    parts = [DENY_PREAMBLE]

    if comments:
        parts.append("\n## Inline comments\n")
        for i, comment in enumerate(comments, 1):
            quote = _truncate(comment.get("quote", ""))
            body = str(comment.get("body", "")).strip()
            parts.append('\n%d. On "> %s":\n   %s\n' % (i, quote, body))

    if str(notes).strip():
        parts.append("\n## Overall notes\n\n%s\n" % str(notes).strip())

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


def _render_page(template, plan, cwd):
    """Inject the plan into the page as inert JSON.

    Escaping '<' means a literal '</script>' inside the plan cannot terminate
    the data block, which is the only way plan text could reach the parser as
    markup rather than data.
    """
    data = json.dumps({"plan": plan, "cwd": cwd})
    data = data.replace("<", "\\u003c")
    block = '<script type="application/json" id="plantor-data">%s</script>' % data
    return template.replace("<!--PLANTOR_DATA-->", block)


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
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Deliberately no Access-Control-Allow-Origin, ever.
        self.end_headers()
        if body:
            self.wfile.write(body)

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
        # Exactly one GET route. No path is ever mapped to the filesystem, so
        # traversal is structurally impossible rather than filtered.
        if self._path_only() != "/":
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

        if self.review.done.is_set():
            return self._send(410, b"review already submitted")

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._send(400, b"bad json")
        if not isinstance(payload, dict):
            return self._send(400, b"bad json")

        accepted = self.review.submit(payload)
        if not accepted:
            return self._send(410, b"review already submitted")
        self._send(200, b'{"ok":true}', "application/json")


class Review(object):
    """One plan review: a single-use loopback server plus its result."""

    def __init__(self, plan, cwd="", timeout=DEFAULT_TIMEOUT, ui_path=UI_FILE):
        self.plan = plan
        self.cwd = cwd
        self.timeout = timeout
        self.token = secrets.token_urlsafe(32)
        self.done = threading.Event()
        self.result = None
        self._lock = threading.Lock()
        self._httpd = None
        self._thread = None
        with open(ui_path, encoding="utf-8") as handle:
            self.page = _render_page(handle.read(), plan, cwd)

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else 0

    @property
    def url(self):
        return "http://%s:%d/?t=%s" % (HOST, self.port, self.token)

    def start(self):
        # Port 0: the kernel picks, so there is no guessable well-known port.
        self._httpd = ThreadingHTTPServer((HOST, 0), _Handler)
        self._httpd.review = self
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        return self

    def submit(self, payload):
        """Record the one submission this server will accept."""
        with self._lock:
            if self.done.is_set():
                return False
            self.result = {
                "verdict": payload.get("verdict"),
                "comments": payload.get("comments") or [],
                "notes": payload.get("notes") or "",
            }
            self.done.set()
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


def serve_review(plan, cwd="", timeout=DEFAULT_TIMEOUT, open_browser=True):
    """Run one review start to finish. Returns the result dict, or None."""
    review = Review(plan, cwd=cwd, timeout=timeout)
    review.start()
    try:
        log("review at %s" % review.url)
        if open_browser:
            webbrowser.open(review.url)
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
        with open(argv[1], encoding="utf-8") as handle:
            plan = handle.read()
        result = serve_review(plan, cwd=os.getcwd())
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
        result = serve_review(plan, cwd=payload.get("cwd", ""))
    except Exception as exc:  # never let a traceback reach stdout
        log("review failed (%s); deferring to the normal prompt" % exc)
        return 0

    emit(build_decision(result, payload.get("tool_input") or {}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
