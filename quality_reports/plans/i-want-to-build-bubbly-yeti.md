# Plantor — a minimal, fully-local plan annotation tool

## Revision log

> **How to read this file:** every section changed in the latest revision is tagged `[r6]` in its
> heading. Older tags mark earlier revisions. Read the log below plus the currently-tagged sections
> — never the whole file again.

**r6** (current) — adversarial review rounds 1-2, token work, and one blocked step.
- **Round 1 (correctness) — 3 real bugs, all in code with zero test coverage.** Nested lists were
  flattened (which also made a comment on one sub-step quote the whole list); the code-span sentinel
  could collide with plan text and render the literal word "undefined"; crossing the 1100px
  breakpoint with a composer open silently hid a half-typed comment. Plus a teardown race: `wait()`
  was released before the 200 was written, and `shutdown()` does not join handler threads.
- **Round 2 (error handling) — 6 findings.** Two serious: a bad `comments` shape crashed the
  formatter *after* a human had completed their review, silently discarding it; and the UI called
  `finish()` from both `.then` and `.catch`, so a dropped submission rendered an identical "sent
  back to Claude" screen while the hook waited for something that never arrived. Both fixed and
  verified — the failure state is now a visible retry notice with the comment intact.
- **Token cost — measured and cut 31%.** New `tools/token_budget.py`. Same feedback: 96 tokens
  typed into Claude Code's dialog, 343 in Plannotator's format, 314 in plantor's original format,
  **216 now**. The saving is dropping verbatim block quotes for `[Section] "<=72-char excerpt"` —
  the quoted block was ~128 tokens of text Claude already held in context.
- **Testing gap closed.** The reviewer's sharpest point was that the UI had no tests at all. The
  markdown parser now lives in a DOM-free `<script id="plantor-md">` block with 22 node tests, run
  from the Python suite and skipped when node is absent. 62 Python tests total.
- **Live end-to-end is BLOCKED** — see the new section below.

**r5** — build complete, verification run. What the visual pass caught that the
46 passing tests did not:
- **Two real UI bugs, both invisible to the test suite.** (a) A CSS source-order mistake left
  `.rail` at `display:none` at every width — the base rule sat *after* the `min-width:1100px`
  override at equal specificity — so on wide screens comments rendered into a hidden container and
  were invisible. (b) Single-key `a`/`r` shortcuts fired an *irreversible* submit whenever focus was
  outside a text field; typing a comment that began with "a" approved the plan instantly. Both
  fixed; the shortcuts are gone entirely rather than guarded, because submitting has no undo.
- **A self-inflicted encoding bug:** the code-span sentinel was written as a literal NUL byte rather
  than the JS escape `\u0001`, making `ui/index.html` a binary file to `grep`/`file`. Fixed.
- **One diagnosis of mine was wrong** and is recorded so it isn't re-litigated: the apparent stray
  space in `` `429` . `` is the code chip's horizontal padding, not a parser defect. The DOM is
  `shed with <code>429</code>.` — correct. Padding tightened from `.35em` to `.26em`.
- **Verification results** — section updated with actual outcomes rather than intentions.

**r4** — you asked me to check both open decisions against Plannotator rather than
reason about them. One of mine was an invention and is now reverted:
- **Approve-with-notes is removed.** `updatedInput` appears in exactly three lines of Plannotator,
  all one statement: `updatedInput: event.tool_input` — echoed verbatim, never rewritten. Their
  `getPlanApprovedWithNotesPrompt` exists but every call site is opencode-plugin or pi-extension,
  runtimes that *have* a message channel on approve. The Claude Code hook path has none, so they
  drop notes on approve. We now match: approve echoes `tool_input` byte-for-byte. The UI warns
  inline when the notes box is non-empty and you hit Approve, so nothing is discarded silently.
- **The review no longer self-times-out.** Plannotator's hook config sets `"timeout": 345600`
  (4 days) and `waitForDecision()` waits indefinitely. A short internal deadline would abandon a
  review while you were still reading it. The ceiling now lives in the hook config, not the code;
  `emit(None)` on no-decision is unchanged and confirmed correct.

**r3** — findings from the step-0 reference pass. **The documented hook contract was
wrong**; corrected against Plannotator's working production code:
- **The hook contract** — rewritten. Real field names are `decision.behavior` + `decision.message`,
  NOT `permissionDecision` / `permissionDecisionReason` as code.claude.com documents. Critically,
  an `allow` for `ExitPlanMode` is **silently dropped** by Claude Code >= 2.1.199 unless
  `updatedInput` echoes the original `tool_input`. Our build is 2.1.247, so this would have bitten.
