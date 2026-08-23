/* ifc-console chat panel.
 *
 * One ES module, no dependencies, mounted either as the whole page (/chat) or
 * as a dock beside the 3D view. Everything goes through the ifc-console
 * server on this origin: it holds the provider key, runs the tool loop, and
 * streams the result back as SSE. The browser never sees a provider URL.
 */

const STORE = "ifc-console-chat";
// The transcript follows the tab, not the machine: reloading keeps the
// conversation, closing it drops it, and nothing lands on disk.
const HISTORY = "ifc-console-chat-history";
const HISTORY_LIMIT = 40;

function isPlainObject(value) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

// ---------------------------------------------------------------- token / api
const hashParams = new URLSearchParams(location.hash.replace(/^#/, ""));
const token = hashParams.get("t") || sessionStorage.getItem("ifc-console-token") || "";
if (token) sessionStorage.setItem("ifc-console-token", token);
if (hashParams.has("t")) history.replaceState(null, "", location.pathname + location.search);

async function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: { Authorization: `Bearer ${token}`, ...(options.headers || {}) },
  });
}

async function postJSON(path, body, signal) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
}

// ------------------------------------------------------------------ markdown
// Small on purpose: fenced code, tables, lists, headings, inline styles. The
// answers we render are technical, so tables and code matter more than the
// long tail of markdown.
const esc = (s) =>
  String(s ?? "")
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
      stash.push(t + "</tbody></table>");
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
  h = h.replace(/(<\/h3>|<hr>|<\/ul>|<\/ol>|<\/table>)\n/g, "$1");
  h = h.replace(/\n/g, "<br>");
  h = h.replace(/\x00(\d+)\x00/g, (m, i) => {
    const b = blocks[i];
    const lang = b.lang ? `<span class="chat-code-lang">${esc(b.lang)}</span>` : "";
    return `<pre>${lang}<code>${esc(b.code.replace(/\n$/, ""))}</code></pre>`;
  });
  h = h.replace(/\x01(\d+)\x01/g, (m, i) => tables[i]);
  return h;
}

// --------------------------------------------------------------------- icons
// Same squared monoline system as the viewer chrome: 16 grid, 1.4 stroke.
const svg = (body, size = 16) =>
  `<svg viewBox="0 0 16 16" width="${size}" height="${size}" fill="none" stroke="currentColor" ` +
  `stroke-width="1.4" stroke-linecap="square" aria-hidden="true">${body}</svg>`;

const I = {
  send: svg('<path d="M8 13.2V3.4M8 3.4 4.2 7.2M8 3.4l3.8 3.8"/>', 15),
  clip: svg(
    '<path d="M10.8 6.2 6.9 10a1.6 1.6 0 0 1-2.3-2.3l4.6-4.6a2.6 2.6 0 0 1 3.7 3.7l-4.9 4.9a3.7 3.7 0 0 1-5.2-5.2l4-4"/>',
    15,
  ),
  stop: '<svg viewBox="0 0 16 16" width="15" height="15" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="6" height="6"/></svg>',
  gear: svg(
    '<circle cx="8" cy="8" r="4.6"/><circle cx="8" cy="8" r="1.7"/>' +
      '<path d="M8 1.6v1.8M8 12.6v1.8M14.4 8h-1.8M3.4 8H1.6' +
      'M12.53 3.47 11.25 4.75M4.75 11.25l-1.28 1.28M12.53 12.53 11.25 11.25M4.75 4.75 3.47 3.47"/>',
    15,
  ),
  plus: svg('<path d="M8 3.2v9.6M3.2 8h9.6"/>', 15),
  close: svg('<path d="m4.5 4.5 7 7M11.5 4.5l-7 7"/>', 14),
  refresh: svg(
    '<path d="M13.1 8a5.1 5.1 0 1 1-1.6-3.7" stroke-linecap="round"/><path d="M13.3 2.5v2.9h-2.9"/>',
    13,
  ),
};

