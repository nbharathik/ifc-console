import { ChatHistoryStore, conversationId, transcriptMarkdown } from "./chat_history.js";
import { esc, md } from "./chat_markdown.js";
import { STAGES, applyEvent, decodeSSE, emptyRun, stageLabel } from "./chat_flow.js";
import { sidebarModel } from "./chat_sidebar.js";
import { TABS, formatBytes, reachSentence, workspaceModel } from "./chat_workspace.js";

/* ifc-console chat panel.
 *
 * One ES module, no dependencies, mounted either as the whole page (/chat) or
 * as a dock beside the 3D view. Everything goes through the ifc-console
 * server on this origin: it holds the provider key, runs the tool loop, and
 * streams the result back as SSE. The browser never sees a provider URL.
 */

const STORE = "ifc-console-chat";
const HISTORY_LIMIT = 40;
// Plain chat reaches every read stage but never the proposal stage: only a
// pack with the proposal block can write anything, even as a preview.
const EVERY_TOOL = STAGES.filter((stage) => stage.id !== "propose")
  .flatMap((stage) => stage.tools);

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
  blocks: svg('<rect x="2.2" y="2.2" width="4.2" height="4.2"/><rect x="9.6" y="2.2" width="4.2" height="4.2"/><rect x="2.2" y="9.6" width="4.2" height="4.2"/><path d="M9.6 11.7h4.2M11.7 9.6v4.2"/>', 15),
  history: svg('<path d="M3 3.2h10M3 8h10M3 12.8h7"/><circle cx="1.4" cy="3.2" r=".4" fill="currentColor" stroke="none"/><circle cx="1.4" cy="8" r=".4" fill="currentColor" stroke="none"/><circle cx="1.4" cy="12.8" r=".4" fill="currentColor" stroke="none"/>', 15),
  export: svg('<path d="M8 2v8M5 7l3 3 3-3M3 12.5v1h10v-1"/>', 15),
  rail: svg('<rect x="2.2" y="2.6" width="11.6" height="10.8"/><path d="M6.2 2.6v10.8"/>', 15),
  notes: svg('<rect x="3" y="2.2" width="10" height="11.6"/><path d="M5.4 5.4h5.2M5.4 8h5.2M5.4 10.6h3"/>', 15),
  trash: svg('<path d="M3.4 4.4h9.2M6.4 4.4V2.8h3.2v1.6M4.6 4.4l.6 8.8h5.6l.6-8.8"/>', 13),
  spark: svg('<path d="M8 2.2 9.3 6l3.8 1.3L9.3 8.6 8 12.4 6.7 8.6 2.9 7.3 6.7 6z"/>', 14),
  chat: svg('<path d="M2.4 3.2h11.2v7.4H7.2L4.4 13v-2.4H2.4z"/>', 15),
  workspace: svg('<rect x="2.2" y="2.6" width="11.6" height="10.8"/><path d="M9.6 2.6v10.8M9.6 6.2h4.2M9.6 9.8h4.2"/>', 15),
  file: svg('<path d="M4 2.2h5l3 3v8.6H4z"/><path d="M9 2.2v3h3"/>', 14),
  image: svg('<rect x="2.4" y="3.2" width="11.2" height="9.6"/><path d="m2.4 10.4 3-3 2.6 2.6 2.4-2.4 3.2 3.2"/><circle cx="5.6" cy="6" r=".9"/>', 14),
};