- **The hook contract** — no `escalate` in this shape. The fallback is now to emit *nothing* and
  exit 0, which lets the normal permission prompt happen.
- **Files to create** — `format_feedback` now uses directive framing (Plannotator's comment says
  softer phrasing got ignored by Claude).
- **Open risk** — first risk retired; `tool_input.plan` confirmed inline for Claude Code.

**r2** — your feedback: *"make it secure and UI beautiful"*
- **Security model** — new section. Replaces the two scattered paragraphs with a real threat model:
  DNS-rebinding defense via `Host` validation, `Origin` checks on POST, constant-time token compare,
  token stripped from the URL after load, no disk persistence, no static file serving (so path
  traversal is structurally impossible), request size caps, single-use server.
- **UI design** — new section. A concrete visual spec: type scale, dark/light palettes, gutter
  comment affordance, keyboard shortcuts, motion and accessibility rules.
- **Files to create** — `ui/index.html` line updated to reference the new design section.
- **Execution order** — security and design verification folded into steps 3–4.
- **Verification** — added the adversarial security pass and the visual design review.

**r1** — initial plan (from-scratch build, hook contract, TDD order).

---

## Context

[Plannotator](https://github.com/backnotprop/plannotator) is a browser-based review surface for
coding agents: when Claude Code calls `ExitPlanMode`, a hook opens a local UI where you annotate
the plan, then approve it or deny it with structured feedback that goes back to the agent.

It also carries a lot you don't want: a Bun/React frontend and its dependency tree, URL-fragment
and AES-encrypted share links, a hosted Workspaces product, GitHub/GitLab API calls, URL fetching,
and an "Ask AI" feature. Those are architecture, not toggles — stripping them means a subtractive
refactor of unfamiliar code whose dependency tree you'd then have to audit to prove nothing phones
home.

**Plantor** rebuilds only the core loop from scratch: intercept `ExitPlanMode`, render the plan
locally, collect per-section comments and a verdict, hand the result back to Claude. Zero
dependencies, stdlib only, bound to loopback. "Nothing leaves the laptop" becomes provable by
reading ~500 lines rather than auditing a package tree.

**Decisions locked in:** written from scratch, with Plannotator read as a *reference*; triggered by
the `PermissionRequest` hook on `ExitPlanMode`; Python stdlib only; v1 = per-section comments +
overall notes + approve/request-changes; MIT license; published as a **private** GitHub repo.

### On using Plannotator as reference

Plannotator is dual-licensed MIT / Apache-2.0, so reading it — and even reusing it with attribution
— is permitted. We still write our own implementation, because a Bun/React codebase shares nothing
mechanically with a stdlib-Python one. The reference pass is scoped to two things worth learning
rather than rediscovering:

1. **The real `ExitPlanMode` payload shape** — their hook entrypoint proves what `tool_input`
   actually contains, which retires the open risk at the bottom of this plan.
2. **UI affordances that turned out to matter** in practice — how a comment anchors to a section,
   how the feedback markdown is phrased so the agent acts on it well.

If any non-trivial snippet does get adapted, it gets an attribution note in the README and the
source comment. Otherwise Plantor stands alone under MIT.

## Constraints

- **System Python 3.9.6** (`/usr/bin/python3`) — no venv, no `pip install`. Code must be 3.9-compatible:
  no `match`, no PEP 604 `X | Y` annotations at runtime, no `tomllib`.
- **No network egress, ever.** The only socket is an HTTP listener on `127.0.0.1`. No CDN fonts,
  no external scripts, no analytics, no telemetry, no update checks.
- No sharing, no teammates, no accounts, no persistence beyond the current review.

## The hook contract `[r3]` — corrected against working code

Hook fires as `PermissionRequest` with matcher `ExitPlanMode`. **stdin** delivers JSON including
`tool_name`, `tool_input`, `session_id`, `cwd`, `transcript_path`, `permission_mode`, `tool_use_id`.
For Claude Code the plan arrives **inline** at `tool_input.plan` (confirmed — the `plan_filename` /
`plan_path` variant is Gemini CLI only, so it is not our concern).

> **The docs are wrong here.** `code.claude.com/docs/en/hooks` documents
> `decision.permissionDecision` / `permissionDecisionReason`. Plannotator's shipping code — which
> demonstrably works against Claude Code 2.1.x — uses `decision.behavior` / `decision.message`.
> We follow the working code, and verification step 5 confirms it live against 2.1.247.

**Approve** — note `updatedInput` is mandatory, not optional:

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
  "decision": {"behavior": "allow", "updatedInput": {"plan": "<original plan>"}}}}