// -------------------------------------------------------------------- markup
const TEMPLATE = `
<header class="chat-head">
  <span class="chat-title" data-role="title">Assistant</span>
  <select class="chat-agent" data-role="agent" hidden aria-label="Assistant agent"></select>
  <span class="chat-dot" data-role="dot" role="status" aria-live="polite"
        aria-label="Assistant unavailable" title=""></span>
  <span class="chat-spacer"></span>
  <div class="chat-actions">
    <button class="chat-icon" data-act="clear" title="New chat" aria-label="New chat">${I.plus}</button>
    <button class="chat-icon" data-act="settings" title="Provider, model and tools"
            aria-label="Chat settings" aria-haspopup="dialog"
            aria-controls="chat-settings" aria-expanded="false">${I.gear}</button>
    <button class="chat-icon" data-role="close" title="Close the panel"
            aria-label="Close the chat panel" hidden>${I.close}</button>
  </div>
</header>

<button class="chat-context" data-act="settings" title="Change the provider or model"
        aria-haspopup="dialog" aria-controls="chat-settings" aria-expanded="false">
  <span class="chat-context-model" data-role="modelname">no AI model</span>
  <span class="chat-context-meta" data-role="context"></span>
</button>

<section class="chat-evidence" data-role="evidence" hidden aria-label="Agent reference files">
  <div class="chat-evidence-head">
    <span class="chat-evidence-title" data-role="file-summary">Project references</span>
    <button class="chat-evidence-add" data-act="attach" type="button">Add files</button>
  </div>
  <div class="chat-evidence-list" data-role="file-list"></div>
</section>

<div class="chat-log" data-role="log" role="log" aria-label="Conversation"
     aria-live="off"></div>

<footer class="chat-composer">
  <div class="chat-input-wrap">
    <textarea data-role="input" rows="1" placeholder="Ask about the model..." aria-label="Message"></textarea>
    <button class="chat-attach" data-act="attach" hidden title="Add a document to the project knowledge"
            aria-label="Add a document to the project knowledge">${I.clip}</button>
    <button class="chat-send" data-act="send" title="Send" aria-label="Send message">${I.send}</button>
  </div>
  <input type="file" data-role="file" hidden multiple
         accept=".md,.markdown,.txt,.pdf,.png,.jpg,.jpeg" aria-hidden="true">
  <div class="chat-hint" data-role="status" role="status" aria-live="polite"></div>
  <div class="chat-sr" data-role="announce" role="status" aria-live="polite"></div>
</footer>

<div class="chat-modal" id="chat-settings" data-role="modal" hidden>
  <div class="chat-scrim" data-act="close-settings" aria-hidden="true"></div>
  <div class="chat-dialog" role="dialog" aria-modal="true" aria-label="Chat settings"
       tabindex="-1">
    <header class="chat-dialog-head">
      <span>Settings</span>
      <button class="chat-icon" data-act="close-settings" aria-label="Close settings">${I.close}</button>
    </header>
    <div class="chat-dialog-body">
      <div class="chat-section">Assistant model</div>
      <div class="chat-field">
        <label for="chat-provider">Provider</label>
        <select id="chat-provider" data-role="provider"></select>
        <p class="chat-help" data-role="note"></p>
      </div>

      <div class="chat-field" data-role="keyfield">
        <label for="chat-key">API key</label>
        <input id="chat-key" type="password" data-role="key" placeholder="paste a key"
               autocomplete="off" spellcheck="false">
        <p class="chat-help" data-role="keystate"></p>
      </div>

      <div class="chat-field">
        <label for="chat-model">AI model</label>
        <div class="chat-inline">
          <select id="chat-model" data-role="model"></select>
          <button class="chat-icon chat-icon-bordered" data-act="models"
                  title="Reload the model list" aria-label="Reload models">${I.refresh}</button>
        </div>
        <input class="chat-custom" type="text" data-role="modelcustom" hidden
               placeholder="model id" spellcheck="false" aria-label="Custom AI model id">
      </div>

      <div class="chat-section">Behaviour</div>
      <label class="chat-toggle">
        <input type="checkbox" data-role="tools" checked>
        <span>
          <b>Use the ifc-console tools</b>
          <small>The AI model can query and analyse the open file. Your session mode still decides whether it may change anything.</small>
        </span>
      </label>

      <details class="chat-advanced">
        <summary>Advanced</summary>
        <div class="chat-field">
          <label for="chat-baseurl">Base URL</label>
          <input id="chat-baseurl" type="text" data-role="baseurl" placeholder="provider default" spellcheck="false">
        </div>
        <div class="chat-field">
          <label for="chat-system">System prompt</label>
          <textarea id="chat-system" data-role="system" rows="3" placeholder="the ifc-console default"></textarea>
        </div>
        <div class="chat-duo">
          <div class="chat-field">
            <label for="chat-temp">Temperature</label>
            <input id="chat-temp" type="number" data-role="temp" min="0" max="2" step="0.1" placeholder="default">
          </div>
          <div class="chat-field">
            <label for="chat-maxtok">Max tokens</label>
            <input id="chat-maxtok" type="number" data-role="maxtok" min="256" step="512" placeholder="no cap">
          </div>
        </div>
      </details>
    </div>
    <footer class="chat-dialog-foot">
      <span class="chat-privacy" data-role="privacy">Prompts and tool results go to this provider.</span>
      <button class="chat-btn primary" data-act="close-settings">Done</button>
    </footer>
  </div>
</div>
`;

const STARTERS = [
  "What is in this model?",
  "Which walls have no fire rating?",
  "Quantities by storey",
  "Check the model and list the worst problems",
];

