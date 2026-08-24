/* The panel's markdown renderer, kept small and dependency-free.
 *
 * Deliberately narrow: fenced code, tables, lists, headings, and inline
 * styles. The answers rendered here are technical, so tables and code matter
 * far more than the long tail of markdown. Split out from the panel so it can
 * be unit tested without a DOM.
 */

export const esc = (value) =>
  String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

function mdInline(h) {
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(
    /\[([^\]]+)\]\((https?:[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  return h;
}

function mdTables(h, stash) {
  const isRow = (s) => /^\s*\|.*\|\s*$/.test(s);
  const isSep = (s) => /^\s*\|[\s:|-]+\|\s*$/.test(s);
  const cells = (s) =>
    s.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => mdInline(c.trim()));
  const lines = h.split("\n");
  const out = [];
  for (let i = 0; i < lines.length; i++) {
    if (isRow(lines[i]) && isSep(lines[i + 1] || "")) {
      let t =
        "<table><thead><tr>" +
        cells(lines[i]).map((c) => `<th>${c}</th>`).join("") +
        "</tr></thead><tbody>";
      for (i += 2; i < lines.length && isRow(lines[i]); i++) {
        t += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
      }
      i--;
      stash.push(`<div class="chat-table-wrap">${t}</tbody></table></div>`);
      out.push("\x01" + (stash.length - 1) + "\x01");
    } else out.push(lines[i]);
  }
  return out.join("\n");
}

export function md(src) {
  const blocks = [];
  const tables = [];
  src = String(src || "").replace(/```(\w*)\n?([\s\S]*?)(```|$)/g, (m, lang, code) => {
    blocks.push({ lang, code });
    return "\x00" + (blocks.length - 1) + "\x00";
  });
  let h = esc(src);
  h = mdTables(h, tables);
  h = h.replace(/^\s*(-{3,}|\*{3,}|_{3,})\s*$/gm, "<hr>");
  h = h.replace(/^#{1,6} (.*)$/gm, "<h3>$1</h3>");
  h = h.replace(/^[-*] (.*)$/gm, "<li>$1</li>");
  h = h.replace(/^\d+[.)] (.*)$/gm, "<oli>$1</oli>");
  h = mdInline(h);
  h = h.replace(/(?:<li>.*<\/li>\n?)+/g, (m) => "<ul>" + m.replace(/\n/g, "") + "</ul>");
  h = h.replace(/(?:<oli>.*<\/oli>\n?)+/g, (m) =>
    "<ol>" + m.replace(/oli>/g, "li>").replace(/\n/g, "") + "</ol>"
  );
  h = h.replace(/(<\/h3>|<hr>|<\/ul>|<\/ol>|<\/div>)\n/g, "$1");
  h = h.replace(/\n/g, "<br>");
  h = h.replace(/\x00(\d+)\x00/g, (m, i) => {
    const b = blocks[i];
    const lang = b.lang ? `<span class="chat-code-lang">${esc(b.lang)}</span>` : "";
    return `<pre>${lang}<code>${esc(b.code.replace(/\n$/, ""))}</code></pre>`;
  });
  h = h.replace(/\x01(\d+)\x01/g, (m, i) => tables[i]);
  return h;
}

export default md;