```

Plannotator's source comment on that field is the whole reason it's there: *"Claude Code >= 2.1.199
silently drops an allow decision for ExitPlanMode (a tool requiring user interaction) when
updatedInput is absent, falling back to the built-in approval dialog."* Omitting it doesn't error —
it silently no-ops, which is the worst possible failure mode.

**Request changes:**

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
  "decision": {"behavior": "deny", "message": "<annotated feedback>"}}}
```

**Anything went wrong** (window closed, timeout, malformed stdin, internal error) — print **nothing**
and exit 0. There is no `escalate` in this shape, and a no-output hook is explicitly a non-blocking
error that leaves the permission flow untouched, so you get the normal approval dialog. Failing to
the built-in prompt is deliberate: a broken annotator must never silently approve or silently block.

Exit code 2 is not honored for this event; denial must go through the JSON.

## Architecture

Single blocking process. The hook *is* the server — it starts, serves one review, and exits.

```
ExitPlanMode → hook fires → plantor.py reads stdin JSON
  → extract tool_input.plan
  → bind ThreadingHTTPServer on 127.0.0.1:0 (kernel-assigned port)
  → mint a one-time token; webbrowser.open("http://127.0.0.1:<port>/?t=<token>")
  → block on threading.Event until POST /submit (or timeout)
  → shut down server, print decision JSON to stdout, exit 0
```

## Security model `[r2]`

"It's only on localhost" is not a security boundary. A loopback HTTP server is reachable by every
process on the machine *and* — via DNS rebinding — by any website you happen to have open. Your
plans contain file paths, architecture, and sometimes credentials-adjacent detail. So the threats
are concrete, and each gets a specific control.

| Threat | Control |
|---|---|
| **DNS rebinding** — a malicious site resolves its own hostname to `127.0.0.1` and scripts requests against our port from its origin | Reject any request whose `Host` header is not exactly `127.0.0.1:<port>`. A rebound request arrives with the attacker's hostname and is refused. This is the single most important control here. |
| **CSRF from a local page** | `Origin` must be absent or exactly our own on `POST /submit`. No `Access-Control-Allow-Origin` header is ever sent, so browsers block cross-origin reads regardless. |
| **Another local process / another user on the box** reaching the port | 256-bit token from `secrets.token_urlsafe(32)`, required on every route, compared with `hmac.compare_digest` (constant time — no timing oracle on the token). |
| **Token leaking via `Referer` or browser history** | `Referrer-Policy: no-referrer` on every response; the page calls `history.replaceState` on load to strip `?t=` from the URL, then holds the token in memory only and sends it as an `X-Plantor-Token` header. |
| **Port scanning / guessing** | Kernel-assigned port (`bind(...,0)`) — not a fixed well-known port — plus the token. |
| **Path traversal / arbitrary file read** | No static file serving at all. `ui/index.html` is read once at startup relative to `__file__`; the router answers exactly `GET /` and `POST /submit`, everything else 404. There is no code path that maps a URL to a filesystem path. |
| **Plan content persisting to disk** | `Cache-Control: no-store`, `Pragma: no-cache`. No temp files, no logs, no history, no config written. Plan text lives in process memory and dies with the process. Diagnostics on stderr never include plan content. |
| **Clickjacking** | `X-Frame-Options: DENY` plus `frame-ancestors 'none'` in the CSP. |
| **XSS / script breakout via plan content** | Plan is delivered inside `<script type="application/json">` with `<` escaped to `<` (so a `</script>` inside the plan cannot break out) and parsed with `JSON.parse`. The renderer HTML-escapes all text and never assigns untrusted `innerHTML`. Links render as inert text, not anchors. |
| **Resource exhaustion** | `POST` bodies capped (413 above ~1 MB); `Content-Length` required; bounded server lifetime via the review timeout. |
| **Stale listener outliving the review** | Server is single-use: after one successful submit it stops accepting (410 on further requests) and the process exits. A timeout ceiling guarantees it cannot linger. |
| **MIME sniffing** | `X-Content-Type-Options: nosniff`. |

