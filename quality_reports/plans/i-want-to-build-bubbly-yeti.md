# plantor — reloadable URLs and an on-demand plan viewer

## Revision log

> **How to read this file:** every section changed in the latest revision is tagged `[rN]` in its
> heading. Read the log plus the currently-tagged sections — never the whole file again.

**r1** (current) — new plan. The previous plan in this file covered the original build and is
complete (shipped through `e297b11`); it has been replaced rather than extended, because this is a
new feature rather than a revision of that work.

---

## Context

Two problems, one root cause.

**1. Reloading a review kills it.** `ui/index.html:1552` strips the token out of the URL with
`history.replaceState`, and `serve_review` deliberately logs the URL *without* the token
(`plantor.py`, "keep it out of terminal scrollback"). So an accidental ⌘R produces a blank
`forbidden` page, and the token now exists in neither the address bar nor the terminal. The review
is unrecoverable, while the hook goes on blocking for its full four-day ceiling. This is a latent
foot-gun in the shipped tool, independent of any new feature.

**2. A plan is only ever visible while Claude is waiting on it.** The review UI exists solely as a
hook. There is no way to go back and read a plan you already approved, or to compare two revisions
after the fact. `--file` comes closest but is single-use and shows the approve/reject bar.

**Intended outcome:** ⌘R just works, and `plantor.py --view` starts a local, read-only browser for
every plan this project has ever produced.

**Decisions locked in** (confirmed with the user): the viewer browses *all* past plans from Claude
Code's transcripts, is *strictly read-only*, and listens on a *fixed, overridable port*.

## The load-bearing constraint

**`sessionStorage` cannot fix reload, and neither can any other client-side store.** A refresh is a
plain document navigation: the browser sends no `X-Plantor-Token` and no `?t=`, the server answers
403, and the page's JavaScript never runs. Whatever the page remembered is unreachable. The three
candidates:

| Approach | Verdict |
|---|---|
| Leave the token in the URL | Works in one line, but puts the token in history and the address bar — reverting a control the security table names. |
| `Set-Cookie` | **Rejected.** Cookies ignore port. A cookie set by `127.0.0.1:7717` is sent to `127.0.0.1:<anything>`, so any other local dev server could harvest the token. Strictly worse than today. |
| **Token-less shell + authenticated data fetch** | **Chosen.** See below. |

## Approach `[r1]`

### A. Split the page into a shell and its data

Today `_render_page` injects the plan into the HTML server-side, so the document request itself must
carry the token. Instead:

- `GET /` and `GET /<plan-path>` serve a **shell**: the same `ui/index.html`, containing no plan
  text at all. **No token required** — it holds nothing secret.
- The shell's bootstrap reads the token from `?t=` (saving it to `sessionStorage`, then scrubbing
  the URL as it does today) or, on a reload, from `sessionStorage`.
- It then calls `GET /data` (index mode: `GET /plans`) with `X-Plantor-Token`, and renders.

Reload now works because the *document* needs no token and the *data* request is made by JS that has
one. The token stays out of the URL and out of history, so the existing claim holds unchanged.

This also removes an XSS surface rather than adding one: plan text stops being embedded in HTML and
arrives as a JSON response body, so `_render_page`'s `<`-escaping and its `MARKER` dance go away.
The renderer's escaping is untouched and remains the defense that matters.

**Failure path, stated explicitly:** no token, or a stale one, must render a plain "This review link
has expired — reopen it from your terminal" card, never a blank `forbidden` and never a spinner that
hangs. The project has fixed a false-success screen once already; this must not become another.

### B. `plantor.py --view` — the read-only browser

New `Viewer` class alongside `Review`, sharing the handler's security gates (`_host_ok`,
`_origin_ok`, `_token_ok`) — factor those into a small mixin or base rather than copying them.

- **Discovery:** `scan_plans(cwd)` locates `~/.claude/projects/<cwd with "/" → "-">/` (honouring
  `CLAUDE_CONFIG_DIR`), globs `*.jsonl`, and extracts every `ExitPlanMode` `tool_use` record.
  **Reuse the parser already in `previous_plan()`** — extract its record-walking loop into a shared
  `_plans_in_transcript(path)` generator and have both callers use it. Records carry `timestamp`,
  `uuid` and `cwd`; sort newest first, and filter on `cwd` so a mangled directory name can never
  surface another project's plans.
