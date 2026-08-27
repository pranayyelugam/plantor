#!/usr/bin/env python3
"""Measure what plantor costs in Claude's context, versus not using it.

Every rejection sends `decision.message` straight into the conversation, so
that string is a recurring per-review tax. This script baselines it.

There is no local Anthropic tokenizer (the SDK's counter requires an API call,
which this repo will not make), so token counts here are a LOCAL ESTIMATE using
a BPE-shaped heuristic: words carry their leading space, long words split on
subword boundaries, digits group in threes, punctuation counts separately. It
tracks real BPE within roughly +/-10% for English prose and markdown, which is
plenty to compare packaging strategies against each other.

Usage:
    python3 tools/token_budget.py
"""

import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import plantor  # noqa: E402


# ---------------------------------------------------------------------------
# Local token estimator
# ---------------------------------------------------------------------------

_PIECE = re.compile(r"\s*\w+|\s*[^\w\s]|\s+")


def estimate_tokens(text):
    """Approximate BPE token count. Local, deterministic, no network."""
    if not text:
        return 0
    total = 0
    for piece in _PIECE.findall(text):
        stripped = piece.strip()
        if not stripped:
            # Runs of whitespace: newlines are usually their own token.
            total += piece.count("\n") or 1
            continue
        if stripped.isdigit():
            total += max(1, (len(stripped) + 2) // 3)
        elif stripped.isalpha():
            # Short words are one token; longer ones split into subwords.
            total += 1 if len(stripped) <= 6 else (len(stripped) + 4) // 5
        else:
            total += max(1, len(stripped) // 4)
    return total


def report(label, text):
    return {
        "label": label,
        "chars": len(text),
        "tokens": estimate_tokens(text),
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

# The substantive feedback -- identical in both arms of the comparison. Only the
# packaging differs. Quotes are the plan text a reviewer clicked on; in a real
# review these are long, because plan blocks are paragraphs.
COMMENTS = [
    {
        "section": "Approach",
        "quote": "Use a **token bucket** per API key, held in Redis so the limit is "
                 "shared across all four API pods. Buckets refill at a steady rate; a "
                 "burst is allowed up to the bucket size, then requests are shed with "
                 "`429`.",
        "body": "Redis becomes a hard dependency of the ingest path. Does fail-open "
                "cover a failover window where Redis is reachable but stale?",
    },
    {
        "section": "Approach",
        "quote": "- Bucket size: 200 requests - Refill rate: 20 requests/second - Key: "
                 "`ratelimit:{api_key}`, TTL 300s",
        "body": "Justify 200 against observed peak traffic rather than picking a round "
                "number.",
    },
    {
        "section": "Implementation",
        "quote": "Write the bucket in Lua so refill and decrement are atomic in one "
                 "round trip. Register the middleware, making sure it runs after auth "
                 "so we can key on the API key rather than the source IP.",
        "body": "Land the Lua script behind a feature flag so it can be disabled "
                "without a deploy.",
    },
]

NOTES = "Overall the shape is right. Split this into two changes: limiter first, then enforcement."

# Without plantor: you reject in Claude Code's own dialog and type free text.
# Same substance, no structure, no quoting -- the plan is already in context.
BASELINE = (
    "Redis becomes a hard dependency of the ingest path -- does fail-open cover a "
    "failover window where Redis is reachable but stale? Justify the bucket size of "
    "200 against observed peak traffic rather than picking a round number. Land the "
    "Lua script behind a feature flag so it can be disabled without a deploy. "
    "Overall the shape is right, but split this into two changes: limiter first, "
    "then enforcement."
)


def plannotator_format(comments, notes):
    """Plannotator's export shape, for comparison.

    Reproduced from packages/ui/utils/parser.ts (the `## N. Feedback on:` entry
    format and the "I've reviewed this plan" lead-in) plus the deny preamble in
    packages/shared/prompts.ts. Their originalText is a user text *selection*
    rather than a whole block, which is why their quoting stays cheap in
    practice -- the interaction model does the trimming, not the formatter.
    """
    out = [
        "YOUR PLAN WAS NOT APPROVED.\n\nYou MUST revise the plan to address ALL "
        "of the feedback below before calling ExitPlanMode again.\n\nRules:\n"
        "- Do not resubmit the same plan unchanged.\n"
        "- Do NOT change the plan title (first # heading) unless the user "
        "explicitly asks you to.\n\n"
    ]
    out.append("I've reviewed this plan and have %d piece%s of feedback:\n\n"
               % (len(comments), "" if len(comments) == 1 else "s"))
    for i, c in enumerate(comments, 1):
        out.append('## %d. Feedback on: "%s"\n> %s\n\n' % (i, c["quote"], c["body"]))
    if notes:
        out.append("## %d. General feedback about the plan\n> %s\n"
                   % (len(comments) + 1, notes))
    return "".join(out)


def main():
    rows = [report("No plantor (typed into Claude Code's dialog)", BASELINE)]
    rows.append(report("Plannotator format, 3 comments",
                       plannotator_format(COMMENTS, NOTES)))

    for n in (1, 2, 3):
        text = plantor.format_feedback(COMMENTS[:n], NOTES if n == 3 else "")
        rows.append(report("plantor, %d comment%s" % (n, "" if n == 1 else "s"), text))

    # The fixed cost paid on every single rejection, before any content.
    rows.append(report("plantor preamble alone (fixed cost)", plantor.DENY_PREAMBLE))

    width = max(len(r["label"]) for r in rows)
    print()
    print("%-*s  %8s  %8s" % (width, "scenario", "chars", "~tokens"))
    print("%s  %s  %s" % ("-" * width, "-" * 8, "-" * 8))
    for r in rows:
        print("%-*s  %8d  %8d" % (width, r["label"], r["chars"], r["tokens"]))

    base = rows[0]["tokens"]
    plan_ = rows[1]["tokens"]
    full = rows[4]["tokens"]
    print()
    print("Same feedback, three ways (3 comments + notes):")
    print("  typed into Claude Code   %4d tokens   (baseline)" % base)
    print("  Plannotator format       %4d tokens   %.1fx baseline" % (plan_, plan_ / float(base)))
    print("  plantor                  %4d tokens   %.1fx baseline, %.0f%% of Plannotator"
          % (full, full / float(base), 100.0 * full / plan_))
    print()
    print("Full block quoting would cost ~%d tokens of that -- text Claude already"
          % sum(estimate_tokens(c["quote"]) for c in COMMENTS))
    print("has verbatim in the same context. plantor sends a section + %d-char"
          % plantor.ANCHOR_CHARS)
    print("excerpt instead.")
    print()
    print("(Local estimate, not an API token count. See module docstring.)")


if __name__ == "__main__":
    main()
