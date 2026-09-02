#!/usr/bin/env python3
"""plantor — a minimal, fully-local plan review surface for Claude Code.

Fires as a PermissionRequest hook on ExitPlanMode. Renders the plan in a local
browser page, collects per-section comments and a verdict, and hands the result
back to Claude as the hook decision.

`--view` starts a long-lived, read-only browser for every plan this project has
produced, read out of the transcripts Claude Code already keeps.

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

# The page is served as a shell with no plan text in it, so the document itself
# needs no token; the plan arrives over an authenticated fetch. That is what
# makes a reload work: a refresh is a plain navigation carrying neither ?t= nor
# a header, and the page's JS never runs, so no client-side store can rescue it.
# The document must therefore be servable without a secret, which means it must
# hold none.

# Port for the read-only viewer. Fixed so the URL is stable across runs, which
# is the whole point of being able to reopen it; guessability is what the token
# is for.
DEFAULT_VIEW_PORT = 7717

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


def _unquote(text):
    """Percent-decode a query value.

    Hand-rolled to keep the standard library's URL package out of this file
    entirely: the README's audit command greps for it as a proxy for "there is
    no HTTP client here", and a guarantee you have to explain away is worth
    less than one that simply holds.

    A malformed escape is left as written. That is safe because every decoded
    value is compared by exact equality against a path plantor generated, so a
    decoding mistake can only ever produce a 404.
    """
    if "%" not in text:
        return text
    out = []
    i = 0
    while i < len(text):
        if text[i] == "%" and i + 3 <= len(text):
            try:
                out.append(chr(int(text[i + 1:i + 3], 16)))
                i += 3
                continue
            except ValueError:
                pass
        out.append(text[i])
        i += 1
    return "".join(out)


def log(message, stream=None):
    """Diagnostics go to stderr only. stdout is reserved for the decision JSON."""
    (sys.stderr if stream is None else stream).write("plantor: %s\n" % message)


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


def _plans_in_transcript(path):
    """Yield every ExitPlanMode plan recorded in one transcript, in order.

    -> [{"plan", "uuid", "when", "cwd"}]. Returns [] rather than raising on any
    unreadable or malformed transcript: a diff or a plan list is a convenience,
    and neither is worth failing a review over.
    """
    if not path or not os.path.isfile(path):
        return []
    out = []
    try:
        if os.path.getsize(path) > MAX_TRANSCRIPT_BYTES:
            return []
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "ExitPlanMode" not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
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
                        out.append({
                            "plan": plan,
                            "uuid": str(record.get("uuid") or ""),
                            "when": str(record.get("timestamp") or ""),
                            "cwd": str(record.get("cwd") or ""),
                        })
    except OSError:
        return []
    return out


def previous_plan(transcript_path, current_plan):
    """The plan from the last ExitPlanMode call, for diffing against this one.

    Read out of the transcript Claude Code already maintains rather than from
    any store of our own: plantor persists nothing, and a revision diff should
    not be the reason that changes.

    Returns "" when there is no earlier plan, the transcript is unreadable, or
    the previous plan is identical to this one -- every case degrades to
    rendering the plan whole, which is what plantor did before this existed.
    """
    # Walk backwards to the most recent plan that is not this one. The current
    # call may or may not already be recorded, depending on write ordering.
    for entry in reversed(_plans_in_transcript(transcript_path)):
        if entry["plan"].strip() != (current_plan or "").strip():
            return entry["plan"]
    return ""


def transcript_dir(cwd, config_dir=None):
    """Where Claude Code keeps this project's transcripts.

    The directory name is the working directory with every "/" turned into "-".
    That mapping is Claude Code's, not ours, so scan_plans also filters on each
    record's own `cwd` -- a change in the mangling should surface nothing rather
    than surface the wrong project.
    """
    root = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or \
        os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(root, "projects", os.path.abspath(cwd).replace(os.sep, "-"))


def scan_plans(cwd, config_dir=None):
    """Every plan this project has produced, newest first.

    -> [{"path", "title", "when", "session", "chars", "plan"}]. `path` is the
    URL the viewer serves that plan at: a slug for reading plus a short unique
    suffix, because consecutive revisions almost always share a title. It is a
    generated key, never anything derived from a request.
    """
    directory = transcript_dir(cwd, config_dir)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []

    target = os.path.abspath(cwd)
    found = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        session = name[:-len(".jsonl")]
        for entry in _plans_in_transcript(os.path.join(directory, name)):
            if entry["cwd"] and os.path.abspath(entry["cwd"]) != target:
                continue
            entry["session"] = session
            found.append(entry)

    found.sort(key=lambda e: (e["when"], e["session"]), reverse=True)

    out = []
    used = set()
    for i, entry in enumerate(found):
        stem = plan_slug(entry["plan"])
        suffix = (entry["uuid"] or "")[:8] or str(i)
        path = "/%s-%s" % (stem, suffix)
        while path in used:                    # uuids are unique; belt and braces
            suffix += "x"
            path = "/%s-%s" % (stem, suffix)
        used.add(path)
        out.append({
            "path": path,
            "title": plan_title(entry["plan"]),
            "when": entry["when"],
            "session": entry["session"],
            "chars": len(entry["plan"]),
            "plan": entry["plan"],
        })
    return out


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


class _BaseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "plantor/" + __version__
    sys_version = ""

    # -- plumbing --------------------------------------------------------

    def log_message(self, fmt, *args):
        """Silence the default stderr access log; it is pure noise here."""

    @property
    def app(self):
        """The Review or Viewer this server belongs to."""
        return self.server.app

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
        return self.headers.get("Host", "") == "%s:%d" % (HOST, self.app.port)

    def _origin_ok(self):
        """A same-origin Origin, or none at all (curl, direct navigation)."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin == "http://%s:%d" % (HOST, self.app.port)

    def _token_ok(self, supplied):
        if not supplied:
            return False
        return hmac.compare_digest(str(supplied), self.app.token)

    def _query(self, name):
        _, _, query = self.path.partition("?")
        for pair in query.split("&"):
            key, _, value = pair.partition("=")
            if key == name:
                return _unquote(value)
        return ""

    def _authed(self):
        """Host pinned, same-origin (or none), and the token in a header.

        A header rather than the query string: it cannot be set cross-origin
        without a preflight, and we send no CORS headers, so a page on another
        origin cannot read these responses even from a rebound host.
        """
        return (self._host_ok() and self._origin_ok()
                and self._token_ok(self.headers.get("X-Plantor-Token")))

    def _json(self, status, payload):
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _shell(self):
        """The page itself: no plan text, so no token needed to fetch it.

        This is what makes reload work. It is also strictly less exposed than
        embedding the plan in the HTML was -- plan text now never touches the
        document at all.
        """
        self._send(200, self.app.shell.encode("utf-8"), "text/html; charset=utf-8")

    def _path_only(self):
        return self.path.split("?", 1)[0]