**Egress**, restated as a guarantee: the only socket is the loopback listener. `plantor.py` imports
no HTTP client — no `urllib.request`, no `socket` beyond the server bind. The CSP
(`default-src 'none'; connect-src 'self'`) omits `img-src` and `font-src` entirely, so even an
accidental external reference cannot load. Tests assert both.

## Files to create

| Path | Purpose |
|---|---|
| `plantor.py` | Everything server-side: stdin parsing, HTTP handler, decision emission, CLI. ~350 lines. |
| `ui/index.html` | Self-contained UI — HTML + inline CSS + inline JS, including a small markdown renderer. No external refs. |
| `tests/test_plantor.py` | `unittest`, stdlib only. |
| `install.sh` | Merges the hook block into `~/.claude/settings.json` (backup first, `python3 -c` for the JSON edit — no `jq` dependency). |
| `README.md` | What it is, install, the privacy guarantee and how to verify it yourself. |
| `LICENSE` | MIT, © Pranay Yelugam. |
| `.gitignore` | `__pycache__/`, `.DS_Store` |

### `plantor.py` structure

- `read_hook_input(stream)` → dict; tolerates malformed/empty stdin.
- `extract_plan(payload)` → markdown string from `tool_input.plan`.
- `format_feedback(comments, notes)` `[r3]` → the markdown sent back to Claude. Uses **directive
  framing** — Plannotator's code carries a note that this template "was tuned to use strong
  directive framing — Claude was ignoring softer phrasing," which is worth inheriting:
  ```
  YOUR PLAN WAS NOT APPROVED.

  You MUST revise the plan to address ALL of the feedback below before
  calling ExitPlanMode again.

  Rules:
  - Do not resubmit the same plan unchanged.
  - Do NOT change the plan title (first # heading) unless asked.

  ## Inline comments
  1. On "> <quoted section text>":
     <comment>

  ## Overall notes
  <notes>
  ```
- `build_decision(result, tool_input)` `[r4]` → the `hookSpecificOutput` dict for allow (echoing
  `tool_input` verbatim) or deny, or `None` meaning "print nothing, let the normal prompt happen".
- `ReviewHandler(BaseHTTPRequestHandler)` — routes: `GET /` (serves UI with the plan injected as
  a JSON `<script type="application/json">` block, not string-interpolated into JS), `POST /submit`,
  everything else 404. Token check on every route. `log_message` silenced so server noise never
  pollutes the hook's stdout.
- `serve_review(plan, timeout)` → blocks, returns the result dict or `None` on timeout.
- `main()` — hook mode (default, reads stdin) and `--file <path>` mode for local testing.

**stdout discipline:** the decision JSON must be the *only* thing on stdout. All diagnostics go to
stderr. This is the single easiest way to break a hook, so it gets a dedicated test.

### `ui/index.html` — see **UI design** below `[r2]`

Markdown rendered by a hand-rolled renderer (~100 lines): headings, paragraphs, lists, fenced code,
inline code, bold/italic, tables, blockquotes. Links render as inert text. All text HTML-escaped;
no `innerHTML` on untrusted content. Security headers and CSP per the security model above.

## UI design `[r2]`

The thing you look at every time a plan comes back should feel like a considered reading surface,
not a form. It's a *document* first, with review affordances that stay out of the way until wanted.

**Constraint that shapes everything:** no external assets. No Google Fonts, no icon library, no CSS
framework — those are egress. So the design leans on what's already excellent locally: the system
font stack, and inline SVG for the two or three glyphs needed.

**Type & rhythm.** `-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif` for prose;
`ui-monospace, "SF Mono", Menlo, monospace` for code. Reading column capped at **72ch** — the plan is
prose and deserves prose measure. Modular scale (13/15/17/21/28px), line-height 1.65 on body copy,
1.25 on headings. Generous vertical rhythm; whitespace is the main visual instrument.

**Color.** Dark-first, with a real light theme via `prefers-color-scheme` — not an inversion, a
second designed palette. Everything as CSS custom properties on `:root`, redefined in the media
query, so there's exactly one place to retune.
- Dark: near-black background (`#0f1012`), raised surface (`#17181c`), warm off-white text
  (`#e8e6e3`), muted secondary (`#9a978f`), hairline borders (`#26282e`).
