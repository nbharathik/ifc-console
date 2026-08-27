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

// An IFC GlobalId is 22 chars of this base64 variant, and its first char
// encodes the UUID's top bits, so it is always 0-3. Tight enough that plain
// words never match; loose enough that every real id does.
const GUID_CHIP =
  '<button type="button" class="chat-guid" data-guid="$1" ' +
  'title="Select this element in the 3D view">$1</button>';

/** GlobalIds become live chips: click one, the viewer frames that element. */
function mdGlobalIds(h) {
  h = h.replace(/<code>([0-3][0-9A-Za-z_$]{21})<\/code>/g, GUID_CHIP);
  // ids already wrapped by the pass above sit right before </button>
  h = h.replace(
    /(^|[\s>({[,;:])([0-3][0-9A-Za-z_$]{21})(?!<\/button>)(?=$|[\s<)\]}.,;:])/g,
    (m, pre, id) => pre + GUID_CHIP.replaceAll("$1", id)
  );
  return h;
}

function mdInline(h) {
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(
    /\[([^\]]+)\]\((https?:[^\s)]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
  );
  h = h.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  h = h.replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  h = mdGlobalIds(h);
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

/* A wrapped list item is still one item.
 *
 * Models wrap long bullets at ~80 columns. Matching list markers line by line
 * left the continuation outside the <li>, so half the sentence appeared
 * unindented under the list and read as a new paragraph.
 */
function mdListContinuations(h) {
  const isItem = (line) => /^\s*(?:[-*]|\d+[.)]) /.test(line);
  const out = [];
  let open = false;
  for (const line of h.split("\n")) {
    if (open && !isItem(line) && line.trim() && /^\s+\S/.test(line)) {
      out[out.length - 1] += " " + line.trim();
      continue;
    }
    open = isItem(line);
    out.push(line);
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
  h = h.replace(/^(#{1,6}) (.*)$/gm, (m, hashes, body) => {
    const level = Math.min(hashes.length + 2, 6);
    return `<h${level}>${body}</h${level}>`;
  });
  h = h.replace(/^&gt; ?(.*)$/gm, "<blockquote>$1</blockquote>");
  h = mdListContinuations(h);
  h = h.replace(/^\s*[-*] (.*)$/gm, "<li>$1</li>");
  h = h.replace(/^\s*\d+[.)] (.*)$/gm, "<oli>$1</oli>");
  h = mdInline(h);
  h = h.replace(/(?:<li>.*<\/li>\n?)+/g, (m) => "<ul>" + m.replace(/\n/g, "") + "</ul>");
  h = h.replace(/(?:<oli>.*<\/oli>\n?)+/g, (m) =>
    "<ol>" + m.replace(/oli>/g, "li>").replace(/\n/g, "") + "</ol>"
  );
  h = h.replace(/<\/blockquote>\n<blockquote>/g, "<br>");
  h = h.replace(/(<\/h[3-6]>|<hr>|<\/ul>|<\/ol>|<\/div>|<\/blockquote>)\n/g, "$1");
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