class _ReviewHandler(_BaseHandler):
    """Routes for one review: the shell, its data, and a single submission."""

    @property
    def review(self):
        return self.app

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden")
        path = self._path_only()

        # The document. Compared by exact equality against values we generated;
        # no URL is ever mapped to the filesystem, so traversal remains
        # structurally impossible rather than filtered.
        if path in ("/", self.review.path):
            return self._shell()

        if path == "/data":
            if not self._authed():
                return self._send(403, b"forbidden")
            if self.review.taken:
                return self._send_already_decided()
            return self._json(200, self.review.data())

        return self._send(404, b"not found")

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


class _ViewerHandler(_BaseHandler):
    """Routes for the read-only viewer. There is no route that mutates
    anything -- read-only is a property of this table, not of hidden buttons."""

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, b"forbidden")
        path = self._path_only()

        if path == "/" or self.app.knows(path):
            return self._shell()

        if path in ("/plans", "/data"):
            if not self._authed():
                return self._send(403, b"forbidden")
            if path == "/plans":
                return self._json(200, self.app.index())
            found = self.app.plan_at(self._query("p"))
            if found is None:
                return self._send(404, b"not found")
            return self._json(200, found)

        return self._send(404, b"not found")

    def do_POST(self):
        # Named explicitly so a submission gets an honest 404 rather than the
        # 501 the base class would send for an unimplemented method.
        self._send(404, b"not found")


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
            self.shell = handle.read()

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

    def data(self):
        """What the page fetches once it has proved it holds the token."""
        return {
            "mode": "review",
            "plan": self.plan,
            "cwd": self.cwd,
            "previous": self.previous or "",
            "title": plan_title(self.plan),
        }

    def start(self):
        # Port 0: the kernel picks, so there is no guessable well-known port.
        self._httpd = ThreadingHTTPServer((HOST, 0), _ReviewHandler)
        self._httpd.app = self
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