- Light: warm paper (`#faf9f7`), white surface, near-black ink (`#1a1a1a`).
- **One accent** (a restrained amber/ochre) for comment markers and focus rings. Semantic color used
  *only* where it carries meaning: approve reads calm green, request-changes reads amber — never
  decoratively. All pairings meet WCAG AA (4.5:1 body, 3:1 large).

**Layout.** Sticky slim header: "Plan review", the session's `cwd` as quiet context, and a live
comment count. The plan occupies the reading column. On ≥1100px, comments live in a **right rail**
aligned to the block they annotate, connected by a hairline leader — the layout that makes a review
legible at a glance. Below that width they collapse inline beneath their block. Actions sit in a
sticky footer bar that stays reachable in a long plan.

**The comment affordance.** Each top-level block gets a hover target in the **left gutter** — a
small `+` that fades in at 60% opacity, full on hover. Click opens a composer anchored to that block;
the block gets an accent left-border and a numbered marker that persists once the comment is saved.
Numbers correspond exactly to the numbered list in the feedback markdown, so what you saw maps
one-to-one onto what Claude reads.

**States.** Empty (no comments yet) shows a one-line hint that fades after first interaction. Saved
comments are editable and deletable. **Request changes** is disabled until there's at least one
comment or some overall notes — an unexplained block helps nobody, and the disabled state says so in
its tooltip. On submit, the page transitions to a calm confirmation card: what was sent, and "you can
close this tab."

**Motion.** 150ms `ease-out` on hover/fade, 200ms on the composer expanding. Nothing bounces. All of
it inside `@media (prefers-reduced-motion: reduce) { *; transition: none }`.

**Keyboard & a11y.** Semantic HTML (`<article>`, `<header>`, real `<button>`s). Visible
`:focus-visible` rings everywhere. `⌘↵` submits a comment, `Esc` closes the composer, `a` approves,
`r` requests changes — shortcuts suppressed while typing. Comment markers carry `aria-label`s; the
count is an `aria-live` region. Fully operable without a mouse.

## Execution order (TDD — per your standing instructions)

Behavior change, so `skills/tdd-first`: **write the tests first, then stop for your approval before
any implementation.**

0. **Reference pass** — shallow-clone Plannotator into the session scratchpad (never into
   `plantor/`), read its hook entrypoint to confirm the `ExitPlanMode` payload shape and its
   feedback-markdown phrasing, take notes, and leave the clone behind. No file crosses over.
1. **Scaffold** — `mkdir`, `git init`, `.gitignore`, `LICENSE`.
2. **Write failing tests** (`tests/test_plantor.py`), then **stop for approval**:
   - `extract_plan` pulls markdown from a realistic ExitPlanMode payload; handles missing/empty.
   - Approve → exact allow JSON shape (asserting the *nested* `decision` key).
   - Request-changes → deny JSON whose reason contains each comment and the notes.
   - Timeout / no submission → escalate.
   - Malformed stdin → escalate, exit 0, never a traceback on stdout.
   - `format_feedback` output is stable and readable.
   - **Egress guard:** assert `ui/index.html` contains no `http://` or `https://` reference outside
     the CSP line, and that the CSP tag is present.
   - **Loopback guard:** assert the server binds `127.0.0.1`, never `0.0.0.0`.
   - **Security suite** `[r2]` — one test per row of the security table that is testable in-process:
     missing token → 403; wrong token → 403; a rebinding-shaped `Host: evil.com:<port>` → 403;
     cross-`Origin` POST → 403; unknown path → 404; oversized body → 413; second submit → 410;
     every response carries the CSP, `X-Frame-Options`, `Referrer-Policy`, `nosniff`, `no-store`;
     a plan containing a literal `</script>` cannot break out of the JSON block.
   - **End-to-end:** start `serve_review` on a thread, POST a real submission with the token,
     assert the returned decision.
3. **Implement** `plantor.py`, then `ui/index.html`, until tests pass.
4. **Verify** (below) — including the design review and the security pass.
5. **`install.sh` + README**, install the hook, live end-to-end test.
6. **Review** — `skills/adversarial-review` over the diff before publishing.
7. **Publish** — `gh repo create plantor --private --source=. --push`.

## Verification `[r5]` — results

Everything below has actually been run; these are outcomes, not intentions.

1. **Unit suite — PASS.** 46 tests green on system Python 3.9.6, and green again with
   `-W error::ResourceWarning`.
