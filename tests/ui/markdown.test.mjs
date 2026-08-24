import assert from "node:assert/strict";
import { test } from "node:test";

import {
  esc,
  md,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/chat_markdown.js";

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