// --------------------------------------------------------------------- panel
export function mountChat(root, options = {}) {
  root.classList.add("chat-root");
  root.innerHTML = TEMPLATE;
  const el = (role) => root.querySelector(`[data-role="${role}"]`);
  const log = el("log");
  const input = el("input");
  const send = root.querySelector('[data-act="send"]');

  let turns = [];
  let providers = [];
  let busy = false;
  let aborter = null;
  let localOnly = false;
  let sessionStatus = {};
  let settingsReturnFocus = null;
  // Agent packs: server-hosted specialists behind the same panel. Plain chat
  // is agent "" and keeps its stateless turns; a pack keeps its thread on the
  // console, so we only remember the thread id here.
  let agents = [];
  let currentAgent = "";
  let referenceFiles = [];
  let agentThreads = {};
  try {
    agentThreads = JSON.parse(sessionStorage.getItem("ifc-console-agent-threads") || "{}");
    if (!isPlainObject(agentThreads)) agentThreads = {};
  } catch {
    agentThreads = {};
  }
  const saveThreads = () => {
    try {
      sessionStorage.setItem("ifc-console-agent-threads", JSON.stringify(agentThreads));
    } catch {
      /* private mode: threads just will not survive a reload */
    }
  };
  const pack = () => agents.find((agent) => agent.name === currentAgent) || null;
  const settings = loadSettings();

  if (options.onClose) {
    const close = el("close");
    close.hidden = false;
    close.addEventListener("click", options.onClose);
  }

  // ------------------------------------------------------------- settings io
  // What lives here is UI state and safe to keep. The API key is not: it goes
  // to the console, which holds it for this run only, and is never stored in
  // the browser. Model and base URL are remembered per provider, so switching
  // to Claude and back does not lose the local server you configured.
  function loadSettings() {
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(STORE) || "{}");
      if (!isPlainObject(saved)) saved = {};
    } catch {
      saved = {};
    }
    return {
      provider: saved.provider || "",
      byProvider: isPlainObject(saved.byProvider) ? saved.byProvider : {},
      system: saved.system || "",
      tools: saved.tools !== false,
      temp: saved.temp ?? "",
      maxtok: saved.maxtok ?? "",
      agent: typeof saved.agent === "string" ? saved.agent : "",
    };
  }

  function chosenModel() {
    const select = el("model");
    return select.value === "__custom__" ? el("modelcustom").value.trim() : select.value;
  }

  const remembered = (id) => settings.byProvider[id] || {};

  function saveSettings() {
    const id = el("provider").value;
    const model = chosenModel();
    const base_url = el("baseurl").value.trim();
    settings.provider = id;
    settings.byProvider[id] = { model, base_url };
    settings.system = el("system").value;
    settings.tools = el("tools").checked;
    settings.temp = el("temp").value;
    settings.maxtok = el("maxtok").value;
    settings.agent = currentAgent;
    try {
      localStorage.setItem(STORE, JSON.stringify(settings));
    } catch {
      // Storage can be unavailable in private mode or full.
    }

    const key = el("key").value.trim();
    postJSON("/api/chat/select", { provider: id, model, base_url, api_key: key || undefined })
      .then((response) => {
        if (!response.ok || !key) return;
        // the console holds it now, so drop our copy
        const p = providers.find((row) => row.id === id);
        if (p) p.has_key = true;
        el("key").value = "";
      })
      .catch(() => {})
      .finally(render);
    render();
  }

  const provider = () =>
    providers.find((p) => p.id === el("provider").value) || providers[0] || null;

  const hasKey = (p) =>
    !p || !p.needs_key || Boolean(p.key_from_env) || Boolean(p.has_key) || Boolean(el("key").value.trim());

  function providerRoute(p) {
    if (!p) return { kind: "off", label: "off", detail: "Chat is off." };
    const configured = el("baseurl").value.trim() || p.base_url || "";
    let hostname = "";
    try {
      hostname = new URL(configured).hostname.replace(/\.$/, "").toLowerCase();
    } catch {
      return { kind: "blocked", label: "invalid URL", detail: "The provider URL is invalid." };
    }
    const loopback =
      hostname === "localhost" ||
      hostname.endsWith(".localhost") ||
      hostname === "127.0.0.1" ||
      hostname === "::1";
    if (localOnly && !loopback) {
      return {
        kind: "blocked",
        label: "blocked",
        detail: "Blocked by chat.local_only. Choose a loopback provider.",
      };
    }
    return loopback
      ? { kind: "local", label: "local", detail: "Prompts stay on this machine." }
      : {
          kind: "network",
          label: "network",
          detail: `Prompts and tool results go to ${p.label}.`,
        };
  }

  function renderContext() {
    const route = providerRoute(provider());
    const mode = sessionStatus.mode || "ask";
    el("context").innerHTML =
      `<span class="chat-route ${route.kind}" title="${esc(route.detail)}">${esc(route.label)}</span>` +
      `<span class="chat-file" title="${esc(sessionStatus.model || "")}">${esc(sessionStatus.model || "no file")}</span>` +
      `<span class="chat-mode ${mode}" title="session mode">${esc(mode)}</span>` +
      (sessionStatus.dirty ? '<span class="chat-mode dirty">unsaved</span>' : "");
    el("privacy").textContent = route.detail;
  }

  // --------------------------------------------------------------- rendering
  const isReady = () => {
    const p = provider();
    return Boolean(p && chosenModel() && hasKey(p));
  };

  function render() {
    const p = provider();
    const model = chosenModel();
    const ready = isReady();
    // the dock is narrow: "Local (vLLM, LM Studio, Ollama)" becomes "Local"
    const short = p ? p.label.split(" (")[0] : "";
    el("modelname").textContent = p ? (model ? `${short} · ${model}` : `${short} · no AI model`) : "chat off";
    el("modelname").title = p ? `${p.label}${model ? " · " + model : ""}` : "";
    el("dot").className = "chat-dot" + (ready ? " ok" : "");
    el("dot").title = ready ? "ready" : "needs an AI model or API key";
    el("dot").setAttribute(
      "aria-label",
      ready ? "Assistant ready" : "Assistant needs an AI model or API key",
    );
    const usesFiles = Boolean(pack() && (pack().features || []).includes("files"));
    for (const attach of root.querySelectorAll('[data-act="attach"]')) {
      attach.hidden = !usesFiles;
    }
    el("evidence").hidden = !usesFiles;
    send.disabled = !ready && !busy;
    if (!turns.length && !busy) empty();
    if (p) {
      el("note").textContent = p.key_from_env
        ? `${p.note} Key found in ${p.key_from_env}.`
        : p.note;
      el("keyfield").hidden = !p.needs_key || Boolean(p.key_from_env);
      el("keystate").textContent = p.has_key
        ? "A key is held for this console run. Paste another to replace it."
        : "Goes to the running console for this session only, never to disk.";
    }
    if (!p) el("status").textContent = "chat is off; type /chat in the console";
    else if (!model) el("status").innerHTML = 'choose an AI model in <b>settings</b>';
    else if (!hasKey(p)) el("status").innerHTML = 'add an API key in <b>settings</b>';
    else el("status").innerHTML = "<b>Enter</b> sends · <b>Shift+Enter</b> new line";
    renderContext();
  }

  async function refreshContext() {
    try {
      const response = await api("/api/status");
      if (!response.ok) return;
      sessionStatus = await response.json();
      renderContext();
      if (typeof options.onStatus === "function") options.onStatus(sessionStatus);
    } catch {
      /* the panel still works without the badge */
    }
  }

  async function loadProviders() {
    let payload;
    try {
      const response = await api("/api/chat/providers");
      if (!response.ok) throw new Error(String(response.status));
      payload = await response.json();
    } catch {
      render();
      return;
    }
    providers = payload.providers;
    localOnly = Boolean(payload.defaults.local_only);
    const select = el("provider");
    select.innerHTML = "";
    for (const p of providers) {
      const option = document.createElement("option");
      option.value = p.id;
      option.textContent = p.label;
      select.appendChild(option);
    }
    select.value = settings.provider || payload.selected.provider || providers[0].id;
    const mine = remembered(select.value);
    el("baseurl").value = mine.base_url || payload.selected.base_url || "";
    el("system").value = settings.system || "";
    el("tools").checked = settings.tools && payload.defaults.tools;
    el("temp").value = settings.temp;
    el("maxtok").value = settings.maxtok;
    el("key").value = "";
    setModelOptions([], mine.model || payload.selected.model || provider()?.suggested_model || "");
    render();
    if (hasKey(provider())) loadModels({ quiet: true });
    // no model yet is not an error worth a modal on open: the empty state
    // offers the button, and the dialog would cover the panel every time.
  }

  async function loadAgents() {
    let payload;
    try {
      const response = await api("/api/agents");
      if (!response.ok) return;
      payload = await response.json();
    } catch {
      return;
    }
    agents = Array.isArray(payload.agents) ? payload.agents : [];
    const select = el("agent");
    const title = el("title");
    if (!agents.length) {
      select.hidden = true;
      title.hidden = false;
      if (currentAgent) switchAgent("");
      renderFiles();
      return;
    }
    select.innerHTML = "";
    const add = (value, label) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    };
    add("", "Chat");
    for (const agent of agents) add(agent.name, agent.title);
    const wanted = agents.some((a) => a.name === settings.agent) ? settings.agent : "";
    select.value = wanted;
    select.hidden = false;
    title.hidden = true;
    if (wanted !== currentAgent) switchAgent(wanted);
    else {
      render();
      loadFiles();
    }
  }

  function switchAgent(name) {
    if (busy) aborter?.abort();
    saveHistory();
    currentAgent = name;
    el("agent").value = name;
    turns = [];
    log.innerHTML = "";
    const restored = restoreHistory();
    if (!restored) empty();
    saveSettings();
    loadFiles();
    input.focus();
  }

  function formatBytes(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderFiles(problem = "") {
    const active = pack();
    const usesFiles = Boolean(active && (active.features || []).includes("files"));
    const evidence = el("evidence");
    evidence.hidden = !usesFiles;
    if (!usesFiles) return;
    const images = referenceFiles.filter((file) => file.media === "image").length;
    const documents = referenceFiles.length - images;
    el("file-summary").textContent = referenceFiles.length
      ? `${referenceFiles.length} project reference${referenceFiles.length === 1 ? "" : "s"}`
      : "No project references yet";
    const list = el("file-list");
    list.innerHTML = "";
    if (problem) {
      const warning = document.createElement("span");
      warning.className = "chat-evidence-problem";
      warning.textContent = problem;
      list.appendChild(warning);
      return;
    }
    if (!referenceFiles.length) {
      const empty = document.createElement("span");
      empty.className = "chat-evidence-empty";
      empty.textContent = "Add a manual, specification, drawing, or site image.";
      list.appendChild(empty);
      return;
    }
    const counts = document.createElement("span");
    counts.className = "chat-evidence-counts";
    counts.textContent = `${documents} doc${documents === 1 ? "" : "s"} · ${images} image${images === 1 ? "" : "s"}`;
    list.appendChild(counts);
    for (const file of referenceFiles.slice(0, 4)) {
      const chip = document.createElement("span");
      chip.className = `chat-evidence-file ${file.indexed ? "indexed" : "pending"}`;
      chip.textContent = file.name;
      chip.title = `${file.path} · ${formatBytes(file.size_bytes)} · ${file.indexed ? "indexed" : "not indexed"}`;
      list.appendChild(chip);
    }
    if (referenceFiles.length > 4) {
      const more = document.createElement("span");
      more.className = "chat-evidence-more";
      more.textContent = `+${referenceFiles.length - 4}`;
      list.appendChild(more);
    }
  }

  async function loadFiles() {
    const agent = currentAgent;
    const active = pack();
    if (!agent || !(active?.features || []).includes("files")) {
      referenceFiles = [];
      renderFiles();
      return;
    }
    try {
      const query = `agent=${encodeURIComponent(agent)}`;
      const response = await api(`/api/agents/files?${query}`);
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (agent !== currentAgent) return;
      referenceFiles = Array.isArray(payload.files) ? payload.files : [];
      renderFiles(payload.problem || "");
    } catch (exc) {
      if (agent !== currentAgent) return;
      referenceFiles = [];
      renderFiles(exc.message || String(exc));
    }
  }

  function note(text, bad = false) {
    if (!turns.length && log.querySelector(".chat-empty")) log.innerHTML = "";
    const line = document.createElement("div");
    line.className = "chat-note" + (bad ? " bad" : "");
    line.textContent = text;
    log.appendChild(line);
    scroll();
  }

  async function uploadFiles(files) {
    const agent = currentAgent;
    for (const file of files) {
      note(`uploading ${file.name}...`);
      try {
        const query = `agent=${encodeURIComponent(agent)}&name=${encodeURIComponent(file.name)}`;
        const response = await api(`/api/agents/upload?${query}`, {
          method: "POST",
          body: file,
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        if (payload.indexed) {
          note(
            `${file.name}: saved and indexed, ${payload.records} records across ` +
              `${payload.documents} project file(s)` +
              (payload.instruction_like_chunks
                ? "; instruction-shaped text was flagged as data"
                : ""),
          );
        } else {
          note(`${file.name}: saved locally but not indexed — ${payload.error}`, true);
        }
        referenceFiles = Array.isArray(payload.files) ? payload.files : referenceFiles;
        renderFiles();
      } catch (exc) {
        note(`${file.name}: ${exc.message || exc}`, true);
      }
    }
    await loadFiles();
  }

  function setModelOptions(names, selected) {
    const select = el("model");
    const custom = el("modelcustom");
    select.innerHTML = "";
    const add = (value, label) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
      return option;
    };
    if (!names.length) add("", "no AI models loaded");
    for (const name of names) add(name, name);
    if (selected && !names.includes(selected)) add(selected, selected);
    add("__custom__", "Custom id...");
    select.value = selected || (names[0] ?? "");
    custom.hidden = select.value !== "__custom__";
  }

  // Switching provider mid-load must not let the older answer win.
  let modelRequest = 0;

  async function loadModels({ quiet = false } = {}) {
    const p = provider();
    if (!p) return;
    const ticket = ++modelRequest;
    const button = root.querySelector('[data-act="models"]');
    button.classList.add("spin");
    if (!quiet) el("note").textContent = "loading models...";
    try {
      const response = await postJSON("/api/chat/models", {
        provider: p.id,
        base_url: el("baseurl").value.trim() || undefined,
        api_key: el("key").value.trim() || undefined,
      });
      const payload = await response.json();
      if (ticket !== modelRequest) return;
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      setModelOptions(payload.models, chosenModel() || p.suggested_model || "");
      if (!quiet) el("note").textContent = `${payload.models.length} model(s) available`;
      saveSettings();
    } catch (exc) {
      if (ticket !== modelRequest) return;
      if (!quiet) {
        el("note").innerHTML =
          `<span class="chat-bad">${esc(exc.message)}</span>. Type the id yourself instead`;
      }
      el("model").value = "__custom__";
      el("modelcustom").hidden = false;
    } finally {
      if (ticket === modelRequest) {
        button.classList.remove("spin");
        render();
      }
    }
  }

  function openSettings(trigger = document.activeElement) {
    settingsReturnFocus = root.contains(trigger) ? trigger : input;
    el("modal").hidden = false;
    for (const button of root.querySelectorAll('[data-act="settings"]')) {
      button.setAttribute("aria-expanded", "true");
    }
    el("provider").focus();
  }

  function closeSettings() {
    el("modal").hidden = true;
    for (const button of root.querySelectorAll('[data-act="settings"]')) {
      button.setAttribute("aria-expanded", "false");
    }
    saveSettings();
    const target = settingsReturnFocus && settingsReturnFocus.isConnected
      ? settingsReturnFocus : input;
    settingsReturnFocus = null;
    target.focus();
  }

  // ---------------------------------------------------------------- messages
  // An unconfigured panel used to say "pick a model in settings" in the status
  // line and leave the user to find it; the empty state now offers the button.
  function empty() {
    if (turns.length) return;
    const active = pack();
    const lead = active
      ? active.description
      : "Questions are answered from the file open in the console.";
    const starters = active && active.starters?.length ? active.starters : STARTERS;
    const body = isReady()
      ? `<p class="chat-empty-lead">${esc(lead)}</p>
         <div class="chat-starters">
           ${starters.map((s) => `<button class="chat-starter">${esc(s)}</button>`).join("")}
         </div>`
      : `<p class="chat-empty-lead">Nothing answers yet: choose an AI provider and model, and add a key if the provider needs one.</p>
         <div class="chat-setup">
           <button class="chat-btn primary" data-act="settings" aria-haspopup="dialog"
                   aria-controls="chat-settings" aria-expanded="false">Configure assistant</button>
         </div>`;
    log.innerHTML = `
      <div class="chat-empty">
        <p class="chat-empty-title">Ask about the open model</p>
        ${body}
        <p class="chat-empty-note">Answers come from the same tools an MCP client uses, under the same ask/edit gate.</p>
      </div>`;
    for (const button of log.querySelectorAll(".chat-starter")) {
      button.addEventListener("click", () => {
        input.value = button.textContent;
        grow();
        submit();
      });
    }
  }

  function addUser(text) {
    if (!turns.length) log.innerHTML = "";
    const div = document.createElement("div");
    div.className = "chat-msg user";
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = text;
    div.appendChild(bubble);
    log.appendChild(div);
    scroll();
  }

  function addAssistant() {
    const div = document.createElement("div");
    div.className = "chat-msg assistant";
    div.innerHTML = `
      <div class="chat-body">
        <details class="chat-think" hidden><summary><span data-role="tlabel">thinking</span></summary><div class="chat-think-body"></div></details>
        <div class="chat-tools" hidden></div>
        <div class="chat-answer"><span class="chat-cursor"></span></div>
        <div class="chat-stats"></div>
      </div>`;
    log.appendChild(div);
    scroll();
    return {
      think: div.querySelector(".chat-think"),
      thinkBody: div.querySelector(".chat-think-body"),
      tlabel: div.querySelector('[data-role="tlabel"]'),
      tools: div.querySelector(".chat-tools"),
      answer: div.querySelector(".chat-answer"),
      stats: div.querySelector(".chat-stats"),
    };
  }

  function addAnswer(text) {
    const view = addAssistant();
    view.answer.innerHTML = md(text);
    addCodeCopies(view.answer);
    view.stats.appendChild(copyButton(text));
    return view;
  }

  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 140;
  const scroll = () => {
    log.scrollTop = log.scrollHeight;
  };

  // ------------------------------------------------------------------ copying
  async function copyText(text, button) {
    const label = button.textContent;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "copied";
    } catch {
      button.textContent = "copy failed";
    }
    setTimeout(() => (button.textContent = label), 1200);
  }

  function copyButton(text, className = "chat-copy") {
    const button = document.createElement("button");
    button.className = className;
    button.type = "button";
    button.textContent = "copy";
    button.addEventListener("click", () => copyText(text, button));
    return button;
  }

  function addCodeCopies(scope) {
    for (const pre of scope.querySelectorAll("pre")) {
      if (pre.querySelector(".chat-code-copy")) continue;
      pre.appendChild(copyButton(pre.querySelector("code")?.textContent ?? "", "chat-code-copy"));
    }
  }

  // ------------------------------------------------------------------ history
  // one transcript per assistant: plain chat and each agent keep their own
  const historySlot = () => (currentAgent ? `${HISTORY}:${currentAgent}` : HISTORY);

  function saveHistory() {
    try {
      sessionStorage.setItem(historySlot(), JSON.stringify(turns.slice(-HISTORY_LIMIT)));
    } catch {
      /* private mode or quota: the conversation just will not survive a reload */
    }
  }

  function restoreHistory() {
    let saved = [];
    try {
      saved = JSON.parse(sessionStorage.getItem(historySlot()) || "[]");
    } catch {
      return false;
    }
    if (!Array.isArray(saved)) return false;
    turns = saved.filter(
      (turn) => turn && (turn.role === "user" || turn.role === "assistant") && turn.text
    );
    if (!turns.length) return false;
    log.innerHTML = "";
    for (const turn of turns) {
      if (turn.role === "user") addUser(turn.text);
      else addAnswer(turn.text);
    }
    scroll();
    return true;
  }

  function toolChip(box, name, args) {
    box.hidden = false;
    const chip = document.createElement("span");
    chip.className = "chat-tool running";
    chip.innerHTML = `<span class="chat-tool-name"></span><span class="chat-tool-state">running</span>`;
    chip.querySelector(".chat-tool-name").textContent = name;
    if (args) chip.title = args;
    box.appendChild(chip);
    scroll();
    return chip;
  }

  // ---------------------------------------------------------------- sending
  async function run() {
    const view = addAssistant();
    busy = true;
    log.setAttribute("aria-busy", "true");
    el("announce").textContent = "Assistant is responding.";
    send.disabled = false;
    send.innerHTML = I.stop;
    send.classList.add("stop");
    send.title = "Stop";
    send.setAttribute("aria-label", "Stop response");
    aborter = new AbortController();

    let answer = "";
    let reasoning = "";
    let usage = null;
    let error = "";
    let capped = false;
    const started = performance.now();
    let firstToken = null;
    const pending = new Map();

    const draw = (streaming) => {
      view.think.hidden = !reasoning.trim();
      view.thinkBody.textContent = reasoning.trim();
      const live = streaming && !answer.trim() && reasoning;
      view.tlabel.textContent = live ? "thinking" : "thoughts";
      view.tlabel.classList.toggle("shimmer", Boolean(live));
      view.answer.innerHTML =
        md(answer.trim()) + (streaming ? '<span class="chat-cursor"></span>' : "");
      if (error) {
        const box = document.createElement("div");
        box.className = "chat-error";
        box.textContent = error;
        view.answer.appendChild(box);
      }
    };

    // Re-parsing the whole answer per token is quadratic and fights the user
    // for the selection. A timer, not requestAnimationFrame: a background tab
    // stops painting frames entirely and the answer would sit invisible.
    let repaint = 0;
    const schedule = () => {
      repaint ||= setTimeout(() => {
        repaint = 0;
        draw(true);
      }, 60);
    };

    try {
      const shared = {
        provider: el("provider").value,
        model: chosenModel(),
        base_url: el("baseurl").value.trim() || undefined,
        api_key: el("key").value.trim() || undefined,
        temperature: el("temp").value === "" ? undefined : parseFloat(el("temp").value),
        max_tokens: el("maxtok").value === "" ? undefined : parseInt(el("maxtok").value, 10),
      };
      const response = await postJSON(
        currentAgent ? "/api/agents/stream" : "/api/chat/stream",
        currentAgent
          ? {
              ...shared,
              agent: currentAgent,
              prompt: turns.findLast((turn) => turn.role === "user")?.text ?? "",
              thread_id: agentThreads[currentAgent] || undefined,
            }
          : {
              ...shared,
              turns,
              system: el("system").value.trim() || undefined,
              tools: el("tools").checked,
            },
        aborter.signal
      );
      if (!response.ok) throw new Error(`chat unavailable (HTTP ${response.status})`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const parts = buffer.split("\n\n");
        buffer = parts.pop();
        for (const part of parts) {
          if (!part.startsWith("data: ")) continue;
          let event;
          try {
            event = JSON.parse(part.slice(6));
          } catch {
            continue;
          }
          const stick = nearBottom();
          if (event.type === "content") {
            firstToken ??= performance.now();
            answer += event.text;
            schedule();
          } else if (event.type === "reasoning") {
            firstToken ??= performance.now();
            reasoning += event.text;
            schedule();
          } else if (event.type === "tool_call") {
            pending.set(event.id, toolChip(view.tools, event.name, event.arguments));
          } else if (event.type === "tool_result") {
            const chip = pending.get(event.id);
            if (chip) {
              chip.className = "chat-tool " + (event.ok ? "ok" : "bad");
              chip.querySelector(".chat-tool-state").textContent = event.summary || "";
              pending.delete(event.id);
            }
          } else if (event.type === "thread") {
            agentThreads[currentAgent] = event.id;
            saveThreads();
          } else if (event.type === "usage") {
            usage = event;
          } else if (event.type === "finish") {
            capped = event.reason === "length";
          } else if (event.type === "error") {
            error = event.text;
          }
          if (stick) scroll();
        }
      }
    } catch (exc) {
      if (exc.name !== "AbortError") error = String(exc.message || exc);
    }

    clearTimeout(repaint);
    const stopped = aborter.signal.aborted;
    draw(false);
    addCodeCopies(view.answer);
    for (const chip of pending.values()) chip.className = "chat-tool bad";
    const text = answer.trim();
    const stoppedMessage = stopped && !text ? "Response stopped before content." : "";
    if (stoppedMessage) {
      view.answer.textContent = stoppedMessage;
      view.answer.classList.add("stopped");
    }
    const transcriptText = text || stoppedMessage;
    if (transcriptText) turns.push({ role: "assistant", text: transcriptText });
    saveHistory();

    const bits = [];
    if (usage) bits.push(`${usage.in ?? "?"} in / ${usage.out ?? "?"} out`);
    if (firstToken) bits.push(`${((firstToken - started) / 1000).toFixed(2)}s to first token`);
    if (usage?.out && firstToken) {
      const seconds = Math.max((performance.now() - firstToken) / 1000, 0.01);
      bits.push(`${(usage.out / seconds).toFixed(0)} tok/s`);
    }
    if (stopped) bits.push("stopped");
    if (capped) bits.push("hit the token cap");
    view.stats.textContent = bits.join(" · ");
    if (text) view.stats.appendChild(copyButton(text));
    // an answer that never arrived is worth one button, not a retyped question
    if (error && !text) {
      view.answer
        .querySelector(".chat-error")
        ?.insertAdjacentHTML(
          "beforeend",
          '<button class="chat-retry" type="button" data-act="retry">retry</button>'
        );
    }

    busy = false;
    log.setAttribute("aria-busy", "false");
    el("announce").textContent = stopped
      ? "Assistant response stopped."
      : error && !text ? "Assistant response failed." : "Assistant response ready.";
    aborter = null;
    send.innerHTML = I.send;
    send.classList.remove("stop");
    send.title = "Send";
    send.setAttribute("aria-label", "Send message");
    render();
    refreshContext();
    input.focus();
  }

  async function submit() {
    if (busy) {
      aborter?.abort();
      return;
    }
    const text = input.value.trim();
    if (!text) return;
    if (!chosenModel() || !hasKey(provider())) {
      openSettings();
      return;
    }
    input.value = "";
    grow();
    addUser(text);
    turns.push({ role: "user", text });
    saveHistory();
    saveSettings();
    await run();
  }

  function grow() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 180) + "px";
  }

  // ------------------------------------------------------------------ wiring
  input.addEventListener("input", grow);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });
  root.addEventListener("keydown", (event) => {
    if (!el("modal").hidden && event.key === "Tab") {
      const focusable = [...el("modal").querySelectorAll(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
      )].filter((node) => !node.hidden && node.offsetParent !== null);
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first) return;
      if (!focusable.includes(document.activeElement)) {
        event.preventDefault();
        first.focus();
      } else if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
      return;
    }
    if (event.key !== "Escape") return;
    if (!el("modal").hidden) {
      closeSettings();
      event.stopPropagation();
    } else if (busy) {
      aborter?.abort();
      event.stopPropagation();
    }
  });
  root.addEventListener("click", (event) => {
    const actionButton = event.target.closest("[data-act]");
    const action = actionButton?.dataset.act;
    if (action === "send") submit();
    else if (action === "settings") openSettings(actionButton);
    else if (action === "close-settings") closeSettings();
    else if (action === "models") loadModels();
    else if (action === "attach") el("file").click();
    else if (action === "clear") {
      turns = [];
      sessionStorage.removeItem(historySlot());
      if (currentAgent) {
        delete agentThreads[currentAgent];
        saveThreads();
      }
      log.innerHTML = "";
      empty();
      input.focus();
    } else if (action === "retry" && !busy) {
      if (log.lastElementChild?.classList.contains("assistant")) log.lastElementChild.remove();
      if (turns.at(-1)?.role === "assistant") turns.pop();
      run();
    }
  });
  el("agent").addEventListener("change", () => switchAgent(el("agent").value));
  el("file").addEventListener("change", async () => {
    const files = [...el("file").files];
    el("file").value = "";
    if (files.length && currentAgent) await uploadFiles(files);
  });
  el("model").addEventListener("change", () => {
    el("modelcustom").hidden = el("model").value !== "__custom__";
    if (!el("modelcustom").hidden) el("modelcustom").focus();
    saveSettings();
  });
  el("provider").addEventListener("change", () => {
    const p = provider();
    const mine = remembered(el("provider").value);
    el("key").value = "";
    el("baseurl").value = mine.base_url || "";
    setModelOptions([], mine.model || p?.suggested_model || "");
    saveSettings();
    if (hasKey(p)) loadModels({ quiet: true });
  });
  for (const role of ["modelcustom", "baseurl", "system", "tools", "temp", "maxtok", "key"]) {
    el(role).addEventListener("change", saveSettings);
  }

  const urlAgent = new URLSearchParams(location.search).get("agent");
  if (urlAgent) settings.agent = urlAgent;
  currentAgent = settings.agent || "";
  if (!restoreHistory()) empty();
  refreshContext();
  loadProviders();
  loadAgents();
  return {
    focus: () => input.focus(),
    ask: (text) => {
      input.value = text;
      submit();
    },
    refresh: refreshContext,
  };
}
