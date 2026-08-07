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
  String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

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
const I = {
  send: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  stop: '<svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor"><rect x="5" y="5" width="14" height="14" rx="2"/></svg>',
  gear: '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="2.4"/><path d="M8 1.6v1.7M8 12.7v1.7M14.4 8h-1.7M3.3 8H1.6M12.5 3.5l-1.2 1.2M4.7 11.3l-1.2 1.2M12.5 12.5l-1.2-1.2M4.7 4.7L3.5 3.5" stroke-linecap="round"/></svg>',
  plus: '<svg viewBox="0 0 16 16" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M8 3.2v9.6M3.2 8h9.6"/></svg>',
  close: '<svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>',
  refresh: '<svg viewBox="0 0 16 16" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="M13.2 8a5.2 5.2 0 1 1-1.6-3.7"/><path d="M13.4 2.4v2.9h-2.9"/></svg>',
};

// -------------------------------------------------------------------- markup
const TEMPLATE = `
<header class="chat-head">
  <div class="chat-id">
    <span class="chat-dot" data-role="dot" title=""></span>
    <div class="chat-id-text">
      <div class="chat-id-model" data-role="modelname">chat</div>
      <div class="chat-id-context" data-role="context"></div>
    </div>
  </div>
  <div class="chat-actions">
    <button class="chat-icon" data-act="clear" title="New chat" aria-label="New chat">${I.plus}</button>
    <button class="chat-icon" data-act="settings" title="Settings" aria-label="Settings">${I.gear}</button>
    <button class="chat-icon" data-role="close" title="Close" aria-label="Close" hidden>${I.close}</button>
  </div>
</header>

<div class="chat-log" data-role="log"></div>

<footer class="chat-composer">
  <div class="chat-input-wrap">
    <textarea data-role="input" rows="1" placeholder="Ask about the model..." aria-label="Message"></textarea>
    <button class="chat-send" data-act="send" title="Send" aria-label="Send">${I.send}</button>
  </div>
  <div class="chat-hint" data-role="status"></div>
</footer>

<div class="chat-modal" data-role="modal" hidden>
  <div class="chat-scrim" data-act="close-settings"></div>
  <div class="chat-dialog" role="dialog" aria-modal="true" aria-label="Chat settings">
    <header class="chat-dialog-head">
      <span>Settings</span>
      <button class="chat-icon" data-act="close-settings" aria-label="Close settings">${I.close}</button>
    </header>
    <div class="chat-dialog-body">
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
        <label for="chat-model">Model</label>
        <div class="chat-inline">
          <select id="chat-model" data-role="model"></select>
          <button class="chat-icon chat-icon-bordered" data-act="models"
                  title="Reload the model list" aria-label="Reload models">${I.refresh}</button>
        </div>
        <input class="chat-custom" type="text" data-role="modelcustom" hidden
               placeholder="model id" spellcheck="false">
      </div>

      <label class="chat-toggle">
        <input type="checkbox" data-role="tools" checked>
        <span>
          <b>Use the ifc-console tools</b>
          <small>The model can query and analyse the open file. Your session mode still decides whether it may change anything.</small>
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
      <span class="chat-privacy">Prompts and tool results go to this provider.</span>
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
    } catch {
      saved = {};
    }
    return {
      provider: saved.provider || "",
      byProvider: saved.byProvider || {},
      system: saved.system || "",
      tools: saved.tools !== false,
      temp: saved.temp ?? "",
      maxtok: saved.maxtok ?? "",
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
    localStorage.setItem(STORE, JSON.stringify(settings));

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

  // --------------------------------------------------------------- rendering
  function render() {
    const p = provider();
    const model = chosenModel();
    const ready = Boolean(p && model && hasKey(p));
    // the dock is narrow: "Local (vLLM, LM Studio, Ollama)" becomes "Local"
    const short = p ? p.label.split(" (")[0] : "";
    el("modelname").textContent = p ? (model ? `${short} · ${model}` : short) : "chat";
    el("modelname").title = p ? `${p.label}${model ? " · " + model : ""}` : "";
    el("dot").className = "chat-dot" + (ready ? " ok" : " warn");
    el("dot").title = ready ? "ready" : "needs a model or a key";
    send.disabled = !ready && !busy;
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
    else if (!model) el("status").innerHTML = 'pick a model in <b>settings</b>';
    else if (!hasKey(p)) el("status").innerHTML = 'add an API key in <b>settings</b>';
    else el("status").textContent = "Enter to send · Shift+Enter for a new line";
  }

  async function refreshContext() {
    try {
      const response = await api("/api/status");
      if (!response.ok) return;
      const status = await response.json();
      const mode = status.mode || "ask";
      el("context").innerHTML =
        `<span>${esc(status.model || "no model")}</span>` +
        `<span class="chat-mode ${mode}">${esc(mode)}</span>` +
        (status.dirty ? '<span class="chat-mode dirty">unsaved</span>' : "");
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
    refreshContext();
    if (hasKey(provider())) loadModels({ quiet: true });
    if (!chosenModel()) openSettings();
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
    if (!names.length) add("", "no models loaded");
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
          `<span class="chat-bad">${esc(exc.message)}</span> — type the id yourself instead`;
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

  function openSettings() {
    el("modal").hidden = false;
    el("provider").focus();
  }

  function closeSettings() {
    el("modal").hidden = true;
    saveSettings();
    input.focus();
  }

  // ---------------------------------------------------------------- messages
  function empty() {
    if (turns.length) return;
    log.innerHTML = `
      <div class="chat-empty">
        <p class="chat-empty-title">Ask about the open model</p>
        <div class="chat-starters">
          ${STARTERS.map((s) => `<button class="chat-starter">${esc(s)}</button>`).join("")}
        </div>
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
  function saveHistory() {
    try {
      sessionStorage.setItem(HISTORY, JSON.stringify(turns.slice(-HISTORY_LIMIT)));
    } catch {
      /* private mode or quota: the conversation just will not survive a reload */
    }
  }

  function restoreHistory() {
    let saved = [];
    try {
      saved = JSON.parse(sessionStorage.getItem(HISTORY) || "[]");
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
    send.disabled = false;
    send.innerHTML = I.stop;
    send.classList.add("stop");
    send.title = "Stop";
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
      const response = await postJSON(
        "/api/chat/stream",
        {
          turns,
          provider: el("provider").value,
          model: chosenModel(),
          base_url: el("baseurl").value.trim() || undefined,
          api_key: el("key").value.trim() || undefined,
          system: el("system").value.trim() || undefined,
          tools: el("tools").checked,
          temperature: el("temp").value === "" ? undefined : parseFloat(el("temp").value),
          max_tokens: el("maxtok").value === "" ? undefined : parseInt(el("maxtok").value, 10),
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
    draw(false);
    addCodeCopies(view.answer);
    for (const chip of pending.values()) chip.className = "chat-tool bad";
    const text = answer.trim();
    if (text) turns.push({ role: "assistant", text });
    saveHistory();

    const bits = [];
    if (usage) bits.push(`${usage.in ?? "?"} in / ${usage.out ?? "?"} out`);
    if (firstToken) bits.push(`${((firstToken - started) / 1000).toFixed(2)}s to first token`);
    if (usage?.out && firstToken) {
      const seconds = Math.max((performance.now() - firstToken) / 1000, 0.01);
      bits.push(`${(usage.out / seconds).toFixed(0)} tok/s`);
    }
    if (aborter.signal.aborted) bits.push("stopped");
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
    aborter = null;
    send.innerHTML = I.send;
    send.classList.remove("stop");
    send.title = "Send";
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
    const action = event.target.closest("[data-act]")?.dataset.act;
    if (action === "send") submit();
    else if (action === "settings") openSettings();
    else if (action === "close-settings") closeSettings();
    else if (action === "models") loadModels();
    else if (action === "clear") {
      turns = [];
      sessionStorage.removeItem(HISTORY);
      log.innerHTML = "";
      empty();
      input.focus();
    } else if (action === "retry" && !busy) {
      if (log.lastElementChild?.classList.contains("assistant")) log.lastElementChild.remove();
      if (turns.at(-1)?.role === "assistant") turns.pop();
      run();
    }
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

  if (!restoreHistory()) empty();
  loadProviders();
  return {
    focus: () => input.focus(),
    ask: (text) => {
      input.value = text;
      submit();
    },
    refresh: refreshContext,
  };
}