class Viewer(object):
    """A long-lived, read-only browser for every plan in this project.

    Unlike a Review this does not latch shut after one use, which is a genuine
    change of posture: a listener stands open until you stop it. It is bounded
    the other ways that matter -- loopback only, Host pinned, token on every
    data route, and no route that mutates anything.

    Plans are re-scanned per request rather than held, so a plan written after
    the viewer started shows up without a restart, and the process holds only
    what someone is currently looking at.
    """

    def __init__(self, cwd=None, config_dir=None, port=DEFAULT_VIEW_PORT,
                 ui_path=UI_FILE):
        self.cwd = os.path.abspath(cwd or os.getcwd())
        self.config_dir = config_dir
        self.token = secrets.token_urlsafe(32)
        self._want_port = port
        self._httpd = None
        self._thread = None
        with open(ui_path, encoding="utf-8") as handle:
            self.shell = handle.read()

    # -- data ------------------------------------------------------------

    def _scan(self):
        return scan_plans(self.cwd, self.config_dir)

    def knows(self, path):
        """True for a path this viewer generated. Exact equality against the
        current scan -- a request string is never turned into a file path."""
        return any(entry["path"] == path for entry in self._scan())

    def index(self):
        plans = []
        for entry in self._scan():
            meta = dict(entry)
            meta.pop("plan", None)      # metadata only; bodies are fetched one at a time
            plans.append(meta)
        return {"mode": "index", "cwd": self.cwd, "plans": plans}

    def plan_at(self, path):
        """One plan plus the revision before it, or None if unknown."""
        plans = self._scan()
        for i, entry in enumerate(plans):
            if entry["path"] != path:
                continue
            # Newest first, so the predecessor is the next entry along.
            previous = plans[i + 1]["plan"] if i + 1 < len(plans) else ""
            return {
                "mode": "view",
                "plan": entry["plan"],
                "previous": previous,
                "cwd": self.cwd,
                "title": entry["title"],
                "when": entry["when"],
                "path": entry["path"],
            }
        return None

    # -- server ----------------------------------------------------------

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else self._want_port

    @property
    def url(self):
        return "http://%s:%d/?t=%s" % (HOST, self.port, self.token)

    def start(self):
        self._httpd = ThreadingHTTPServer((HOST, self._want_port), _ViewerHandler)
        self._httpd.app = self
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever)
        self._thread.daemon = True
        self._thread.start()
        return self

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


def serve_viewer(cwd=None, port=DEFAULT_VIEW_PORT, open_browser=True, stderr=None):
    """Run the read-only viewer until interrupted. Returns a process exit code."""
    viewer = Viewer(cwd=cwd, port=port)
    try:
        viewer.start()
    except OSError as exc:
        # Almost always "address already in use", and a traceback here is
        # useless noise for something the user fixes with --port.
        log("could not listen on %s:%d (%s)" % (HOST, port, exc), stderr)
        log("another viewer may already be running; try --port", stderr)
        return 1

    plans = viewer.index()["plans"]
    log("%d plan%s for %s" % (len(plans), "" if len(plans) == 1 else "s", viewer.cwd),
        stderr)
    if not plans:
        log("no plans found -- this project has no recorded ExitPlanMode calls",
            stderr)

    opened = False
    if open_browser:
        try:
            opened = webbrowser.open(viewer.url)
        except Exception as exc:
            log("could not open a browser (%s)" % exc, stderr)
    if opened:
        log("viewer open at http://%s:%d/ -- ^C to stop" % (HOST, viewer.port), stderr)
    else:
        log("open this to browse: %s" % viewer.url, stderr)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log("stopped", stderr)
    finally:
        viewer.stop()
    return 0


def main(argv=None, stderr=None):
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__)
        return 0
    if argv and argv[0] == "--version":
        (sys.stderr if stderr is None else stderr).write("plantor %s\n" % __version__)
        return 0

    # --view: a long-lived, read-only browser for this project's past plans.
    if argv and argv[0] == "--view":
        port = DEFAULT_VIEW_PORT
        rest = argv[1:]
        if rest[:1] == ["--port"]:
            if len(rest) < 2:
                log("--port requires a number", stderr)
                return 2
            try:
                port = int(rest[1])
            except ValueError:
                log("--port must be a number, not %r" % rest[1], stderr)
                return 2
            rest = rest[2:]
        if rest:
            log("unexpected argument %r after --view" % rest[0], stderr)
            return 2
        return serve_viewer(port=port, stderr=stderr)

    # --file: annotate any markdown file. For local testing and manual review;
    # prints the feedback to stderr rather than emitting a hook decision.
    if argv and argv[0] == "--file":
        if len(argv) < 2:
            log("--file requires a path", stderr)
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
