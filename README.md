# plantor

A minimal, fully-local plan review surface for Claude Code.

When Claude presents a plan, plantor opens a page in your browser where you read
it, comment on individual sections, and either approve it or send it back with
your annotations attached. That's the whole tool.

No sharing. No teammates. No accounts. No telemetry. Nothing leaves the laptop.

<sub>Inspired by [Plannotator](https://github.com/backnotprop/plannotator), which
does far more than this. plantor is an independent implementation of just the
plan-review loop, written to be small enough to audit in one sitting.</sub>

## Requirements

Python 3.9+. That's it — standard library only, no `pip install`, no Node, no
build step. It runs on the Python that ships with macOS.

## Install

```sh
git clone <your-repo-url> plantor
cd plantor
./install.sh
```

This registers a `PermissionRequest` hook matched on `ExitPlanMode` in
`~/.claude/settings.json`, backing up the existing file first. Re-running it
replaces the entry rather than stacking a second one.

Then start a new Claude Code session and enter plan mode.

To uninstall, remove the `plantor` entry from the `PermissionRequest` array in
`~/.claude/settings.json`, or restore one of the `.bak` files `install.sh` left
next to it.

## Using it

- **Hover any block** in the plan and click the `+` in the left gutter to comment
  on that section. `⌘↵` saves, `Esc` cancels.
- **Overall notes** at the bottom apply to the plan as a whole.
- **Approve** sends the plan through unchanged.
- **Request changes** sends your comments back to Claude, which revises and
  presents a new plan.

There are deliberately no single-key shortcuts for approve or reject. Submitting
is irreversible, and a bare keystroke is too easy to hit by accident.

Notes only travel with **Request changes**. An approval carries no message back
to Claude, so the page tells you rather than discarding them silently.

You can also review any markdown file without the hook:

```sh
python3 plantor.py --file some-plan.md
```

## The privacy claim, and how to check it

The claim is that nothing leaves your machine. Don't take it on faith — it's
about 400 lines, and here is how to verify it yourself:

```sh
# 1. No HTTP client exists in the source.
grep -nE 'urllib|requests|http\.client|urlopen|socket\.' plantor.py

# 2. Nothing binds anywhere but loopback.
grep -n '0\.0\.0\.0' plantor.py

# 3. The page references no external host.
grep -nE 'https?://' ui/index.html

# 4. With a review open, the only socket is a loopback listener.
lsof -nP -iTCP -a -p "$(pgrep -f plantor.py | head -1)"
```

All four come back empty or loopback-only, and the test suite asserts each of
them so they cannot silently regress.

The served page also carries a Content-Security-Policy of
`default-src 'none'; connect-src 'self'` with no `img-src` and no `font-src`, so
even an accidental external reference would be blocked by the browser.

## Security

A loopback HTTP server is not private by default: every process on the machine
can reach it, and so can any website you have open, via DNS rebinding. plantor
treats that as a real threat surface.

| Threat | Control |
|---|---|
| DNS rebinding from a malicious site | `Host` must be exactly `127.0.0.1:<port>`. A rebound request arrives with the attacker's hostname and is refused — `localhost` is not trusted either. |
| Another local process reading your plan | 256-bit token required on every route, compared in constant time. |
| Token leaking via `Referer` or history | `Referrer-Policy: no-referrer`; the page strips `?t=` from the URL on load and sends the token as a header. |
| CSRF | `Origin` must be absent or our own; no CORS headers are ever sent. |
| Port guessing | Kernel-assigned random port. |
| Path traversal | No static file serving exists. Two exact routes; no URL is ever mapped to a path. |
| Plan text persisting | `Cache-Control: no-store`. No temp files, no logs, no history. Plan text lives in memory and dies with the process. |
| XSS via plan content | Plan is delivered as inert JSON with `<` escaped, and rendered through an escaping renderer. Links render as text, never as anchors. |
| Clickjacking | `X-Frame-Options: DENY`, `frame-ancestors 'none'`. |
| Resource exhaustion | 1 MiB body cap, refused without being read. |
| Stale server | Single-use: one submission, then the process exits. |
| A second submission racing yours | The single-use latch means whoever submits first wins. A later submission gets a 410 naming the standing verdict and time, and the page says so — rather than a generic error you would retry forever. |

**Known limitation, stated plainly:** the review URL carries the token, and
`webbrowser.open()` passes that URL to a subprocess (`open` / `xdg-open`), whose
argv is visible to other local users via `ps` on a shared machine. plantor no
longer echoes the full URL to stderr unless it could not open a browser for you,
but the subprocess exposure is inherent to opening a tokenised URL — the same
tradeoff Jupyter's token scheme accepts. On a single-user laptop, which is what
this is for, that is fine. On a shared box, anyone who reads that URL before you
click can submit a verdict in your place.

## How it fails

If anything goes wrong — you close the tab, the payload is malformed, the server
can't start — plantor prints **nothing** and exits 0. Claude Code treats a
hook with no output as a non-blocking error and falls back to its own approval
dialog.

This is deliberate. A broken annotator must never silently approve a plan, and
must never silently block one either.

## Token cost

The feedback you send back lands in Claude's context on every rejection, and
rejections repeat — so the format is kept deliberately tight.
`tools/token_budget.py` measures it:

```sh
python3 tools/token_budget.py
```

Same three comments plus overall notes, three ways:

| | ~tokens | vs. baseline |
|---|---|---|
| Typed into Claude Code's own dialog | 96 | baseline |
| Plannotator's export format | 343 | 3.6x |
| plantor | **216** | 2.2x |

The largest avoidable cost is quoting. Sending the commented block back verbatim
costs ~128 tokens here — text Claude already has, word for word, in the same
context. plantor sends a section name plus a 72-character excerpt instead
(`[Approach] "Use a token bucket per API key..."`), which locates the block
precisely for far less.

It will never match the typed baseline, and shouldn't: the numbering and
per-section anchoring are why the feedback is more actionable than a paragraph.
The goal is paying for structure, not for redundancy.

(Counts are a local estimate — there is no offline Anthropic tokenizer and this
repo makes no API calls — accurate to roughly +/-10%, which is enough to compare
formats.)

## Tests

```sh
python3 -m unittest discover -s tests -v
```

62 tests. They cover the hook contract, the feedback format, every security
control above, and the no-egress guarantees.

The markdown parser lives inline in `ui/index.html` to keep the page
self-contained, but it is isolated in a DOM-free `<script id="plantor-md">`
block so it can be tested directly:

```sh
node tests/test_markdown.js   # 26 tests
```

The Python suite runs these too, and skips them if node is absent. **Node is a
dev-only dependency** — plantor itself needs nothing but Python.

## A note on the hook contract

plantor emits `decision.behavior` / `decision.message`, not the
`permissionDecision` / `permissionDecisionReason` shape currently documented for
`PermissionRequest`. **The documented shape is wrong.** This is not a guess — the
schema compiled into the Claude Code 2.1.247 binary is:

```js
{hookEventName: "PermissionRequest",
 decision: union([
   {behavior: "allow", updatedInput: record(...).optional(),
                       updatedPermissions: array(...).optional()},
   {behavior: "deny",  message: string().optional(),
                       interrupt: boolean().optional()}
 ])}
```

`permissionDecision` does not appear in it. (`deny` also accepts `interrupt:
true`, which aborts the turn outright — plantor does not use it, since a denial
is meant to make Claude revise, not stop.)

One detail worth knowing if you modify this: an `allow` decision for
`ExitPlanMode` is **silently dropped** unless it echoes `updatedInput`. It
doesn't error — it just falls back to the built-in dialog as though the hook
never ran.

## License

MIT — see [LICENSE](LICENSE).
