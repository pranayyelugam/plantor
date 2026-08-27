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

if (failures.length) {
  failures.forEach(f => {
    console.error("FAIL: " + f.name);
    console.error("      " + f.err.message);
  });
  console.error("\n" + failures.length + " failed, " + passed + " passed");
  process.exit(1);
}
console.log("ok - " + passed + " markdown tests passed");