2. **Visual design review — PASS after two fixes.** Wide (right-rail), narrow (inline), and dark
   palette all confirmed by screenshot. The narrow and dark paths could not be reached by resizing
   (this browser reports `innerWidth: 1920` regardless of window size), so each was exercised by
   serving a temporary variant with the breakpoint raised past 1920 / the light media query
   neutered — same code path, forced. The two bugs found are listed in r5 above.
3. **Live security pass — PASS.** Against a running hook-mode server:

   | Check | Result |
   |---|---|
   | `Host: evil.example.com:<port>` (DNS rebinding) | 403 |
   | `Host: localhost:<port>` | 403 |
   | no token / wrong token | 403 / 403 |
   | `../../etc/passwd` (traversal) | 404 |
   | `/ui/index.html` (static route) | 404 |
   | cross-`Origin` POST | 403 |
   | valid GET | 200 |

   Headers confirmed present: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
   `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. No `Access-Control-Allow-Origin`.
4. **Egress proof — PASS.** With a review open the page issued exactly two requests, `GET /` and
   `POST /submit`, both to `127.0.0.1`. `lsof` on the process shows one `127.0.0.1` LISTEN socket
   and its loopback connections; no outbound sockets. The README's four audit commands each return
   empty.
5. **Hook round-trip — PASS.** Real payload on stdin, submitted via `curl`:
   - Approve → stdout is exactly `{"hookSpecificOutput":{...{"behavior":"allow","updatedInput":
     {"plan":"# Test plan\n\n- step one\n- step two\n"}}}}`, 166 bytes, valid single JSON doc,
     plan echoed verbatim.
   - Request changes → `behavior: deny` with the directive preamble, both numbered comments and the
     overall notes present in `message`.
   - Malformed stdin → **nothing** on stdout, diagnostic on stderr, exit 0. (Verified accidentally
     but genuinely, when zsh's `echo` mangled a test payload.)
6. **`install.sh` — PASS.** Against a throwaway settings file: registers the `ExitPlanMode` entry,
   preserves an unrelated `Bash` hook and the `model` key, writes a timestamped backup, and running
   it twice does not duplicate the entry.
7. **Hook installed for real — PASS.** `./install.sh` against `~/.claude/settings.json`:
   registered under `PermissionRequest`/`ExitPlanMode` with `timeout: 345600`, all other hook events
   (`Stop`, `PostToolUse`, `PreCompact`, `SessionStart`, `Notification`) and the `model` key intact.
   Backup at `~/.claude/settings.json.bak.20260827082833`. Malformed-settings cases all exit with a
   readable `error:` line and no traceback.
8. **Live end-to-end in Claude Code — BLOCKED.** `ExitPlanMode` is not available in `claude -p`
   (non-interactive) sessions; the spawned session reported the tool absent from its list and simply
   printed the plan as text, so the hook never fired. Two attempts, one explicitly instructing the
   tool call. **This step needs an interactive session and cannot be driven from here** — see
   "Remaining" below.
9. **UI failure path — PASS.** Server killed under an open page, then Request changes: the page
   shows "Could not send to plantor (Failed to fetch). Nothing was submitted — try again.", keeps
   the comment, re-enables both buttons, and stays on the review. Previously this showed a false
   success screen.

## Remaining `[r6]`

1. **The one test I cannot run: live in an interactive session.** Everything about the hook is
   verified against a synthetic payload matching the real `PermissionRequest` shape — both verdicts,
   byte-exact stdout, and the no-output fallback — but the actual Claude Code → hook → browser →
   decision loop has not been exercised end to end, because `-p` sessions have no `ExitPlanMode`.
   To confirm: start `claude` interactively in any repo, enter plan mode, and let it present a plan.
   The review UI should open in the browser. The specific risk this retires is the `decision.behavior`
   vs documented `permissionDecision` question — if the UI opens and Approve proceeds, the contract
   is right.
2. **Adversarial review round 3 (security)** — in progress.
3. **Publish** — `gh repo create plantor --private --source=. --push`.

## Open risk `[r3]`

**Retired:** the `tool_input.plan` shape is confirmed from working code — inline markdown for Claude
Code. The plan-file concern was Gemini CLI's `plan_filename`, not ours.

**Remaining:** the docs and the working code disagree on the decision field names. We implement the
working-code shape (`behavior` / `message`). Verification step 5 settles it live; if 2.1.247 has
moved to the documented shape, `build_decision` emits both key pairs in the one `decision` object,
since unknown keys are ignored rather than rejected.