- **Routes:** `GET /plans` → the index JSON (title via the existing `plan_title`, timestamp, size,
  session id). `GET /data?id=<id>` → one plan plus the previous revision for diffing.
  **`id` is an opaque key looked up in a dict the viewer generated** — no URL segment is ever mapped
  to a filesystem path, preserving the "traversal is structurally impossible" property.
- **No `/submit` route exists in the viewer at all.** Read-only is enforced by the route table, not
  by hiding buttons.
- **Lifetime:** long-lived, until ⌃C. Plans are read from disk per request, so the process holds
  only what is currently being viewed.
- **Port:** default `7717`, overridable via `--port`. A fixed port is guessable, which is exactly
  what the token is for; `_host_ok` is unaffected. Bind failure must print a readable "port 7717 is
  in use" line, not a traceback.

### C. Read-only and index modes in the page

One flag on the fetched data drives both:

- `mode: "view"` — hide the `+` gutter, the composer, the comment rail and the whole action bar.
  **Keep the Changes / Full diff / Split / Plan switcher**, which is most of the value when reading
  an old revision.
- `mode: "index"` — render the plan list: title, when, size, and a link into each plan. Same page,
  same CSS, no new stylesheet.

## Files to change

| Path | Change |
|---|---|
| `plantor.py` | `_plans_in_transcript` extracted from `previous_plan`; `scan_plans`; `Viewer`; shared handler gates; `--view` / `--port` in `main`; `_render_page` reduced to serving the shell. |
| `ui/index.html` | Shell bootstrap + authenticated data fetch; expired-token card; `view` and `index` modes. |
| `tests/test_plantor.py` | Reload, viewer routes, read-only, scanning, index. |
| `tests/test_markdown.js` | Only if the module's surface changes — it should not. |
| `README.md` | Viewer section; update the security table's token row. |

## Verification `[r1]`

1. **Both suites green** — `python3 -m unittest discover -s tests` (86 today) and
   `node tests/test_markdown.js` (73 today).
2. **New tests, each asserting a property this plan claims:**
   - the shell contains no plan text and is served **without** a token;
   - `GET /data` without a token is 403, and with the token is 200;
   - a second `GET` of the review document after a submit still yields the 410 verdict card;
   - the viewer has **no** `/submit` route (404) and no route mutates anything;
   - `scan_plans` ignores records whose `cwd` is another project;
   - an unknown `id` is 404 and cannot reach the filesystem;
   - `--port` on an occupied port exits with a readable error, not a traceback.
3. **Reload, by hand, in the browser** — the actual bug. Open a review, ⌘R, confirm the plan comes
   back with comments-in-progress behaviour understood and stated. Then reload after submitting and
   confirm the 410 card. Then clear `sessionStorage` and reload, and confirm the expired card.
4. **Viewer, by hand** — `python3 plantor.py --view`, confirm the index lists this project's plans
   newest first, open one, confirm no `+` gutter and no action bar, confirm the diff switcher works
   between consecutive revisions, and ⌘R on both index and plan.
5. **Hook mode still works end to end** — the existing `Review` path must be unchanged in behaviour:
   drive a real payload through and confirm the approve and deny decision JSON byte-for-byte.
6. **Egress unchanged** — the README's four audit commands, plus `lsof` showing one loopback
   listener for the viewer.

## Risks

- **The shell refactor touches how plan text reaches the page** — the most security-sensitive path
  in the project. It is a net simplification (no HTML embedding at all), but the fuzz suite and the
  escaping tests must be re-run and re-read, not assumed.
- **A long-lived listener is a new posture.** Today's server is single-use and dies; the viewer
  stands open. Mitigated by read-only routes, the token, loopback-only binding and per-request reads
  — but it is a real change and belongs in the README's security table, not only in this plan.
- **The transcript directory name is derived, not given.** If Claude Code changes the mangling, the
  viewer finds nothing. It must say "no plans found for this project" plainly rather than appear
  broken.