// -------------------------------------------------------------------- markup
// Three columns. The rail names the assistants and the conversations, with
// building a new one at the top because that is the point of the panel. The
// centre is the conversation. The workspace on the right answers "what is this
// thing and what can it reach" so that question stays out of the transcript.
const TEMPLATE = `
<aside class="chat-rail" data-role="rail" aria-label="Assistants and conversations">
  <div class="chat-rail-top">
    <button class="chat-rail-pin" data-act="pin-rail" type="button"
            title="Keep the sidebar open" aria-label="Keep the sidebar open"
            aria-pressed="false">${I.rail}</button>
    <span class="chat-rail-brand">Assistants</span>
  </div>

  <div class="chat-rail-create">
    <button class="chat-rail-new t-press" data-act="builder" type="button"
            aria-haspopup="dialog" aria-controls="chat-builder" aria-expanded="false"
            title="Build an assistant from capability blocks">
      <i>${I.plus}</i><span>New assistant</span>
    </button>
    <button class="chat-rail-new secondary t-press" data-act="clear" type="button"
            title="Start a new conversation">
      <i>${I.chat}</i><span>New chat</span>
    </button>
  </div>

  <div class="chat-rail-scroll" data-role="rail-scroll">
    <div data-role="rail-agents"></div>
    <div class="chat-rail-group chat-rail-conversations">
      <span class="chat-rail-label">
        Conversations
        <button class="chat-rail-mini" data-act="clear-history" type="button"
                title="Delete every saved conversation">clear</button>
      </span>
      <div data-role="rail-history"></div>
    </div>
  </div>

  <div class="chat-rail-foot">
    <button class="chat-rail-item t-press" data-act="settings" type="button"
            aria-haspopup="dialog" aria-controls="chat-settings" aria-expanded="false">
      <i>${I.gear}</i><span>Settings</span>
    </button>
  </div>
</aside>

<div class="chat-main">
  <header class="chat-head">
    <button class="chat-rail-toggle" data-act="toggle-rail" type="button"
            title="Assistants and conversations" aria-label="Assistants and conversations">${I.rail}</button>
    <button class="chat-identity t-press" data-role="identity" data-act="workspace"
            title="What this assistant can reach" aria-expanded="false"
            aria-controls="chat-workspace">
      <span class="chat-title" data-role="title">Assistant</span>
      <span class="chat-dot" data-role="dot" role="status" aria-live="polite"
            aria-label="Assistant unavailable" title=""></span>
    </button>
    <select class="chat-agent chat-sr" data-role="agent" hidden aria-label="Assistant agent"></select>
    <button class="chat-context t-press" data-act="settings"
            title="Change the provider or model"
            aria-haspopup="dialog" aria-controls="chat-settings" aria-expanded="false">
      <span class="chat-context-model" data-role="modelname">no AI model</span>
      <span class="chat-context-meta" data-role="context"></span>
    </button>
    <span class="chat-spacer"></span>
    <div class="chat-actions">
      <button class="chat-icon" data-role="export" data-act="export"
              title="Export this conversation as Markdown"
              aria-label="Export this conversation as Markdown">${I.export}</button>
      <button class="chat-icon" data-act="settings" title="Provider, model and tools"
              aria-label="Chat settings" aria-haspopup="dialog"
              aria-controls="chat-settings" aria-expanded="false">${I.gear}</button>
      <button class="chat-icon" data-role="close" title="Close the panel"
              aria-label="Close the chat panel" hidden>${I.close}</button>
    </div>
  </header>

  <div class="chat-alert t-reveal" data-role="alert" hidden role="status"></div>

  <div class="chat-log" data-role="log" role="log" aria-label="Conversation" aria-live="off"></div>

  <footer class="chat-composer">
    <div class="chat-attachments" data-role="attachments" hidden></div>
    <div class="chat-input-wrap">
      <textarea data-role="input" rows="1" placeholder="Ask about the model..." aria-label="Message"></textarea>
      <div class="chat-input-tools">
        <button class="chat-attach t-press" data-act="attach" hidden
                title="Attach a document or image" aria-label="Attach a document or image">${I.clip}</button>
        <button class="chat-attach t-press" data-act="instructions"
                title="Instructions for this assistant"
                aria-label="Instructions for this assistant">${I.notes}</button>
        <button class="chat-send t-press" data-act="send" title="Send" aria-label="Send message">${I.send}</button>
      </div>
    </div>
    <input type="file" data-role="file" hidden multiple
           accept=".md,.markdown,.txt,.pdf,.png,.jpg,.jpeg" aria-hidden="true">
    <textarea data-role="system" hidden aria-hidden="true" tabindex="-1"></textarea>
    <div class="chat-hint" data-role="status" role="status" aria-live="polite"></div>
    <div class="chat-sr" data-role="announce" role="status" aria-live="polite"></div>
  </footer>
</div>

<div class="chat-shell-scrim" data-act="close-overlays" aria-hidden="true"></div>

<aside class="chat-workspace" id="chat-workspace" data-role="workspace" hidden
       aria-label="Agent workspace">
  <header class="chat-ws-head">
    <div class="chat-ws-identity">
      <b data-role="ws-title">Assistant</b>
      <small data-role="ws-reach"></small>
    </div>
    <button class="chat-icon" data-act="close-workspace" aria-label="Close the workspace">${I.close}</button>
  </header>
  <nav class="chat-ws-tabs" data-role="ws-tabs" role="tablist"></nav>
  <div class="chat-ws-body" id="chat-ws-panel" data-role="ws-body"
       role="tabpanel" tabindex="0"></div>
</aside>

<aside class="chat-history-panel" data-role="history-panel" hidden aria-label="Conversation history">
  <header class="chat-history-head">
    <div><b>Conversations</b><small>Stored on this computer</small></div>
    <button class="chat-icon" data-act="close-history" aria-label="Close history">${I.close}</button>
  </header>
  <div class="chat-history-list" data-role="history-list"></div>
  <footer class="chat-history-foot">
    <button class="chat-btn" data-act="clear-history">Clear local history</button>
  </footer>
</aside>

<div class="chat-modal" id="chat-settings" data-role="modal" hidden>
  <div class="chat-scrim" data-act="close-settings" aria-hidden="true"></div>
  <div class="chat-dialog t-modal" role="dialog" aria-modal="true" aria-label="Chat settings"
       tabindex="-1">
    <header class="chat-dialog-head">
      <span>Settings</span>
      <button class="chat-icon" data-act="close-settings" aria-label="Close settings">${I.close}</button>
    </header>
    <div class="chat-dialog-body">
      <p class="chat-help chat-scope-note">
        These apply to every assistant. Anything specific to one assistant lives
        in its workspace.
      </p>
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
        <label class="chat-key-save">
          <input type="checkbox" data-role="savekey">
          <span>Save in the operating-system credential store</span>
        </label>
        <button class="chat-btn chat-key-remove" data-act="delete-key" type="button" hidden>
          Remove saved key
        </button>
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
          <small>Plain chat can query and analyse the open file. Your session mode still decides whether anything may change.</small>
        </span>
      </label>
      <label class="chat-toggle">
        <input type="checkbox" data-role="savehistory" checked>
        <span>
          <b>Keep conversation history locally</b>
          <small>Stores transcripts and agent thread ids in this browser. API keys are never included.</small>
        </span>
      </label>

      <details class="chat-advanced">
        <summary>Advanced</summary>
        <div class="chat-field">
          <label for="chat-baseurl">Base URL</label>
          <input id="chat-baseurl" type="text" data-role="baseurl" placeholder="provider default" spellcheck="false">
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
        <div class="chat-field">
          <label>Installed agent capabilities</label>
          <div class="chat-caps" data-role="caps"></div>
        </div>
      </details>
    </div>
    <footer class="chat-dialog-foot">
      <span class="chat-privacy" data-role="privacy">Prompts and tool results go to this provider.</span>
      <button class="chat-btn primary" data-act="close-settings">Done</button>
    </footer>
  </div>
</div>

<div class="chat-modal" id="chat-builder" data-role="builder-modal" hidden>
  <div class="chat-scrim" data-act="close-builder" aria-hidden="true"></div>
  <div class="chat-dialog chat-builder-dialog t-modal" role="dialog" aria-modal="true"
       aria-label="Build an assistant" tabindex="-1">
    <header class="chat-dialog-head">
      <span>Build an assistant</span>
      <button class="chat-icon" data-act="close-builder" aria-label="Close the builder">${I.close}</button>
    </header>
    <div class="chat-dialog-body">
      <p class="chat-builder-lead">Start from an assistant that already works, or pick blocks yourself. Runtime policy and human approval stay in force whatever you write.</p>
      <div class="chat-field">
        <label>Start from</label>
        <div class="chat-preset-row" data-role="builder-presets"></div>
      </div>
      <div class="chat-duo">
        <div class="chat-field">
          <label for="agent-title">Name</label>
          <input id="agent-title" type="text" data-role="builder-title" maxlength="80"
                 placeholder="Envelope compliance" autocomplete="off">
        </div>
        <div class="chat-field">
          <label for="agent-description">Purpose</label>
          <input id="agent-description" type="text" data-role="builder-description" maxlength="300"
                 placeholder="Check envelope evidence and measurements" autocomplete="off">
        </div>
      </div>
      <div class="chat-section">Capability blocks <span class="chat-optional" data-role="builder-count"></span></div>
      <div class="chat-block-grid" data-role="builder-blocks"></div>
      <div class="chat-field">
        <label for="agent-instructions">Instructions</label>
        <textarea id="agent-instructions" data-role="builder-instructions" rows="7" maxlength="12000"
                  placeholder="Describe the procedure, the documents that define it, and the output format..."></textarea>
        <p class="chat-help">Combined with each block's safety and evidence rules.</p>
      </div>
      <div class="chat-field">
        <label for="agent-starters">Starter prompts <span class="chat-optional">optional, one per line</span></label>
        <textarea id="agent-starters" data-role="builder-starters" rows="3"
                  placeholder="Review the selected walls"></textarea>
      </div>
      <p class="chat-builder-error" data-role="builder-error" role="alert"></p>
    </div>
    <footer class="chat-dialog-foot">
      <span class="chat-privacy">Saved in this project's .ifc-console folder.</span>
      <button class="chat-btn" data-act="close-builder">Cancel</button>
      <button class="chat-btn primary" data-act="save-builder">Create assistant</button>
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
  let workspaceReturnFocus = null;
  // Agent packs: server-hosted specialists behind the same panel. Plain chat
  // is agent "" and keeps its stateless turns; a pack keeps its thread on the
  // console, so we only remember the thread id here.
  let agents = [];
  let agentBlocks = [];
  let capabilities = null;
  let railPinned = false;
  let workspace = null;
  let workspaceTab = "overview";
  let workspaceOpen = false;
  let workspaceRequest = 0;
  let currentAgent = "";
  let referenceFiles = [];
  let pendingAttachments = [];
  let agentThreads = {};
  const historyStore = new ChatHistoryStore(window.localStorage);
  let currentConversationId = "";
  const COMPACT_SHELL_WIDTH = 680;
  const OVERLAY_SHELL_WIDTH = 1120;

  const isCompactShell = () =>
    root.classList.contains("chat-compact") ||
    root.getBoundingClientRect().width < COMPACT_SHELL_WIDTH;

  function updateRailControl() {
    const pin = root.querySelector('[data-act="pin-rail"]');
    const compact = isCompactShell();
    pin.setAttribute("aria-pressed", String(!compact && railPinned));
    pin.title = compact
      ? "Close navigation"
      : railPinned ? "Let the navigation collapse" : "Keep the navigation open";
    pin.setAttribute("aria-label", pin.title);
  }

  // The panel can be a 340px viewer dock inside a 1600px browser window.
  // Viewport media queries therefore cannot tell us whether the chat itself
  // has room for three columns. Observe the component and classify its shell.
  function syncShellLayout() {
    const width = root.getBoundingClientRect().width;
    const compact = width < COMPACT_SHELL_WIDTH;
    const overlay = width < OVERLAY_SHELL_WIDTH;
    root.classList.toggle("chat-compact", compact);
    root.classList.toggle("chat-overlay", overlay);
    root.classList.toggle("chat-wide", !overlay);
    el("workspace").setAttribute("role", overlay ? "dialog" : "complementary");
    if (overlay) el("workspace").setAttribute("aria-modal", "true");
    else el("workspace").removeAttribute("aria-modal");
    if (!compact && railPinned) root.classList.add("rail-open");
    updateRailControl();
  }

  const shellObserver = typeof ResizeObserver === "undefined"
    ? null
    : new ResizeObserver(syncShellLayout);
  shellObserver?.observe(root);
  syncShellLayout();
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
  el("savehistory").checked = settings.history;
  el("savekey").checked = settings.credentialStore;

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
      prompts: isPlainObject(saved.prompts)
        ? saved.prompts
        : (saved.system ? { chat: String(saved.system) } : {}),
      tools: saved.tools !== false,
      temp: saved.temp ?? "",
      maxtok: saved.maxtok ?? "",
      agent: typeof saved.agent === "string" ? saved.agent : "",
      history: saved.history !== false,
      credentialStore: saved.credentialStore === true,
    };
  }

  function chosenModel() {
    const select = el("model");
    return select.value === "__custom__" ? el("modelcustom").value.trim() : select.value;
  }

  const remembered = (id) => settings.byProvider[id] || {};
  const promptSlot = (agent = currentAgent) => agent || "chat";

  function saveSettings() {
    const id = el("provider").value;
    const model = chosenModel();
    const base_url = el("baseurl").value.trim();
    settings.provider = id;
    settings.byProvider[id] = { model, base_url };
    settings.prompts[promptSlot()] = el("system").value;
    settings.tools = el("tools").checked;
    settings.temp = el("temp").value;
    settings.maxtok = el("maxtok").value;
    settings.agent = currentAgent;
    settings.history = el("savehistory").checked;
    settings.credentialStore = el("savekey").checked;
    try {
      localStorage.setItem(STORE, JSON.stringify(settings));
    } catch {
      // Storage can be unavailable in private mode or full.
    }

    const key = el("key").value.trim();
    postJSON("/api/chat/select", { provider: id, model, base_url, api_key: key || undefined })
      .then(async (response) => {
        if (!response.ok || !key) return;
        const p = providers.find((row) => row.id === id);
        if (settings.credentialStore) {
          const stored = await postJSON("/api/chat/credentials", {
            provider: id,
            action: "store",
            api_key: key,
          });
          const payload = await stored.json();
          if (!stored.ok) throw new Error(payload.hint || payload.error || "Could not save key");
          if (p) p.key_from_env = "keyring";
          note("Key saved in the operating-system credential store.");
        }
        // The console has it now, so the DOM drops its only browser copy.
        if (p) p.has_key = true;
        el("key").value = "";
      })
      .catch((error) => note(error.message || "Could not save settings", true))
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
    for (const attach of root.querySelectorAll('.chat-attach[data-act="attach"]')) {
      attach.hidden = !usesFiles;
    }
    const active = pack();
    el("title").textContent = active ? active.title : "Chat";
    el("identity").title = active ? active.description : "Plain chat over the open model";
    input.placeholder = active ? `Ask ${active.title.toLowerCase()}...` : "Ask about the model...";
    send.disabled = !ready && !busy;
    el("export").hidden = turns.length === 0;
    if (!turns.length && !busy) empty();
    if (p) {
      el("note").textContent = p.key_from_env
        ? `${p.note} Key found in ${p.key_from_env}.`
        : p.note;
      el("keyfield").hidden = !p.needs_key || Boolean(p.key_from_env);
      el("keystate").textContent = p.has_key
        ? (p.key_from_env === "keyring"
            ? "Saved in the operating-system credential store. The browser cannot read it."
            : "A key is available to this console. Paste another to replace it.")
        : "Held in the running console only unless you explicitly choose secure storage.";
      root.querySelector('[data-act="delete-key"]').hidden = p.key_from_env !== "keyring";
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
    el("system").value = settings.prompts[promptSlot()] || "";
    el("tools").checked = settings.tools && payload.defaults.tools;
    el("savehistory").checked = settings.history;
    el("savekey").checked = settings.credentialStore;
    el("temp").value = settings.temp;
    el("maxtok").value = settings.maxtok;
    el("key").value = "";
    setModelOptions([], mine.model || payload.selected.model || provider()?.suggested_model || "");
    render();
    if (hasKey(provider())) loadModels({ quiet: true });
    // no model yet is not an error worth a modal on open: the empty state
    // offers the button, and the dialog would cover the panel every time.
  }

  function railButton(agent) {
    const open = document.createElement("button");
    open.className = "chat-rail-item t-press";
    open.type = "button";
    open.title = agent.description || agent.title;
    open.innerHTML = "<i></i><span></span>";
    open.querySelector("i").textContent = agent.initials;
    open.querySelector("span").textContent = agent.title;
    open.addEventListener("click", () => {
      if (agent.name !== currentAgent) switchAgent(agent.name);
      if (isCompactShell() || !railPinned) collapseRail();
    });
    if (agent.active) open.setAttribute("aria-current", "true");
    return open;
  }

  function renderRail() {
    const model = sidebarModel({
      agents,
      records: settings.history ? historyStore.list() : [],
      currentAgent,
      currentConversationId,
    });
    const host = el("rail-agents");
    host.innerHTML = "";
    for (const group of model.agentGroups) {
      const section = document.createElement("div");
      section.className = "chat-rail-group";
      const label = document.createElement("span");
      label.className = "chat-rail-label";
      label.textContent = group.label;
      section.appendChild(label);
      const list = document.createElement("div");
      list.className = "chat-rail-list";
      for (const agent of group.agents) {
        const row = document.createElement("div");
        row.className = "chat-rail-row" + (agent.active ? " active" : "");
        row.appendChild(railButton(agent));
        if (agent.deletable) {
          const remove = document.createElement("button");
          remove.className = "chat-rail-delete";
          remove.type = "button";
          remove.title = `Delete ${agent.title}`;
          remove.setAttribute("aria-label", `Delete ${agent.title}`);
          remove.innerHTML = I.trash;
          remove.addEventListener("click", (event) => {
            event.stopPropagation();
            deleteCustomAgent(agent);
          });
          row.appendChild(remove);
        }
        list.appendChild(row);
      }
      section.appendChild(list);
      host.appendChild(section);
    }
    renderRailHistory(model);
  }

  function renderRailHistory(model) {
    const list = el("rail-history");
    if (!list) return;
    const groups = (model || sidebarModel({
      agents,
      records: settings.history ? historyStore.list() : [],
      currentAgent,
      currentConversationId,
    })).conversationGroups;
    list.innerHTML = "";
    if (!groups.length) {
      const empty = document.createElement("span");
      empty.className = "chat-rail-empty";
      empty.textContent = "Nothing saved yet";
      list.appendChild(empty);
      return;
    }
    for (const group of groups) {
      const when = document.createElement("span");
      when.className = "chat-rail-when";
      when.textContent = group.label;
      list.appendChild(when);
      const rows = document.createElement("div");
      rows.className = "chat-rail-list";
      for (const record of group.records) {
        const button = document.createElement("button");
        button.className =
          "chat-rail-item chat-rail-conversation t-press" + (record.active ? " active" : "");
        button.type = "button";
        button.title = `${record.agent_title}: ${record.title}`;
        button.innerHTML = "<i></i><span></span>";
        button.querySelector("i").textContent = record.turns.length;
        button.querySelector("span").textContent = record.title;
        button.addEventListener("click", () => {
          selectHistory(record);
          if (isCompactShell() || !railPinned) collapseRail();
        });
        if (record.active) button.setAttribute("aria-current", "true");
        rows.appendChild(button);
      }
      list.appendChild(rows);
    }
  }

  // The rail opens when asked and stays put. Opening it on pointer hover made
  // it flap open every time the cursor crossed the left edge on its way
  // somewhere else, which is the opposite of calm. Keyboard focus still opens
  // it, because a keyboard user cannot see where they are otherwise.
  const expandRail = () => root.classList.add("rail-open");
  const collapseRail = () => {
    if (isCompactShell() || !railPinned) root.classList.remove("rail-open");
  };

  function toggleRail() {
    const opening = !root.classList.contains("rail-open");
    if (!opening && railPinned && !isCompactShell()) {
      railPinned = false;
      root.classList.remove("rail-pinned");
      try {
        localStorage.setItem("ifc-console-chat-rail", "");
      } catch {
        /* private mode: the rail simply starts collapsed next time */
      }
    }
    root.classList.toggle("rail-open", opening);
    updateRailControl();
  }

  function togglePin() {
    if (isCompactShell()) {
      root.classList.remove("rail-open");
      updateRailControl();
      root.querySelector('[data-act="toggle-rail"]')?.focus({ preventScroll: true });
      return;
    }
    railPinned = !railPinned;
    root.classList.toggle("rail-pinned", railPinned);
    root.classList.toggle("rail-open", railPinned);
    updateRailControl();
    try {
      localStorage.setItem("ifc-console-chat-rail", railPinned ? "pinned" : "");
    } catch {
      /* private mode: the rail simply starts collapsed next time */
    }
  }

  async function deleteCustomAgent(agent) {
    try {
      const response = await postJSON("/api/agents/custom/delete", { name: agent.name });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (currentAgent === agent.name) switchAgent("");
      await loadAgents();
      note(`${agent.title} deleted. Its blocks and tools are gone with it.`);
    } catch (exc) {
      note(`Could not delete ${agent.title}: ${exc.message || exc}`, true);
    }
  }

  async function loadCapabilities() {
    try {
      const response = await api("/api/agents/capabilities");
      if (!response.ok) return;
      capabilities = await response.json();
    } catch {
      return;
    }
    renderCapabilities();
  }

  function renderCapabilities() {
    const box = el("caps");
    const alert = el("alert");
    if (!capabilities) {
      if (box) box.textContent = "";
      alert.hidden = true;
      return;
    }
    if (box) {
      box.innerHTML = "";
      for (const capability of capabilities.capabilities || []) {
        const chip = document.createElement("span");
        chip.className = "chat-cap " + (capability.present ? "ok" : "bad");
        chip.textContent = capability.present
          ? `${capability.label} ${capability.version}`
          : `${capability.label} missing`;
        chip.title = capability.present ? capability.distribution : capability.consequence;
        box.appendChild(chip);
      }
    }
    const missing = capabilities.missing || [];
    alert.hidden = !missing.length;
    if (missing.length) {
      alert.innerHTML = "<b></b><span></span><code></code>";
      alert.querySelector("b").textContent = `${missing.join(", ")} missing from this install`;
      alert.querySelector("span").textContent =
        "These ship inside ifc-console, so this console is running from a stale environment. Uploads and page images will fail until it is repaired:";
      alert.querySelector("code").textContent = capabilities.repair || "";
    }
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
    if (Array.isArray(payload.problems) && payload.problems.length) {
      note(`Some custom agents could not be loaded: ${payload.problems.join("; ")}`, true);
    }
    const select = el("agent");
    const title = el("title");
    if (!agents.length) {
      select.hidden = true;
      title.hidden = false;
      if (currentAgent) switchAgent("");
      renderFiles();
      renderRail();
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
    title.hidden = false;
    if (wanted !== currentAgent) switchAgent(wanted);
    else {
      render();
      loadFiles();
    }
    loadBlocks();
    renderRail();
  }

  async function loadBlocks() {
    try {
      const response = await api("/api/agents/blocks");
      const payload = await response.json();
      if (!response.ok) return;
      agentBlocks = Array.isArray(payload.blocks) ? payload.blocks : [];
      renderBuilderBlocks();
      renderBuilderPresets();
      } catch {
      agentBlocks = [];
    }
  }

  function switchAgent(name) {
    if (busy) aborter?.abort();
    saveHistory();
    settings.prompts[promptSlot()] = el("system").value;
    currentAgent = name;
    el("agent").value = name;
    el("system").value = settings.prompts[promptSlot()] || "";
    turns = [];
    log.innerHTML = "";
    const latest = settings.history ? historyStore.latest(currentAgent) : null;
    currentConversationId = latest?.id || conversationId();
    const restored = restoreHistory();
    if (!restored) empty();
    saveSettings();
    loadFiles();
    pendingAttachments = [];
    renderAttachments();
    renderRail();
    workspace = null;
    loadWorkspace();
    input.focus();
  }

  function renderAttachments() {
    const tray = el("attachments");
    tray.hidden = !pendingAttachments.length;
    tray.innerHTML = "";
    for (const [index, attachment] of pendingAttachments.entries()) {
      const chip = document.createElement("span");
      chip.className = `chat-attachment-chip ${attachment.media}`;
      chip.innerHTML = `<span></span><button type="button" class="chat-attachment-remove" data-index="${index}" aria-label="Remove attachment">${I.close}</button>`;
      chip.querySelector("span").textContent = attachment.name;
      tray.appendChild(chip);
    }
  }

  function formatBytes(size) {
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${Math.round(size / 1024)} KB`;
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }

  function renderFiles(problem = "") {
    // The file view lives in the workspace now. Keep the problem visible where
    // the user is looking, and let the workspace redraw itself.
    if (problem) note(problem, true);
    if (workspaceOpen && workspace) renderWorkspace();
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
          if (payload.attachment?.path) {
            pendingAttachments.push({
              path: payload.attachment.path,
              media: payload.attachment.media || "document",
              name: file.name,
            });
            renderAttachments();
          }
        } else {
          note(
            `${file.name}: saved locally but not indexed — ${payload.error}` +
              (payload.hint ? ` ${payload.hint}` : ""),
            true,
          );
        }
        referenceFiles = Array.isArray(payload.files) ? payload.files : referenceFiles;
        renderFiles();
      } catch (exc) {
        note(`${file.name}: ${exc.message || exc}`, true);
      }
    }
    await loadFiles();
    if (workspace) await loadWorkspace({ force: true });
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

  // The instructions field is the one setting people change per question, so
  // it gets its own entry point from the composer rather than three clicks
  // through an Advanced disclosure.
  function openInstructions(trigger = document.activeElement) {
    openSettings(trigger);
    const field = el("system");
    field.scrollIntoView({ block: "center" });
    field.focus();
    field.setSelectionRange(field.value.length, field.value.length);
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

  // Starting from an assistant that already works beats starting from an
  // empty checklist: most people want "Measurement, but stricter".
  function renderBuilderPresets() {
    const row = el("builder-presets");
    if (!row) return;
    row.innerHTML = "";
    const blank = { name: "", title: "Blank", description: "Pick blocks yourself.", blocks: [] };
    for (const agent of [blank, ...agents.filter((item) => item.kind !== "custom")]) {
      const button = document.createElement("button");
      button.className = "chat-preset t-press";
      button.type = "button";
      button.title = agent.description || agent.title;
      button.innerHTML = "<b></b><small></small>";
      button.querySelector("b").textContent = agent.title;
      button.querySelector("small").textContent = agent.blocks?.length
        ? `${agent.blocks.length} blocks`
        : "no blocks";
      button.addEventListener("click", () => applyBuilderPreset(agent, button));
      row.appendChild(button);
    }
  }

  function applyBuilderPreset(agent, button) {
    for (const other of el("builder-presets").children) other.classList.remove("active");
    button.classList.add("active");
    const wanted = new Set(agent.blocks || []);
    for (const input of el("builder-blocks").querySelectorAll("input")) {
      input.checked = wanted.has(input.value);
    }
    if (agent.description && !el("builder-description").value.trim()) {
      el("builder-description").value = agent.description.slice(0, 300);
    }
    updateBuilderCount();
  }

  function updateBuilderCount() {
    const count = el("builder-blocks").querySelectorAll("input:checked").length;
    const label = el("builder-count");
    if (label) label.textContent = count ? `${count} selected` : "pick at least one";
  }

  function renderBuilderBlocks() {
    const grid = el("builder-blocks");
    grid.innerHTML = "";
    for (const block of agentBlocks) {
      const label = document.createElement("label");
      label.className = "chat-block";
      label.innerHTML = `<input type="checkbox" value="${esc(block.name)}"><span><b></b><small></small></span>`;
      label.querySelector("b").textContent = block.title;
      label.querySelector("small").textContent = block.description;
      label.querySelector("input").addEventListener("change", updateBuilderCount);
      grid.appendChild(label);
    }
    updateBuilderCount();
  }

  function openBuilder(trigger = document.activeElement) {
    settingsReturnFocus = root.contains(trigger) ? trigger : input;
    el("builder-modal").hidden = false;
    root.querySelector('[data-act="builder"]').setAttribute("aria-expanded", "true");
    el("builder-error").textContent = "";
    if (!el("builder-blocks").children.length) loadBlocks();
    else renderBuilderPresets();
    el("builder-title").focus();
  }

  function closeBuilder() {
    el("builder-modal").hidden = true;
    root.querySelector('[data-act="builder"]').setAttribute("aria-expanded", "false");
    const target = settingsReturnFocus && settingsReturnFocus.isConnected
      ? settingsReturnFocus : input;
    settingsReturnFocus = null;
    target.focus();
  }

  async function saveBuilder() {
    const title = el("builder-title").value.trim();
    const description = el("builder-description").value.trim();
    const instructions = el("builder-instructions").value.trim();
    const blocks = [...el("builder-blocks").querySelectorAll('input:checked')]
      .map((node) => node.value);
    const starters = el("builder-starters").value.split("\n")
      .map((line) => line.trim()).filter(Boolean).slice(0, 6);
    const error = el("builder-error");
    if (!title || !description || !instructions || !blocks.length) {
      error.textContent = "Add a name, purpose, instructions, and at least one capability block.";
      return;
    }
    const button = root.querySelector('[data-act="save-builder"]');
    button.disabled = true;
    button.textContent = "Creating...";
    try {
      const response = await postJSON("/api/agents/custom", {
        title, description, instructions, blocks, starters,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      settings.agent = payload.agent.name;
      closeBuilder();
      await loadAgents();
      note(`${payload.agent.title} is ready. Its tools are limited to the selected blocks.`);
    } catch (exc) {
      error.textContent = exc.message || String(exc);
    } finally {
      button.disabled = false;
      button.textContent = "Create assistant";
    }
  }


  // -------------------------------------------------------------- workspace
  // Everything about the current assistant, out of the transcript's way: what
  // it is, which blocks and tools it holds, what it may write, the files it
  // can see, and the instructions that shape it.
  async function loadWorkspace({ force = false } = {}) {
    const agent = currentAgent;
    if (!agent) {
      workspace = null;
      renderWorkspace();
      return;
    }
    if (!force && workspace && workspace.name === agent) {
      renderWorkspace();
      return;
    }
    const ticket = ++workspaceRequest;
    try {
      const query =
        `agent=${encodeURIComponent(agent)}` +
        `&instructions=${encodeURIComponent(el("system").value.trim().slice(0, 12000))}`;
      const response = await api(`/api/agents/workspace?${query}`);
      const payload = await response.json();
      if (ticket !== workspaceRequest || agent !== currentAgent) return;
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      workspace = workspaceModel(payload);
      referenceFiles = workspace.files.length ? workspace.files : referenceFiles;
    } catch (exc) {
      if (ticket !== workspaceRequest) return;
      workspace = null;
      note(`Could not read the agent workspace: ${exc.message || exc}`, true);
    }
    renderWorkspace();
  }

  function openWorkspace(trigger = document.activeElement) {
    workspaceReturnFocus = trigger instanceof HTMLElement && root.contains(trigger)
      ? trigger
      : el("identity");
    workspaceOpen = true;
    // Unhide before the class lands: a keyframe applied to a display:none
    // element never starts, and its fill state would stick.
    el("workspace").hidden = false;
    root.classList.add("workspace-open");
    for (const button of root.querySelectorAll('[data-act="workspace"]')) {
      button.setAttribute("aria-expanded", "true");
    }
    loadWorkspace();
    requestAnimationFrame(() => {
      el("workspace").querySelector(".chat-ws-tab.active, .chat-icon")?.focus();
    });
  }

  function closeWorkspace() {
    workspaceOpen = false;
    root.classList.remove("workspace-open");
    el("workspace").hidden = true;
    for (const button of root.querySelectorAll('[data-act="workspace"]')) {
      button.setAttribute("aria-expanded", "false");
    }
    const target = workspaceReturnFocus;
    workspaceReturnFocus = null;
    if (target?.isConnected) target.focus({ preventScroll: true });
    else input.focus();
  }

  function toggleWorkspace(trigger) {
    if (workspaceOpen) closeWorkspace();
    else openWorkspace(trigger);
  }

  const wsNode = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function renderWorkspaceTabs() {
    const bar = el("ws-tabs");
    bar.innerHTML = "";
    if (!workspace) return;
    for (const tab of TABS) {
      const count = workspace.counts[tab.id];
      const button = wsNode("button", "chat-ws-tab t-press");
      button.type = "button";
      button.id = `chat-ws-tab-${tab.id}`;
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(tab.id === workspaceTab));
      button.setAttribute("aria-controls", "chat-ws-panel");
      button.tabIndex = tab.id === workspaceTab ? 0 : -1;
      button.classList.toggle("active", tab.id === workspaceTab);
      button.appendChild(wsNode("span", "", tab.label));
      if (count) button.appendChild(wsNode("i", "chat-ws-count", String(count)));
      button.addEventListener("click", () => {
        workspaceTab = tab.id;
        renderWorkspace();
      });
      bar.appendChild(button);
    }
  }

  function wsOverview(body) {
    const model = workspace;
    if (model.summary) body.appendChild(wsNode("p", "chat-ws-lead", model.summary));
    else if (model.description) body.appendChild(wsNode("p", "chat-ws-lead", model.description));

    const pipeline = wsNode("div", "chat-ws-pipeline");
    for (const stage of model.stages) {
      const step = wsNode("div", "chat-ws-step" + (stage.available ? "" : " off"));
      step.appendChild(wsNode("b", "", stage.label));
      step.appendChild(wsNode("small", "", stage.hint));
      step.appendChild(
        wsNode(
          "span",
          "chat-ws-step-tools",
          stage.available ? `${stage.tools.length} tools` : "not available"
        )
      );
      pipeline.appendChild(step);
    }
    body.appendChild(wsNode("div", "chat-ws-section", "Pipeline"));
    body.appendChild(pipeline);

    body.appendChild(wsNode("div", "chat-ws-section", "Capability blocks"));
    const blocks = wsNode("div", "chat-ws-blocks");
    for (const block of model.blocks) {
      const card = wsNode("div", "chat-ws-block" + (block.available ? "" : " off"));
      card.appendChild(wsNode("b", "", block.title));
      card.appendChild(wsNode("small", "", block.description));
      card.appendChild(
        wsNode(
          "span",
          "chat-ws-block-count",
          block.available ? `${block.tools.length} tools` : "unavailable here"
        )
      );
      blocks.appendChild(card);
    }
    body.appendChild(blocks);

    if (model.examples.length) {
      body.appendChild(wsNode("div", "chat-ws-section", "Try it"));
      const list = wsNode("div", "chat-ws-examples");
      for (const example of model.examples) {
        const card = wsNode("button", "chat-ws-example t-press");
        card.type = "button";
        card.appendChild(wsNode("b", "", example.title));
        card.appendChild(wsNode("span", "", example.prompt));
        if (example.note) card.appendChild(wsNode("small", "", example.note));
        card.addEventListener("click", () => {
          input.value = example.prompt;
          grow();
          closeWorkspace();
          input.focus();
        });
        list.appendChild(card);
      }
      body.appendChild(list);
    }

    body.appendChild(wsNode("div", "chat-ws-section", "What it may change"));
    const policy = wsNode("div", "chat-ws-policy" + (model.canWriteModel ? " writes" : ""));
    policy.appendChild(
      wsNode(
        "b",
        "",
        model.canWriteModel
          ? "Can propose AI-marked changes"
          : "Read-only: cannot change the model"
      )
    );
    if (model.writePolicy) {
      policy.appendChild(wsNode("small", "", model.writePolicy.note || ""));
      if (model.canWriteModel) {
        const sets = wsNode("div", "chat-ws-psets");
        for (const name of model.writePolicy.property_sets || []) {
          sets.appendChild(wsNode("code", "", name));
        }
        policy.appendChild(sets);
      }
    }
    body.appendChild(policy);

    if (model.role) {
      const details = wsNode("details", "chat-ws-prompt");
      details.appendChild(wsNode("summary", "", "Its standing instructions"));
      details.appendChild(wsNode("pre", "", model.role));
      body.appendChild(details);
    }
  }

  function wsTools(body) {
    const model = workspace;
    body.appendChild(
      wsNode(
        "p",
        "chat-ws-lead",
        `${model.tools.length} tools, grouped by the stage they belong to. ` +
          "This is the exact list the assistant holds right now."
      )
    );
    for (const group of model.stageGroups) {
      body.appendChild(wsNode("div", "chat-ws-section", group.label));
      const list = wsNode("div", "chat-ws-tools");
      for (const tool of group.tools) {
        const row = wsNode("div", "chat-ws-tool" + (tool.writes_model ? " writes" : ""));
        const head = wsNode("div", "chat-ws-tool-head");
        head.appendChild(wsNode("code", "", tool.name));
        if (tool.writes_model) head.appendChild(wsNode("span", "chat-ws-tag write", "preview"));
        if (tool.requires_approval) {
          head.appendChild(wsNode("span", "chat-ws-tag approval", "approval"));
        }
        row.appendChild(head);
        row.appendChild(wsNode("small", "", tool.summary));
        list.appendChild(row);
      }
      body.appendChild(list);
    }
    if (model.unavailable.length) {
      body.appendChild(wsNode("div", "chat-ws-section", "Not available here"));
      const missing = wsNode("div", "chat-ws-tools");
      for (const name of model.unavailable) {
        const row = wsNode("div", "chat-ws-tool off");
        row.appendChild(wsNode("code", "", name));
        missing.appendChild(row);
      }
      body.appendChild(missing);
    }
  }

  function wsFiles(body) {
    const model = workspace;
    const groups = model.fileGroups;
    const head = wsNode("div", "chat-ws-files-head");
    head.appendChild(
      wsNode(
        "span",
        "",
        groups.total
          ? `${groups.total} file${groups.total === 1 ? "" : "s"}, ${groups.indexed} indexed`
          : "No project references yet"
      )
    );
    const add = wsNode("button", "chat-btn primary t-press", "Add files");
    add.type = "button";
    add.dataset.act = "attach";
    head.appendChild(add);
    body.appendChild(head);

    if (!groups.total) {
      body.appendChild(
        wsNode(
          "p",
          "chat-ws-lead",
          "Add a manual, a specification, a drawing, or a site photograph. " +
            "PDF pages and images are read as pixels, not just as text."
        )
      );
      return;
    }

    if (groups.images.length) {
      body.appendChild(wsNode("div", "chat-ws-section", "Images"));
      const grid = wsNode("div", "chat-ws-gallery");
      for (const file of groups.images) grid.appendChild(fileCard(file));
      body.appendChild(grid);
    }
    if (groups.documents.length) {
      body.appendChild(wsNode("div", "chat-ws-section", "Documents"));
      const list = wsNode("div", "chat-ws-filelist");
      for (const file of groups.documents) list.appendChild(fileCard(file));
      body.appendChild(list);
    }
  }

  function fileCard(file) {
    const card = wsNode("div", `chat-ws-file ${file.media} ${file.indexed ? "indexed" : "pending"}`);
    const icon = wsNode("span", "chat-ws-file-icon");
    icon.innerHTML = file.media === "image" ? I.image : I.file;
    card.appendChild(icon);
    const meta = wsNode("div", "chat-ws-file-meta");
    meta.appendChild(wsNode("b", "", file.name));
    meta.appendChild(
      wsNode(
        "small",
        "",
        `${formatBytes(file.size_bytes)} · ${file.indexed ? "indexed" : "not indexed"}`
      )
    );
    card.appendChild(meta);
    const attach = wsNode("button", "chat-ws-file-attach t-press", "Attach");
    attach.type = "button";
    attach.title = "Attach this to the next message";
    attach.addEventListener("click", () => {
      attachReference(file);
    });
    card.appendChild(attach);
    return card;
  }

  function attachReference(file) {
    if (pendingAttachments.some((item) => item.path === file.path)) return;
    pendingAttachments.push({ path: file.path, media: file.media, name: file.name });
    renderAttachments();
    note(`${file.name} will go with your next message.`);
  }

  function wsSettings(body) {
    const model = workspace;
    body.appendChild(
      wsNode(
        "p",
        "chat-ws-lead",
        "These apply to this assistant only and are stored in this browser. " +
          "Provider, model, and API key live in the main settings."
      )
    );
    const field = wsNode("div", "chat-field");
    const label = wsNode("label", "", "Standing instructions");
    label.setAttribute("for", "chat-ws-instructions");
    field.appendChild(label);
    const area = document.createElement("textarea");
    area.id = "chat-ws-instructions";
    area.rows = 8;
    area.placeholder =
      "How to calculate a property, which document defines it, the output format you want...";
    area.value = el("system").value;
    area.addEventListener("change", () => {
      el("system").value = area.value;
      saveSettings();
      loadWorkspace({ force: true });
      note("Instructions saved. The assistant restarts with them on your next message.");
    });
    field.appendChild(area);
    field.appendChild(
      wsNode(
        "p",
        "chat-help",
        "Added to this assistant's system prompt. The block safety, evidence, " +
          "and approval rules always stay above it."
      )
    );
    const reset = wsNode("button", "chat-btn t-press", "Clear instructions");
    reset.type = "button";
    reset.addEventListener("click", () => {
      area.value = "";
      el("system").value = "";
      saveSettings();
      loadWorkspace({ force: true });
      note("Standing instructions cleared for this assistant.");
    });
    field.appendChild(reset);
    body.appendChild(field);

    body.appendChild(wsNode("div", "chat-ws-section", "Limits"));
    const limits = wsNode("div", "chat-ws-limits");
    for (const [key, value] of [
      ["Tool rounds", model.limits.max_tool_rounds],
      ["Timeout", model.limits.timeout_s ? `${model.limits.timeout_s}s` : ""],
      ["Session mode", model.mode],
      ["3D viewer", model.viewer ? "connected" : "off"],
    ]) {
      if (value === undefined || value === "") continue;
      const row = wsNode("div", "chat-ws-limit");
      row.appendChild(wsNode("span", "", key));
      row.appendChild(wsNode("b", "", String(value)));
      limits.appendChild(row);
    }
    body.appendChild(limits);

    if (!model.builtin) {
      body.appendChild(wsNode("div", "chat-ws-section", "This assistant"));
      const remove = wsNode("button", "chat-btn danger t-press", "Delete this assistant");
      remove.type = "button";
      remove.addEventListener("click", () => {
        const agent = agents.find((item) => item.name === model.name);
        if (agent) deleteCustomAgent(agent);
      });
      body.appendChild(remove);
    }
  }

  function renderWorkspace() {
    const body = el("ws-body");
    renderWorkspaceTabs();
    body.innerHTML = "";
    if (!workspace) {
      el("ws-title").textContent = currentAgent ? "Loading..." : "Plain chat";
      el("ws-reach").textContent = currentAgent
        ? ""
        : "No agent selected. Pick one in the sidebar to see its workspace.";
      return;
    }
    el("ws-title").textContent = workspace.title;
    el("ws-reach").textContent = reachSentence(workspace);
    body.setAttribute("aria-labelledby", `chat-ws-tab-${workspaceTab}`);
    const draw =
      { overview: wsOverview, tools: wsTools, files: wsFiles, settings: wsSettings }[
        workspaceTab
      ] || wsOverview;
    draw(body);
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
    const ready = isReady();
    const selectedProvider = provider();
    const setupLead = !selectedProvider
      ? "Choose a provider and an AI model."
      : !chosenModel()
        ? `Choose an AI model for ${selectedProvider.label}.`
        : !hasKey(selectedProvider)
          ? `Add the API key required by ${selectedProvider.label}.`
          : "Finish the AI model setup.";
    const body = ready
      ? `<p class="chat-empty-lead">${esc(lead)}</p>
         <div class="chat-starters" aria-label="Suggested questions">
           ${starters.map((s) =>
             `<button class="chat-starter"><span>${esc(s)}</span>${I.send}</button>`
           ).join("")}
         </div>`
      : `<p class="chat-empty-lead">Connect an AI model once, then ask grounded questions about the IFC file open in the console.</p>
         <div class="chat-setup">
           <span class="chat-setup-status" aria-hidden="true"></span>
           <div><b>${esc(setupLead)}</b><small>Provider credentials stay in the running console unless you choose secure storage.</small></div>
           <button class="chat-btn primary" data-act="settings" aria-haspopup="dialog"
                   aria-controls="chat-settings" aria-expanded="false">Configure AI model</button>
         </div>`;
    log.innerHTML = `
      <div class="chat-empty">
        <span class="chat-empty-mark" aria-hidden="true">${I.workspace}</span>
        <span class="chat-empty-eyebrow">${esc(active ? active.title : "IFC assistant")}</span>
        <h1 class="chat-empty-title">${ready ? "What do you want to inspect?" : "Ask the open model"}</h1>
        ${body}
        <p class="chat-empty-note">${ready
          ? `Uses the open IFC model and respects ${esc(sessionStatus.mode || "ask")} mode.`
          : "You can change the provider or model at any time."}</p>
      </div>`;
    for (const button of log.querySelectorAll(".chat-starter")) {
      button.addEventListener("click", () => {
        input.value = button.textContent;
        grow();
        submit();
      });
    }
  }

  function addUser(text, attachments = []) {
    if (!turns.length) log.innerHTML = "";
    const div = document.createElement("div");
    div.className = "chat-msg user";
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = text;
    div.appendChild(bubble);
    if (attachments.length) {
      const row = document.createElement("div");
      row.className = "chat-user-attachments";
      row.textContent = attachments.map((item) => item.name || item.path).join(" · ");
      div.appendChild(row);
    }
    log.appendChild(div);
    scroll();
  }

  function addAssistant() {
    const div = document.createElement("div");
    div.className = "chat-msg assistant";
    div.innerHTML = `
      <div class="chat-body">
        <div class="chat-step" hidden><i></i><span></span></div>
        <details class="chat-think" hidden><summary><span data-role="tlabel">thinking</span></summary><div class="chat-think-body"></div></details>
        <details class="chat-work" hidden>
          <summary><span class="chat-work-label">Worked on it</span></summary>
          <div class="chat-tools"></div>
        </details>
        <div class="chat-proposals" hidden></div>
        <div class="chat-answer"><span class="chat-cursor"></span></div>
        <div class="chat-stats"></div>
      </div>`;
    log.appendChild(div);
    scroll();
    return {
      step: div.querySelector(".chat-step"),
      stepLabel: div.querySelector(".chat-step span"),
      work: div.querySelector(".chat-work"),
      workLabel: div.querySelector(".chat-work-label"),
      think: div.querySelector(".chat-think"),
      thinkBody: div.querySelector(".chat-think-body"),
      tlabel: div.querySelector('[data-role="tlabel"]'),
      tools: div.querySelector(".chat-tools"),
      proposals: div.querySelector(".chat-proposals"),
      answer: div.querySelector(".chat-answer"),
      stats: div.querySelector(".chat-stats"),
    };
  }

  // What the run is doing, in one line, only while it is doing it. Same five
  // stages the workspace pipeline explains, in the words a reader would use.
  const STEP_TEXT = {
    scope: "Finding the elements",
    evidence: "Reading the documents",
    method: "Measuring",
    verify: "Checking the result",
    propose: "Preparing a proposal",
  };

  function showStep(view, state) {
    // Once a tool has run, the stage is the honest description of what is
    // happening, even while the model is narrating: models routinely talk
    // before and between tool calls, so "is it emitting text" says nothing
    // about what the run is actually doing. settleWork hides this at the end.
    const stage = STAGES[state.stage];
    if (stage) {
      view.step.hidden = false;
      view.stepLabel.textContent = STEP_TEXT[stage.id] || stageLabel(state.stage);
      return;
    }
    view.step.hidden = !(state.thinking && !state.answering);
    view.stepLabel.textContent = "Thinking";
  }

  function settleWork(view, state) {
    view.step.hidden = true;
    const count = state.tools.length;
    view.work.hidden = !count;
    if (!count) return;
    const failed = state.tools.filter((tool) => tool.state === "bad").length;
    view.workLabel.textContent =
      `Used ${count} tool${count === 1 ? "" : "s"}` + (failed ? `, ${failed} failed` : "");
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
  // Transcripts live in a small reusable local archive. Provider credentials
  // are deliberately managed by a separate OS-keyring route and never enter
  // these records.
  function conversationRecord() {
    const first = turns.find((turn) => turn.role === "user")?.text || "New conversation";
    return {
      id: currentConversationId || (currentConversationId = conversationId()),
      agent: currentAgent,
      agent_title: pack()?.title || "Chat",
      title: first.replace(/\s+/g, " ").trim().slice(0, 68) || "New conversation",
      updated_at: Date.now(),
      thread_id: currentAgent ? (agentThreads[currentAgent] || "") : "",
      turns: turns.slice(-HISTORY_LIMIT),
    };
  }

  function saveHistory() {
    if (!settings.history || !turns.length) return;
    historyStore.save(conversationRecord());
    renderHistory();
    renderRailHistory();
  }

  function paintTranscript() {
    log.innerHTML = "";
    for (const turn of turns) {
      if (turn.role === "user") addUser(turn.text, turn.attachments || []);
      else addAnswer(turn.text);
    }
    scroll();
  }

  function restoreHistory() {
    if (!settings.history) return false;
    const saved = historyStore.get(currentConversationId) || historyStore.latest(currentAgent);
    if (!saved?.turns?.length) return false;
    currentConversationId = saved.id;
    turns = saved.turns;
    if (saved.agent && saved.thread_id) {
      agentThreads[saved.agent] = saved.thread_id;
      saveThreads();
    }
    paintTranscript();
    return true;
  }

  function historyDate(timestamp) {
    const date = new Date(timestamp);
    const today = new Date();
    return date.toDateString() === today.toDateString()
      ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
      : date.toLocaleDateString([], { month: "short", day: "numeric" });
  }

  function renderHistory() {
    const list = el("history-list");
    const records = historyStore.list();
    list.innerHTML = "";
    if (!records.length) {
      list.innerHTML = '<div class="chat-history-empty"><b>No saved conversations</b><span>Start a chat and it will appear here.</span></div>';
      return;
    }
    for (const record of records) {
      const row = document.createElement("div");
      row.className = "chat-history-row" + (record.id === currentConversationId ? " active" : "");
      const open = document.createElement("button");
      open.className = "chat-history-open";
      open.type = "button";
      open.innerHTML = '<b></b><span><i></i><time></time></span>';
      open.querySelector("b").textContent = record.title;
      open.querySelector("i").textContent = record.agent_title;
      open.querySelector("time").textContent = historyDate(record.updated_at);
      open.addEventListener("click", () => selectHistory(record));
      const remove = document.createElement("button");
      remove.className = "chat-history-delete";
      remove.type = "button";
      remove.title = "Delete conversation";
      remove.setAttribute("aria-label", `Delete ${record.title}`);
      remove.innerHTML = I.close;
      remove.addEventListener("click", () => removeHistory(record));
      row.append(open, remove);
      list.appendChild(row);
    }
  }

  function selectHistory(record) {
    if (busy) return;
    saveHistory();
    currentAgent = agents.some((agent) => agent.name === record.agent) ? record.agent : "";
    settings.agent = currentAgent;
    el("agent").value = currentAgent;
    el("system").value = settings.prompts[promptSlot()] || "";
    currentConversationId = record.id;
    turns = record.turns;
    if (currentAgent && record.thread_id) agentThreads[currentAgent] = record.thread_id;
    paintTranscript();
    saveSettings();
    loadFiles();
    closeHistory();
    input.focus();
  }

  async function forgetAgentThread(record) {
    if (!record.thread_id) return true;
    let removed = false;
    try {
      const response = await postJSON("/api/agents/thread/delete", {
        thread_id: record.thread_id,
      });
      removed = response.ok;
    } catch {
      removed = false;
    }
    if (record.agent && agentThreads[record.agent] === record.thread_id) {
      delete agentThreads[record.agent];
      saveThreads();
    }
    return removed;
  }

  async function removeHistory(record) {
    const threadRemoved = await forgetAgentThread(record);
    historyStore.remove(record.id);
    if (record.id === currentConversationId) startConversation(false);
    renderHistory();
    if (!threadRemoved) {
      note("The transcript was removed, but its project-local agent context could not be deleted.", true);
    }
  }

  function openHistory(trigger) {
    const panel = el("history-panel");
    renderHistory();
    panel.hidden = false;
    root.classList.add("history-open");
    root.querySelector('[data-act="history"]').setAttribute("aria-expanded", "true");
    panel.querySelector(".chat-history-open, button")?.focus();
  }

  function closeHistory() {
    el("history-panel").hidden = true;
    root.classList.remove("history-open");
    root.querySelector('[data-act="history"]').setAttribute("aria-expanded", "false");
  }

  function startConversation(saveCurrent = true) {
    if (saveCurrent) saveHistory();
    turns = [];
    currentConversationId = conversationId();
    if (currentAgent) {
      delete agentThreads[currentAgent];
      saveThreads();
    }
    log.innerHTML = "";
    empty();
    renderHistory();
    renderRailHistory();
    input.focus();
  }

  function exportConversation() {
    if (!turns.length) {
      note("There is no conversation to export yet.", true);
      return;
    }
    const record = conversationRecord();
    const source = transcriptMarkdown(record, chosenModel());
    const blob = new Blob([source], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${record.title.replace(/[^a-z0-9]+/gi, "-").replace(/^-|-$/g, "").toLowerCase() || "ifc-chat"}.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    note("Conversation exported as Markdown.");
  }

  function toolChip(box, name, args) {
    box.closest("details")?.removeAttribute("hidden");
    const chip = document.createElement("span");
    chip.className = "chat-tool running";
    chip.innerHTML = `<span class="chat-tool-name"></span><span class="chat-tool-state">running</span>`;
    chip.querySelector(".chat-tool-name").textContent = name;
    if (args) chip.title = args;
    box.appendChild(chip);
    scroll();
    return chip;
  }

  function proposalCard(box, proposal) {
    box.hidden = false;
    const card = document.createElement("section");
    card.className = "chat-proposal" + (proposal.marked ? "" : " unmarked");
    const value = proposal.value === null || proposal.value === undefined
      ? "measured value" : String(proposal.value);
    card.innerHTML = `
      <div class="chat-proposal-mark">${proposal.marked ? "AI-marked · preview only" : "preview only · provenance marker missing"}</div>
      <div class="chat-proposal-value"><b></b><span></span></div>
      <div class="chat-proposal-target"></div>
      <dl class="chat-proposal-facts"></dl>
      <p>Nothing changed in the IFC. Review and approve this revision-bound ChangeSet in the host.</p>
      <button type="button" class="chat-proposal-copy">Copy ChangeSet id</button>`;
    card.querySelector("b").textContent = proposal.property || "Measured value";
    card.querySelector(".chat-proposal-value span").textContent =
      value + (proposal.unit ? ` ${proposal.unit}` : "");
    card.querySelector(".chat-proposal-target").textContent =
      `${proposal.pset || "IfcConsole_AI_Measurements"} · ${proposal.elements || proposal.count || 1} element(s)`;
    const facts = card.querySelector(".chat-proposal-facts");
    for (const [label, text] of [
      ["Method", proposal.method],
      ["Source", proposal.source],
      ["Confidence", proposal.confidence],
      ["ChangeSet", (proposal.id || "").slice(0, 22)],
    ]) {
      if (!text) continue;
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = text;
      facts.append(term, detail);
    }
    if (proposal.warning) {
      const warning = document.createElement("p");
      warning.className = "chat-proposal-warning";
      warning.textContent = proposal.warning;
      card.appendChild(warning);
    }
    card.querySelector(".chat-proposal-copy").addEventListener("click", (event) =>
      copyText(proposal.id || "", event.currentTarget));
    box.appendChild(card);
    scroll();
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
    // One run object holds every derived fact about this turn: which stage is
    // live, which tools ran, what came back. The view reads it; nothing else
    // has to track partial state.
    const state = emptyRun();

    let answer = "";
    let reasoning = "";
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
      if (state.error) {
        const box = document.createElement("div");
        box.className = "chat-error";
        box.textContent = state.error;
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
              persist_history: settings.history,
              additional_instructions: el("system").value.trim() || undefined,
              attachments: turns.findLast((turn) => turn.role === "user")?.attachments
                ?.map((item) => item.path) || [],
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
        const { events, rest } = decodeSSE(buffer);
        buffer = rest;
        for (const event of events) {
          const stick = nearBottom();
          applyEvent(state, event);
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
          } else if (event.type === "proposal") {
            proposalCard(view.proposals, event);
          } else if (event.type === "thread") {
            agentThreads[currentAgent] = event.id;
            saveThreads();
          }
          showStep(view, state);
          if (stick) scroll();
        }
      }
    } catch (exc) {
      if (exc.name !== "AbortError") state.error = String(exc.message || exc);
    }

    clearTimeout(repaint);
    const stopped = aborter.signal.aborted;
    settleWork(view, state);
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
    const usage = state.usage;
    if (usage) bits.push(`${usage.in ?? "?"} in / ${usage.out ?? "?"} out`);
    if (firstToken) bits.push(`${((firstToken - started) / 1000).toFixed(2)}s to first token`);
    if (usage?.out && firstToken) {
      const seconds = Math.max((performance.now() - firstToken) / 1000, 0.01);
      bits.push(`${(usage.out / seconds).toFixed(0)} tok/s`);
    }
    if (stopped) bits.push("stopped");
    if (state.finishReason === "length") bits.push("hit the token cap");
    const ran = state.tools.length;
    if (ran) bits.push(`${ran} tool call${ran === 1 ? "" : "s"}`);
    view.stats.textContent = bits.join(" · ");
    if (text) view.stats.appendChild(copyButton(text));
    // an answer that never arrived is worth one button, not a retyped question
    if (state.error && !text) {
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
      : state.error && !text ? "Assistant response failed." : "Assistant response ready.";
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
    const attachments = pendingAttachments;
    pendingAttachments = [];
    renderAttachments();
    addUser(text, attachments);
    turns.push({ role: "user", text, attachments });
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
    const openModal = !el("modal").hidden ? el("modal")
      : !el("builder-modal").hidden ? el("builder-modal")
        : workspaceOpen && root.classList.contains("chat-overlay") ? el("workspace")
          : root.classList.contains("rail-open") && (isCompactShell() || !railPinned)
            ? el("rail") : null;
    if (openModal && event.key === "Tab") {
      const focusable = [...openModal.querySelectorAll(
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
    } else if (!el("builder-modal").hidden) {
      closeBuilder();
      event.stopPropagation();
    } else if (!el("history-panel").hidden) {
      closeHistory();
      event.stopPropagation();
    } else if (workspaceOpen) {
      closeWorkspace();
      event.stopPropagation();
    } else if (root.classList.contains("rail-open") && (isCompactShell() || !railPinned)) {
      root.classList.remove("rail-open");
      if (isCompactShell()) {
        root.querySelector('[data-act="toggle-rail"]')?.focus({ preventScroll: true });
      }
      event.stopPropagation();
    } else if (busy) {
      aborter?.abort();
      event.stopPropagation();
    }
  });
  root.addEventListener("click", async (event) => {
    const actionButton = event.target.closest("[data-act]");
    const action = actionButton?.dataset.act;
    if (action === "send") submit();
    else if (action === "workspace") toggleWorkspace(actionButton);
    else if (action === "close-workspace") closeWorkspace();
    else if (action === "close-overlays") {
      root.classList.remove("rail-open");
      if (workspaceOpen) closeWorkspace();
      else if (isCompactShell()) {
        root.querySelector('[data-act="toggle-rail"]')?.focus({ preventScroll: true });
      } else input.focus();
    }
    else if (action === "toggle-rail") toggleRail();
    else if (action === "pin-rail") togglePin();
    else if (action === "instructions") openInstructions(actionButton);
    else if (action === "settings") openSettings(actionButton);
    else if (action === "history") openHistory(actionButton);
    else if (action === "close-history") closeHistory();
    else if (action === "export") exportConversation();
    else if (action === "builder") openBuilder(actionButton);
    else if (action === "close-settings") closeSettings();
    else if (action === "close-builder") closeBuilder();
    else if (action === "save-builder") saveBuilder();
    else if (action === "models") loadModels();
    else if (action === "delete-key") {
      const id = el("provider").value;
      const response = await postJSON("/api/chat/credentials", { provider: id, action: "delete" });
      const payload = await response.json();
      if (!response.ok) note(payload.hint || payload.error || "Could not remove key", true);
      else {
        const p = providers.find((row) => row.id === id);
        if (p) {
          p.has_key = false;
          p.key_from_env = null;
        }
        note("Saved key removed from the operating-system credential store.");
        render();
      }
    }
    else if (action === "attach") el("file").click();
    else if (action === "clear") {
      startConversation();
    } else if (action === "clear-history") {
      const records = historyStore.list();
      const deleted = await Promise.all(records.map((record) => forgetAgentThread(record)));
      historyStore.clear();
      startConversation(false);
      closeHistory();
      note(
        deleted.every(Boolean)
          ? "Local conversation history cleared."
          : "Browser history cleared; some project-local agent context could not be deleted.",
        !deleted.every(Boolean),
      );
    } else if (action === "retry" && !busy) {
      if (log.lastElementChild?.classList.contains("assistant")) log.lastElementChild.remove();
      if (turns.at(-1)?.role === "assistant") turns.pop();
      run();
    }
    const remove = event.target.closest(".chat-attachment-remove");
    if (remove) {
      pendingAttachments.splice(Number(remove.dataset.index), 1);
      renderAttachments();
    }
  });
  el("agent").addEventListener("change", () => switchAgent(el("agent").value));
  el("ws-tabs").addEventListener("keydown", (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...el("ws-tabs").querySelectorAll('[role="tab"]')];
    if (!tabs.length) return;
    event.preventDefault();
    const current = Math.max(0, tabs.indexOf(document.activeElement));
    const next = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    tabs[next].click();
  });
  const rail = el("rail");
  rail.addEventListener("focusin", expandRail);
  rail.addEventListener("focusout", (event) => {
    if (!rail.contains(event.relatedTarget)) collapseRail();
  });

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
  for (const role of ["modelcustom", "baseurl", "system", "tools", "savehistory", "savekey", "temp", "maxtok", "key"]) {
    el(role).addEventListener("change", saveSettings);
  }
  el("system").addEventListener("change", () => loadWorkspace({ force: true }));

  try {
    railPinned = localStorage.getItem("ifc-console-chat-rail") === "pinned";
  } catch {
    railPinned = false;
  }
  if (railPinned) {
    root.classList.add("rail-pinned", "rail-open");
  }
  syncShellLayout();

  const urlAgent = new URLSearchParams(location.search).get("agent");
  const openBuilderFromUrl = new URLSearchParams(location.search).get("builder") === "1";
  if (urlAgent) settings.agent = urlAgent;
  currentAgent = settings.agent || "";
  currentConversationId = historyStore.latest(currentAgent)?.id || conversationId();
  if (!restoreHistory()) empty();
  renderHistory();
  renderRail();
  loadWorkspace();
  refreshContext();
  loadProviders();
  loadCapabilities();
  loadAgents().then(() => {
    if (openBuilderFromUrl) openBuilder(root.querySelector('[data-act="builder"]'));
  });
  return {
    focus: () => input.focus(),
    ask: (text) => {
      input.value = text;
      submit();
    },
    refresh: refreshContext,
  };
}
