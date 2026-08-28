import assert from "node:assert/strict";
import { test } from "node:test";

import {
  esc,
  md,
} from "../../src/ifc_console_agents/static/chat_markdown.js";

test("model output cannot inject markup", () => {
  const out = md('<img src=x onerror="alert(1)">');
  assert.equal(out.includes("<img"), false);
  assert.equal(out.includes("&lt;img"), true);
});

test("script tags inside fenced code stay inert", () => {
  const out = md("```html\n<script>alert(1)</script>\n```");
  assert.equal(out.includes("<script"), false);
  assert.match(out, /<pre>/);
  assert.match(out, /chat-code-lang/);
});

test("tables render inside a horizontally scrollable wrapper", () => {
  const out = md("| a | b |\n| --- | --- |\n| 1 | 2 |");
  assert.match(out, /chat-table-wrap/);
  assert.match(out, /<th>a<\/th>/);
  assert.match(out, /<td>2<\/td>/);
});

test("lists, headings, and inline styles survive", () => {
  const out = md("# Title\n\n- one\n- two\n\n1. first\n\n**bold** and `code`");
  assert.match(out, /<h3>Title<\/h3>/);
  assert.match(out, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
  assert.match(out, /<ol><li>first<\/li><\/ol>/);
  assert.match(out, /<strong>bold<\/strong>/);
  assert.match(out, /<code>code<\/code>/);
});

test("only http links become anchors, and they are safe", () => {
  const safe = md("[docs](https://example.test/x)");
  assert.match(safe, /rel="noopener noreferrer"/);
  const unsafe = md("[x](javascript:alert(1))");
  assert.equal(unsafe.includes("<a "), false);
});

test("esc leaves plain text alone", () => {
  assert.equal(esc("plain text"), "plain text");
  assert.equal(esc(null), "");
});

test("an unterminated code fence still renders", () => {
  const out = md("```python\nprint(1)");
  assert.match(out, /<pre>/);
  assert.match(out, /print\(1\)/);
});

test("DSML execute calls render as Python without leaking protocol markup", () => {
  const source = [
    '<｜DSML｜tool_calls>',
    '<｜DSML｜invoke name="execute_ifc_code">',
    '<｜DSML｜parameter name="code" string="true">walls = ifc.by_type("IfcWall")',
    'print(len(walls))</｜DSML｜parameter>',
    '<｜DSML｜parameter name="description" string="true">Count walls</｜DSML｜parameter>',
    '</｜DSML｜invoke>',
    '</｜DSML｜tool_calls>',
  ].join("\n");
  const out = md(source);
  assert.match(out, /<pre>/);
  assert.match(out, />python<\/span>/);
  assert.match(out, /ifc\.by_type\(&quot;IfcWall&quot;\)/);
  assert.equal(out.includes("DSML"), false);
  assert.equal(out.includes("description"), false);
});

test("a streaming DSML code parameter renders before its closing tags arrive", () => {
  const out = md(
    '<｜DSML｜tool_calls><｜DSML｜invoke name="execute_ifc_code">' +
    '<｜DSML｜parameter name="code" string="true">print(1)',
  );
  assert.match(out, /<pre>/);
  assert.match(out, /print\(1\)/);
  assert.equal(out.includes("DSML"), false);
});

test("a wrapped bullet stays one list item", () => {
  const html = md("- Every card above is a real tool call this run made; open one\n  to see its arguments and what came back.\n- Nothing changed.");
  assert.equal((html.match(/<li>/g) || []).length, 2);
  assert.match(html, /open one to see its arguments/);
  assert.equal(html.includes("</ul>"), true);
});

test("an indented list still renders as a list", () => {
  const html = md("  - one\n  - two");
  assert.equal((html.match(/<li>/g) || []).length, 2);
});

test("headings keep their level instead of all becoming h3", () => {
  assert.match(md("# Title"), /<h3>Title<\/h3>/);
  assert.match(md("## Section"), /<h4>Section<\/h4>/);
  assert.match(md("#### Deep"), /<h6>Deep<\/h6>/);
});

test("a blockquote is quoted, not escaped into the text", () => {
  const html = md("> the manual says 240 mm");
  assert.match(html, /<blockquote>the manual says 240 mm<\/blockquote>/);
});

test("GlobalIds become viewer chips, bare or backticked, and nothing else does", () => {
  const guid = "2O2Fr$t4X7Zf8NOew3FL9r";
  const bare = md(`Wall ${guid} is thicker.`);
  assert.match(bare, /<button type="button" class="chat-guid" data-guid="2O2Fr\$t4X7Zf8NOew3FL9r"/);
  assert.ok(!bare.includes("<button" + "><button"), "no nested chips");
  const coded = md("Wall `" + guid + "` is thicker.");
  assert.match(coded, /class="chat-guid"/);
  assert.ok(!coded.includes("<code>"), "the code span became the chip");
  // a chip renders once even when the same id appears twice
  const twice = md(`${guid} and again ${guid}.`);
  assert.equal((twice.match(/chat-guid/g) || []).length, 2);
  // 22-char words that are not GUIDs stay text: wrong first char, wrong charset
  assert.ok(!md("word 9O2Fr$t4X7Zf8NOew3FL9r here").includes("chat-guid"));
  assert.ok(!md("word ABCDEFGHIJKLMNOPQRSTUV here").includes("chat-guid"));
  // ids inside fenced code blocks stay code
  assert.ok(!md("```\n" + guid + "\n```").includes("chat-guid"));
});
