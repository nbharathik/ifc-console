import {
  ChatHistoryStore,
  approvalArgumentPreview,
  approvalDigest,
  boundedChatTurns,
  carrySlice,
  conversationId,
  transcriptMarkdown,
} from "./chat_history.js";
import { esc, md } from "./chat_markdown.js";
import {
  agentChatRequest,
  decodeIfcSSE,
  normalizeIfcProposal,
  plainChatRequest,
} from "./chat_ai_sdk.js";
import {
  STAGES,
  applyEvent,
  composerIntent,
  duration,
  emptyRun,
  globalIdPattern,
  globalIdsIn,
  pretty,
  settleRun,
  stageLabel,
  toolHeadline,
  transcriptBlocks,
} from "./chat_flow.js";
import { PLAIN_CHAT, initials as initialsOf, sidebarModel } from "./chat_sidebar.js";
import {
  StudioDraftStore,
  createStudioDraft,
  normalizeStudioDraft,
  reorderSelectedBlocks,
  studioModel,
  studioPayload,
} from "./chat_studio.js";
import { formatBytes, reachSentence, workspaceModel } from "./chat_workspace.js";

/* ifc-console chat panel.
 *
 * One ES module, no dependencies, mounted either as the whole page (/chat) or
 * as a dock beside the 3D view. Everything goes through the ifc-console
 * server on this origin: it holds the provider key, runs the tool loop, and
 * streams the result back as SSE. The browser never sees a provider URL.
 *
 * Three surfaces, one job each: the sidebar navigates, the conversation stays
 * in place, and one Agent workspace control opens assistant context, content,
 * provider configuration, and focused assistant setup.
 */

const STORE = "ifc-console-chat";
const HISTORY_LIMIT = 40;
const SIDE_STORE = "ifc-console-chat-side";
const THREAD_STORE = "ifc-console-agent-threads";
const HISTORY_RESET = "ifc-console-chat-history-reset-v3";
// Below these component widths a panel becomes an overlay rather than a
// column. Component width, not viewport width: the same panel is a 340px dock
// inside a 1600px browser window.
const SIDE_INLINE_WIDTH = 660;
const MEDIUM_WIDTH = 760;
const COMPACT_WIDTH = 520;
const SIDE_WIDTH = 224;
const COMPACT_MAIN_WIDTH = 380;
const MEDIUM_MAIN_WIDTH = 500;
const PROMPT_LIMIT = 100_000;
// Kept in step with what the panel stores per turn, so the block list it
// archives and the one it re-decorates are the same list.
const TRANSCRIPT_BLOCKS = 60;
const UI_THEME_IDS = ["light", "dark", "modern", "blue"];

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
// `data-size` and not a style attribute: the panel's CSP forbids inline
// styles, so a style attribute here is dropped and every icon silently
// falls back to one size. chat.css matches the attribute instead.
const svg = (body, size = 16) =>
  `<svg viewBox="0 0 16 16" width="${size}" height="${size}" data-size="${size}" fill="none" stroke="currentColor" ` +
  `stroke-width="1.4" stroke-linecap="square" aria-hidden="true">${body}</svg>`;

const I = {
  send: svg('<path d="M8 13.2V3.4M8 3.4 4.2 7.2M8 3.4l3.8 3.8"/>', 15),
  clip: svg(
    '<path d="M10.8 6.2 6.9 10a1.6 1.6 0 0 1-2.3-2.3l4.6-4.6a2.6 2.6 0 0 1 3.7 3.7l-4.9 4.9a3.7 3.7 0 0 1-5.2-5.2l4-4"/>',
    15,
  ),
  stop: '<svg viewBox="0 0 16 16" width="15" height="15" data-size="15" fill="currentColor" aria-hidden="true"><rect x="5" y="5" width="6" height="6"/></svg>',
  plus: svg('<path d="M8 3.2v9.6M3.2 8h9.6"/>', 15),
  close: svg('<path d="m4.5 4.5 7 7M11.5 4.5l-7 7"/>', 14),
  refresh: svg(
    '<path d="M13.1 8a5.1 5.1 0 1 1-1.6-3.7" stroke-linecap="round"/><path d="M13.3 2.5v2.9h-2.9"/>',
    13,
  ),
  export: svg('<path d="M8 2v8M5 7l3 3 3-3M3 12.5v1h10v-1"/>', 15),
  side: svg('<rect x="2.2" y="2.6" width="11.6" height="10.8"/><path d="M6.2 2.6v10.8"/>', 15),
  notes: svg('<rect x="3" y="2.2" width="10" height="11.6"/><path d="M5.4 5.4h5.2M5.4 8h5.2M5.4 10.6h3"/>', 15),
  trash: svg('<path d="M3.4 4.4h9.2M6.4 4.4V2.8h3.2v1.6M4.6 4.4l.6 8.8h5.6l.6-8.8"/>', 13),
  chat: svg('<path d="M2.4 3.2h11.2v7.4H7.2L4.4 13v-2.4H2.4z"/>', 15),
  workspace: svg('<rect x="2.2" y="2.6" width="11.6" height="10.8"/><path d="M9.6 2.6v10.8M9.6 6.2h4.2M9.6 9.8h4.2"/>', 15),
  agent: svg('<circle cx="8" cy="5.1" r="2.2"/><path d="M3.5 13c.4-2.4 2-3.7 4.5-3.7s4.1 1.3 4.5 3.7"/>', 15),
  pipeline: svg('<circle cx="4" cy="3.4" r="1.3"/><circle cx="12" cy="8" r="1.3"/><circle cx="4" cy="12.6" r="1.3"/><path d="M5.3 3.4h2.2c2.6 0 2.6 4.6 3.2 4.6M10.7 8c-.6 0-.6 4.6-3.2 4.6H5.3"/>', 15),
  capability: svg('<path d="M8 1.8 13 4.6v6.8L8 14.2 3 11.4V4.6z"/><path d="m5.8 8 1.4 1.4 3-3"/>', 15),
  tools: svg('<path d="M9.8 2.5a3.2 3.2 0 0 0 3.7 4.1l-6.9 6.9-2.1-2.1 6.9-6.9a3.2 3.2 0 0 0-4.1-3.7l1.8 1.8-1.7 1.7-1.8-1.8"/>', 15),
  model: svg('<path d="M3 4.2 8 1.8l5 2.4-5 2.4zM3 8l5 2.4L13 8M3 11.8 8 14.2l5-2.4"/>', 15),
  app: svg('<rect x="2.5" y="2.5" width="4.2" height="4.2"/><rect x="9.3" y="2.5" width="4.2" height="4.2"/><rect x="2.5" y="9.3" width="4.2" height="4.2"/><rect x="9.3" y="9.3" width="4.2" height="4.2"/>', 15),
  file: svg('<path d="M4 2.2h5l3 3v8.6H4z"/><path d="M9 2.2v3h3"/>', 14),
  skill: svg('<path d="M4.2 2.5h7.6v11l-3.8-2.4-3.8 2.4z"/>', 15),
  image: svg('<rect x="2.4" y="3.2" width="11.2" height="9.6"/><path d="m2.4 10.4 3-3 2.6 2.6 2.4-2.4 3.2 3.2"/><circle cx="5.6" cy="6" r=".9"/>', 14),
  camera: svg('<path d="M2.2 5.2h2.1l1-1.6h5.4l1 1.6h2.1v7.1H2.2z"/><circle cx="8" cy="8.7" r="2.2"/>', 14),
  cube: svg('<path d="m8 2 5 2.7v6.4L8 14l-5-2.9V4.7zM3 4.7 8 7.5l5-2.8M8 7.5V14"/>', 14),
  search: svg('<circle cx="7" cy="7" r="4.2"/><path d="m10.2 10.2 3 3"/>', 14),
  chevron: svg('<path d="m6 3.6 4.4 4.4L6 12.4"/>', 12),
  user: svg('<circle cx="8" cy="5.2" r="2.3"/><path d="M3.6 13c.3-2.5 2-3.8 4.4-3.8s4.1 1.3 4.4 3.8"/>', 15),
  up: svg('<path d="M8 12.6V3.8M8 3.8 4.6 7.2M8 3.8l3.4 3.4"/>', 13),
  down: svg('<path d="M8 3.4v8.8M8 12.2 4.6 8.8M8 12.2l3.4-3.4"/>', 13),
};

// -------------------------------------------------------------------- markup
// Three regions: the sidebar names assistants and conversations, the centre is
// the conversation, and Agent workspace consolidates configuration and reach.
// Each has exactly one control that opens it.
const TEMPLATE = `
<aside class="chat-side" data-role="side" aria-label="Assistants and conversations">
  <div class="chat-side-top">
    <!-- The two groups below label themselves; a third heading here just
         repeated one of them above the other one's list. -->
    <span class="chat-side-brand" data-role="side-scope"></span>
    <button class="chat-icon" data-act="toggle-side" type="button"
            title="Hide the sidebar" aria-label="Hide the sidebar">${I.close}</button>
  </div>

  <div class="chat-side-create">
    <button class="chat-side-new t-press" data-act="clear" type="button"
            title="Start a new conversation">
      <i>${I.plus}</i><span>New chat</span>
    </button>
  </div>

  <div class="chat-side-scroll" data-role="side-scroll">
    <div data-role="side-agents"></div>
    <div class="chat-side-group chat-side-conversations">
      <span class="chat-side-label">
        Conversations
        <span class="chat-side-count" data-role="history-count">0</span>
      </span>
      <div data-role="side-history"></div>
    </div>
  </div>

  <div class="chat-side-foot">
    <button class="chat-side-item chat-side-build t-press" data-act="workspace" type="button"
            aria-controls="chat-workspace" aria-expanded="false"
            title="Agents, content, models, and appearance">
      <i>${I.workspace}</i><span>Agent workspace</span>
    </button>
  </div>
</aside>

<div class="chat-main">
  <header class="chat-head">
    <button class="chat-icon chat-side-toggle" data-act="toggle-side" type="button"
            title="Assistants and conversations" aria-expanded="false"
            aria-label="Assistants and conversations">${I.side}</button>
    <div class="chat-identity" data-role="identity">
      <span class="chat-avatar" data-role="avatar" aria-hidden="true">C</span>
      <span class="chat-identity-text">
        <span class="chat-title" data-role="title">Assistant</span>
        <span class="chat-subtitle" data-role="reach" hidden></span>
      </span>
    </div>
    <span class="chat-spacer"></span>
    <div class="chat-actions">
      <button class="chat-icon chat-model-setup-toggle t-press" data-act="settings" type="button"
              title="Model setup" aria-label="Open model setup in Agent workspace"
              aria-expanded="false" aria-controls="chat-workspace">${I.model}</button>
      <button class="chat-icon chat-workspace-toggle t-press" data-act="workspace" type="button"
              title="Agent settings" aria-label="Open agent settings"
              aria-expanded="false" aria-controls="chat-workspace">${I.workspace}</button>
      <button class="chat-icon" data-role="export" data-act="export"
              title="Export this conversation as Markdown"
              aria-label="Export this conversation as Markdown">${I.export}</button>
      <button class="chat-icon" data-role="close" title="Close the panel"
              aria-label="Close the chat panel" hidden>${I.close}</button>
    </div>
  </header>

  <div class="chat-alert t-reveal" data-role="alert" hidden role="status"></div>

  <div class="chat-log" data-role="log" role="log" aria-label="Conversation" aria-live="off"></div>

  <footer class="chat-composer">
    <div class="chat-attachments" data-role="attachments" hidden></div>
    <div class="chat-input-wrap">
      <textarea data-role="input" rows="1" maxlength="100000"
                placeholder="Ask about the model..." aria-label="Message"></textarea>
      <div class="chat-input-toolbar">
        <div class="chat-context-rail" aria-label="AI and IFC context">
          <button class="chat-attach chat-plus t-press" data-act="plus" type="button"
                  aria-haspopup="menu" aria-expanded="false" aria-controls="chat-plus-menu"
                  title="Add context to this message"
                  aria-label="Add context to this message">${I.plus}</button>
          <button class="chat-composer-pill chat-model-pill t-press" data-act="settings" type="button"
                  title="Choose AI model" aria-label="Choose AI model in Agent workspace"
                  aria-controls="chat-workspace" aria-expanded="false">
            <span data-role="modelname">no AI model</span>
          </button>
          <label class="chat-composer-select" data-role="ifcmodel-wrap" hidden>
            <span class="chat-sr">IFC model</span>
            ${I.cube}<select data-role="ifcmodel" aria-label="IFC model to view"></select>
          </label>
          <label class="chat-composer-select chat-mode-select">
            <span class="chat-sr">What the assistant may change</span>
            <select data-role="session-mode" aria-label="What the assistant may change">
              <option value="ask">Ask</option>
              <option value="edit">Edit</option>
            </select>
          </label>
          <label class="chat-composer-select chat-autonomy-select">
            <span class="chat-sr">Whether the assistant asks first</span>
            <select data-role="session-autonomy" aria-label="Whether the assistant asks first">
              <option value="approval">Approval</option>
              <option value="auto">Auto</option>
            </select>
          </label>
        </div>
        <div class="chat-plus-menu" id="chat-plus-menu" data-role="plus-menu" role="menu" hidden></div>
        <div class="chat-suggest" id="chat-suggest" data-role="suggest" role="listbox"
             aria-label="Message suggestions" hidden></div>
        <!-- Save is the one action no assistant can take, so it never scrolls
             off the end of the context rail: it sits beside Send. -->
        <button class="chat-composer-pill chat-save-pill t-press" data-act="save-model"
                type="button" hidden
                title="Write the in-memory changes to the IFC file">
          <span data-role="save-label">Save</span>
        </button>
        <button class="chat-send t-press" data-act="send" title="Send" aria-label="Send message">${I.send}</button>
      </div>
      <!-- Scrolling back through a long run used to be one-way. This rides on
           the composer's own positioning context, as the popovers do. -->
      <button class="chat-jump t-press" data-role="jump" data-act="jump" type="button" hidden
              title="Jump to the latest message" aria-label="Jump to the latest message">${I.down}</button>
    </div>
    <input type="file" data-role="file" hidden multiple
           accept=".md,.markdown,.txt,.pdf,.png,.jpg,.jpeg" aria-hidden="true">
    <textarea data-role="system" hidden aria-hidden="true" tabindex="-1"></textarea>
    <div class="chat-hint" data-role="hint" role="status" aria-live="polite"></div>
    <div class="chat-sr" data-role="announce" role="status" aria-live="polite"></div>
  </footer>
</div>

<div class="chat-shell-scrim" data-act="close-overlays" aria-hidden="true"></div>

<dialog class="chat-workspace" id="chat-workspace" data-role="workspace"
        aria-labelledby="chat-workspace-label" aria-describedby="chat-workspace-context">
  <header class="chat-ws-head">
    <span class="chat-ws-avatar" data-role="ws-avatar" aria-hidden="true">IFC</span>
    <div class="chat-ws-identity">
      <span id="chat-workspace-label">Agent workspace</span>
      <b data-role="ws-title">Agents and model context</b>
      <small id="chat-workspace-context" data-role="ws-reach"></small>
    </div>
    <button class="chat-icon" data-act="close-workspace" aria-label="Close Agent workspace">${I.close}</button>
  </header>
  <div class="chat-ws-shell">
  <nav class="chat-workspace-nav" data-role="workspace-nav" aria-label="Agent workspace sections"
       role="tablist" aria-orientation="vertical">
    <span class="chat-workspace-nav-label">Assistant</span>
    <button class="active" id="chat-workspace-tab-agent" data-workspace-view="agent" type="button"
            role="tab" aria-selected="true" title="Choose and inspect assistants"
            aria-controls="chat-workspace-panel">${I.agent}<span>Agents</span></button>
    <div class="chat-workspace-agents" data-role="workspace-agents" aria-label="Available assistants"></div>
    <button id="chat-workspace-tab-capabilities" data-workspace-view="capabilities" type="button"
            role="tab" aria-selected="false" title="Inspect capability blocks"
            aria-controls="chat-workspace-panel">${I.capability}<span>Capabilities</span></button>
    <button id="chat-workspace-tab-tools" data-workspace-view="tools" type="button"
            role="tab" aria-selected="false" title="Inspect tools and their arguments"
            aria-controls="chat-workspace-panel">${I.tools}<span>Tools</span></button>
    <button id="chat-workspace-tab-content" data-workspace-view="content" type="button"
            role="tab" aria-selected="false" title="Manage shared project content"
            aria-controls="chat-content-panel">${I.file}<span>Content</span></button>
    <button id="chat-workspace-tab-skills" data-workspace-view="skills" type="button"
            role="tab" aria-selected="false" title="Saved measurement procedures agents can reuse"
            aria-controls="chat-workspace-panel">${I.skill}<span>Skills</span></button>
    <span class="chat-workspace-nav-label system">Workspace</span>
    <button id="chat-workspace-tab-models" data-workspace-view="models" type="button"
            role="tab" aria-selected="false" title="Configure providers and models"
            aria-controls="chat-settings">${I.model}<span>Models</span></button>
    <button id="chat-workspace-tab-app" data-workspace-view="app" type="button"
            role="tab" aria-selected="false" title="Configure appearance, history, and system health"
            aria-controls="chat-settings">${I.app}<span>App</span></button>
  </nav>

  <div class="chat-ws-content">
    <section class="chat-workspace-pane" id="chat-workspace-panel" data-role="workspace-pane" role="tabpanel">
      <div class="chat-ws-body" id="chat-ws-panel" data-role="ws-body" tabindex="0"></div>
      <footer class="chat-ws-foot" data-role="workspace-foot">
        <span>Project-local · policy constrained</span>
        <button class="chat-btn t-press" data-act="builder" type="button">New assistant</button>
        <button class="chat-btn primary t-press" data-act="studio-current" type="button">Edit agent</button>
      </footer>
    </section>

    <section class="chat-workspace-pane chat-content-pane" id="chat-content-panel" data-role="content-pane"
             role="tabpanel" hidden>
      <div class="chat-ws-body" data-role="content-body" tabindex="0"></div>
      <input type="file" data-role="content-file" hidden multiple
             accept=".md,.markdown,.txt,.pdf,.png,.jpg,.jpeg" aria-hidden="true">
    </section>

<section class="chat-workspace-pane chat-settings-pane" id="chat-settings" data-role="modal"
         role="tabpanel" hidden>
  <div class="chat-dialog" role="region" aria-label="AI and app settings" tabindex="-1">
    <div class="chat-dialog-body">
      <div class="chat-settings-view" data-role="settings-models">
      <header class="chat-dialog-head">
        <span>Models</span>
        <small>Provider, credentials, and generation controls shared by every assistant.</small>
      </header>
      <div class="chat-section">AI model</div>
      <div class="chat-field">
        <label for="chat-provider">Provider</label>
        <select id="chat-provider" data-role="provider"></select>
        <p class="chat-help" data-role="note"></p>
      </div>

      <div class="chat-field" data-role="keyfield">
        <div class="chat-field-label">
          <label for="chat-key">API key</label>
          <button class="chat-text-action" data-act="toggle-key" type="button">Show typed key</button>
        </div>
        <input id="chat-key" type="password" data-role="key" placeholder="paste a new key to use or replace"
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
        <label for="chat-model">Model</label>
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

      <details class="chat-advanced">
        <summary>Advanced model controls</summary>
        <div class="chat-duo">
          <div class="chat-field">
            <label for="chat-tool-capability">Tool calling</label>
            <select id="chat-tool-capability" data-role="toolcap">
              <option value="auto">Auto-detect</option>
              <option value="supported">Supported</option>
              <option value="unsupported">Not supported</option>
            </select>
          </div>
          <div class="chat-field">
            <label for="chat-vision-capability">Image input</label>
            <select id="chat-vision-capability" data-role="visioncap">
              <option value="auto">Auto-detect</option>
              <option value="supported">Supported</option>
              <option value="unsupported">Not supported</option>
            </select>
          </div>
        </div>
        <p class="chat-help chat-capability-state" data-role="capstate"></p>
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
      </details>
      </div>

      <div class="chat-settings-view" data-role="settings-app" hidden>
      <header class="chat-dialog-head">
        <span>App</span>
        <small>Appearance, local history, and system health.</small>
      </header>
      <div class="chat-section">Conversation history</div>
      <label class="chat-toggle">
        <input type="checkbox" data-role="savehistory" checked>
        <span>
          <b>Keep conversation history locally</b>
          <small>Stores transcripts and agent thread ids in this browser. Changing this starts a fresh conversation. API keys are never included.</small>
        </span>
      </label>
      <div class="chat-history-manage" role="group" aria-label="Saved conversation controls">
        <div class="chat-history-manage-copy">
          <b>Saved conversations</b>
          <small data-role="history-state">Nothing saved in this browser.</small>
        </div>
        <button class="chat-btn danger" data-act="request-clear-history" type="button">
          Delete all
        </button>
        <div class="chat-history-confirm" data-role="history-confirm" hidden>
          <p>This permanently deletes every saved transcript and its project-local assistant context.</p>
          <div>
            <button class="chat-btn" data-act="cancel-clear-history" type="button">Cancel</button>
            <button class="chat-btn danger" data-act="confirm-clear-history" type="button">Delete conversations</button>
          </div>
        </div>
      </div>

      <div class="chat-section">Appearance</div>
      <div class="chat-field">
        <label for="chat-theme">Theme</label>
        <select id="chat-theme" data-role="theme">
          <option value="light">Light</option>
          <option value="dark">Dark</option>
          <option value="modern">Modern Dark</option>
          <option value="blue">Default Blue</option>
        </select>
        <p class="chat-help">Applies to the viewer, chat, and Agent workspace.</p>
      </div>

      <details class="chat-advanced" open>
        <summary>System health</summary>
        <div class="chat-field">
          <label>Optional runtime features</label>
          <div class="chat-caps" data-role="caps"></div>
        </div>
      </details>
      </div>
    </div>
    <footer class="chat-dialog-foot">
      <span class="chat-privacy" data-role="privacy">Prompts and tool results go to this provider.</span>
      <button class="chat-btn primary" data-act="close-settings">Save settings</button>
    </footer>
  </div>
</section>

<section class="chat-workspace-pane chat-studio" id="chat-builder" data-role="builder-modal" hidden
         aria-label="Agent setup" tabindex="-1">
  <header class="chat-studio-head">
    <button class="chat-studio-back" data-act="close-builder" type="button">${I.chevron}<span>Agents</span></button>
    <div class="chat-studio-brand">
      <span class="chat-studio-mark">IFC</span>
      <span><small>Assistant</small><b>Agent setup</b></span>
    </div>
    <span class="chat-studio-draft" data-role="studio-draft-status">New assistant</span>
  </header>

  <div class="chat-studio-editor" role="region" aria-label="Agent setup fields" tabindex="0">
    <section class="chat-studio-panel active">
      <span class="chat-studio-eyebrow">Focused assistant</span>
      <h2 tabindex="-1">Set up one clear IFC job.</h2>
      <p class="chat-studio-lead">Start from a specialist, choose what it may use, and give it a concise operating method.</p>

      <div class="chat-field">
        <label>Start from</label>
        <div class="chat-preset-row" data-role="builder-presets"></div>
      </div>
      <div class="chat-duo chat-studio-duo">
        <div class="chat-field">
          <label for="agent-title">Name</label>
          <input id="agent-title" type="text" data-role="builder-title" maxlength="80"
                 placeholder="Envelope compliance" autocomplete="off">
        </div>
        <div class="chat-field">
          <label for="agent-description">Purpose</label>
          <textarea id="agent-description" data-role="builder-description" rows="2" maxlength="300"
                    placeholder="Check envelope evidence and measurements"></textarea>
        </div>
      </div>

      <details class="chat-studio-capabilities" data-role="studio-capabilities">
        <summary>
          <span>Capabilities</span><b data-role="builder-count">Pick at least one</b>
        </summary>
        <div class="chat-block-grid" data-role="builder-blocks"></div>
      </details>

      <div class="chat-field chat-studio-instructions">
        <label for="agent-instructions">Instructions</label>
        <textarea id="agent-instructions" data-role="builder-instructions" rows="6" maxlength="12000"
                  placeholder="Describe the method, evidence rules, checks, and output format..."></textarea>
        <p class="chat-help">Capability safety rules and session approval still apply.</p>
      </div>

      <details class="chat-studio-advanced">
        <summary>Advanced run controls</summary>
        <div class="chat-studio-runtime">
          <div class="chat-field">
            <label for="agent-strategy">Strategy</label>
            <select id="agent-strategy" data-role="builder-strategy">
              <option value="adaptive">Adaptive</option>
              <option value="evidence-first">Evidence first</option>
              <option value="fast-scan">Fast scan</option>
            </select>
          </div>
          <div class="chat-field">
            <label for="agent-rounds">Tool rounds</label>
            <input id="agent-rounds" type="number" data-role="builder-rounds" min="1" max="100" value="12">
          </div>
          <div class="chat-field">
            <label for="agent-calls">Tool calls</label>
            <input id="agent-calls" type="number" data-role="builder-calls" min="1" max="1000" value="48">
          </div>
        </div>
        <div class="chat-field">
          <label for="agent-starters">Starter prompts <span class="chat-optional">one per line</span></label>
          <textarea id="agent-starters" data-role="builder-starters" rows="3"
                    placeholder="Review the selected walls"></textarea>
        </div>
      </details>

      <div class="chat-studio-guardrail">
        <span>${I.notes}</span>
        <div><b>Reviewable by construction</b><small>Writes remain AI-marked previews and still require human approval.</small></div>
      </div>
      <p class="chat-builder-error" data-role="builder-error" role="alert"></p>
    </section>
  </div>

  <footer class="chat-studio-foot">
    <div class="chat-studio-summary" aria-label="Assistant reach">
      <span><b data-role="studio-block-count">0</b> capabilities</span>
      <span><b data-role="studio-tool-count">0</b> tools</span>
      <span><b data-role="studio-stage-count">0</b> stages</span>
    </div>
    <span class="chat-spacer"></span>
    <button class="chat-btn" data-act="close-builder">Cancel</button>
    <button class="chat-btn primary" data-act="save-builder">Create assistant</button>
  </footer>
</section>
  </div>
  </div>
</dialog>
`;

const STARTERS = [
  "What is in this model?",
  "Which walls have no fire rating?",
  "Quantities by storey",
  "Check the model and list the worst problems",
];

// What the run is doing, in one line, only while it is doing it. Same five
// stages the workspace pipeline explains, in the words a reader would use.
const STEP_TEXT = {
  scope: "Finding the elements",
  evidence: "Reading the documents",
  method: "Measuring",
  verify: "Checking the result",
  propose: "Preparing a proposal",
};

/* Write `text` into `host`, with every GlobalId in it as a live chip.
 *
 * Tool output is untrusted, so the ids become real nodes around real text
 * nodes: nothing here goes near innerHTML. The chips carry the same class the
 * markdown renderer uses, so the one delegated handler on the log serves both.
 */
function guidChipsInto(host, text) {
  const source = String(text ?? "");
  let at = 0;
  for (const match of source.matchAll(globalIdPattern())) {
    const guid = match[2];
    const start = match.index + match[1].length;
    if (start > at) host.append(source.slice(at, start));
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chat-guid";
    chip.dataset.guid = guid;
    chip.title = "Open, select, and frame this element in the 3D view";
    chip.textContent = guid;
    host.appendChild(chip);
    at = start + guid.length;
  }
  if (at < source.length) host.append(source.slice(at));
}

// --------------------------------------------------------------------- panel
export function mountChat(root, options = {}) {
  root.classList.add("chat-root");
  root.innerHTML = TEMPLATE;
  const el = (role) => root.querySelector(`[data-role="${role}"]`);
  const act = (name) => root.querySelector(`[data-act="${name}"]`);
  const log = el("log");
  const input = el("input");
  const send = act("send");

  // Docked beside the viewer, an id in the transcript is a way into the 3D
  // view. The solo page has no viewer, so callers fall back to copying.
  const viewerAttached = () => Boolean(document.getElementById("canvas"));
  let lastTranscriptGuids = [];

  const selectInViewer = (guids, { isolate = false, modelId = null } = {}) => {
    if (!guids.length || !viewerAttached()) return false;
    lastTranscriptGuids = [...guids];
    document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
      detail: {
        action: isolate ? "isolate-guids" : "reveal-guids",
        guids,
        model_id: modelId,
        commandId: `chat-guid-${Date.now()}`,
      },
    }));
    return true;
  };

  // GlobalId chips are live wherever they appear: in an answer, and in the
  // tool result the answer was written from.
  log.addEventListener("click", (event) => {
    const chip = event.target.closest?.(".chat-guid");
    if (!chip) return;
    const guid = chip.dataset.guid || "";
    if (!guid) return;
    if (selectInViewer([guid])) return;
    if (navigator.clipboard?.writeText) {
      void navigator.clipboard.writeText(guid);
      chip.classList.add("copied");
      setTimeout(() => chip.classList.remove("copied"), 900);
    }
  });

  // A reader can also drag over a GlobalId instead of clicking its chip. With
  // that text selected, I means the same isolate action as it does over the
  // canvas. Text inputs keep ordinary typing behaviour.
  document.addEventListener("keydown", (event) => {
    if (event.key.toLowerCase() !== "i" || event.ctrlKey || event.metaKey || event.altKey) return;
    const target = event.target;
    const editing = target instanceof HTMLInputElement
      || target instanceof HTMLTextAreaElement
      || target?.isContentEditable;
    const textSelection = window.getSelection?.();
    const selectedText = textSelection
      && !textSelection.isCollapsed
      && textSelection.anchorNode
      && log.contains(textSelection.anchorNode)
      ? globalIdsIn(textSelection.toString())
      : [];
    if (editing && !selectedText.length) return;
    const current = viewerSelections().find(
      (row) => row.model_id === sessionStatus.view_model_id,
    );
    const guids = selectedText.length
      ? selectedText
      : lastTranscriptGuids.length ? lastTranscriptGuids : current?.guids || [];
    if (!guids.length) return;
    event.preventDefault();
    const known = viewerSelections().find(
      (row) => guids.every((guid) => row.guids.includes(guid)),
    );
    selectInViewer(guids, { isolate: true, modelId: known?.model_id || null });
  });

  let turns = [];
  let providers = [];
  let modelDetails = {};
  let busy = false;
  // A follow-up typed while the agent is still answering. It waits here so
  // that Enter never destroys the run it was typed underneath.
  let queuedPrompt = "";
  let resetInProgress = false;
  let aborter = null;
  let activeRun = null;
  let runSequence = 0;
  let localOnly = false;
  let sessionStatus = {};
  let settingsReturnFocus = null;
  let workspaceReturnFocus = null;
  let studioReturnFocus = null;
  // Agent packs: server-hosted specialists behind the same panel. Plain chat
  // is agent "" and keeps its stateless turns; a pack keeps its thread on the
  // console, so we only remember the thread id here.
  let agents = [];
  let agentBlocks = [];
  let capabilities = null;
  let sideOpen = false;
  let workspace = null;
  let workspaceView = "agent";
  let workspaceOpen = false;
  let workspaceRequest = 0;
  let workspaceError = "";
  let contentLibrary = null;
  let contentLibraryAgent = "";
  let contentLibraryError = "";
  let contentLibraryLoadingAgent = "";
  let contentLibraryRequest = 0;
  const contentAccessQueues = new Map();
  const contentAccessRevisions = new Map();
  let toolSearch = "";
  let contentSearch = "";
  let currentAgent = "";
  // Approving one call at a time is right for a single write and wrong for an
  // agent looping on the same tool. A decision can be extended to the rest of
  // this conversation, in memory only, and only for the exact capability set
  // the person already read.
  const approvalAllowlist = new Map();
  // What a switched-away assistant leaves behind. Offered, never merged: the
  // handoff keeps its scoping and the server thread stays append-only.
  let carryOffer = null;
  let pendingAttachments = [];
  // Thread ids belong to conversations, not assistants. Keying this map by an
  // assistant made a visually blank New Chat silently resume its last context.
  let conversationThreads = {};
  const historyStore = new ChatHistoryStore(window.localStorage);
  const studioStore = new StudioDraftStore(window.localStorage);
  let studioDraft = createStudioDraft();
  let studioSaveTimer = 0;
  let currentConversationId = "";
  let historyScope = "";
  let historyResetRequired = false;
  let sideReturnFocus = null;
  let armedDelete = null;
  let contextRequest = 0;
  let pendingCaptureCommand = "";
  // The panel also runs as a standalone page with no 3D surface behind it.
  let viewerLinked = false;
  let settingsApplyQueue = Promise.resolve();
  let settingsApplyRevision = 0;
  let queuedConnection = "";
  let appliedConnection = "";

  // This rebuild intentionally starts with a clean conversation archive. The
  // marker makes the migration one-shot: future v3 conversations remain, but
  // the incomplete v1/v2 UI state and its detached thread pointers do not.
  try {
    if (localStorage.getItem(HISTORY_RESET) !== "done") {
      historyStore.discardLegacy();
      localStorage.removeItem(SIDE_STORE);
      sessionStorage.removeItem(THREAD_STORE);
      historyResetRequired = true;
    }
  } catch {
    historyResetRequired = true;
  }

  // ------------------------------------------------------------------- shell
  // The panel is a 340px dock as often as it is a 1600px page, so the layout
  // is chosen from the component's own width. Each panel is either a grid
  // column or an overlay; `.chat-main` always owns the middle track, so an
  // overlaid panel can never squeeze the conversation.
  const shellWidth = () => root.getBoundingClientRect().width;
  const sideIsInline = () => root.classList.contains("side-inline");
  const focusableNow = (node) => Boolean(
    node instanceof HTMLElement
    && node.isConnected
    && !node.closest("[inert]")
    && !node.hidden
    && node.offsetParent !== null
  );
  const overlayReturnTarget = (preferred, fallback = input) => {
    const headerWorkspace = root.querySelector(".chat-workspace-toggle");
    return [preferred, headerWorkspace, fallback].find(focusableNow) || fallback;
  };

  // :focus-visible cannot tell a programmatic focus() from a keypress, so
  // opening a surface with the mouse used to outline whatever control focus
  // landed on. Track the modality and mark that one element, rather than
  // weakening the ring for the people who need it.
  let pointerInput = false;
  root.addEventListener("pointerdown", () => { pointerInput = true; }, true);
  root.addEventListener("keydown", () => { pointerInput = false; }, true);

  function focusQuietly(node, options = { preventScroll: true }) {
    if (!node) return;
    if (pointerInput) {
      node.dataset.quietFocus = "1";
      node.addEventListener("blur", () => { delete node.dataset.quietFocus; }, { once: true });
    }
    node.focus(options);
  }

  function syncShellLayout() {
    const width = shellWidth();
    const inlineSide = width >= SIDE_INLINE_WIDTH;
    const mainWidth = width
      - (inlineSide && sideOpen ? SIDE_WIDTH : 0);
    root.classList.toggle("chat-compact", width < COMPACT_WIDTH || mainWidth < COMPACT_MAIN_WIDTH);
    root.classList.toggle("chat-medium", width < MEDIUM_WIDTH || mainWidth < MEDIUM_MAIN_WIDTH);
    root.classList.toggle("side-inline", inlineSide);
    const side = el("side");
    side.setAttribute("role", inlineSide ? "complementary" : "dialog");
    if (inlineSide) side.removeAttribute("aria-modal");
    else side.setAttribute("aria-modal", "true");
  }

  el("workspace").inert = true;
  el("workspace").setAttribute("aria-hidden", "true");

  const shellObserver = typeof ResizeObserver === "undefined"
    ? null
    : new ResizeObserver(syncShellLayout);
  shellObserver?.observe(root);
  syncShellLayout();

  // A closed panel must be unreachable, not just invisible: `inert` takes it
  // out of the tab order and off the accessibility tree while the CSS slides
  // it away, which a `hidden` attribute would cancel.
  function applySide() {
    root.classList.toggle("side-open", sideOpen);
    for (const button of root.querySelectorAll('[data-act="toggle-side"]')) {
      button.setAttribute("aria-expanded", String(sideOpen));
    }
    el("side").inert = !sideOpen;
    el("side").setAttribute("aria-hidden", String(!sideOpen));
  }

  function setSide(
    open,
    { remember = true, trigger = document.activeElement, restoreFocus = true, moveFocus = true } = {},
  ) {
    const wasOpen = sideOpen;
    if (open && workspaceOpen) {
      closeWorkspace({ restoreFocus: false });
    }
    sideOpen = Boolean(open);
    if (sideOpen && !wasOpen && trigger instanceof HTMLElement && root.contains(trigger)) {
      sideReturnFocus = trigger;
    }
    syncShellLayout();
    applySide();
    const openerWillHide = trigger instanceof HTMLElement
      && trigger.classList.contains("chat-side-toggle");
    if (sideOpen && moveFocus && (!sideIsInline() || openerWillHide)) {
      requestAnimationFrame(() => focusQuietly(el("side").querySelector("button:not(:disabled)")));
    } else if (!sideOpen && wasOpen) {
      const target = sideReturnFocus;
      sideReturnFocus = null;
      if (restoreFocus) {
        focusQuietly(overlayReturnTarget(target));
      }
    }
    if (!remember) return;
    try {
      localStorage.setItem(SIDE_STORE, sideOpen ? "open" : "closed");
    } catch {
      /* private mode: the sidebar simply starts closed next time */
    }
  }

  const closeSideIfOverlay = () => {
    if (!sideIsInline()) setSide(false, { restoreFocus: false });
  };

  try {
    conversationThreads = JSON.parse(sessionStorage.getItem(THREAD_STORE) || "{}");
    if (!isPlainObject(conversationThreads)) conversationThreads = {};
  } catch {
    conversationThreads = {};
  }
  const saveThreads = () => {
    try {
      if (!settings.history) {
        sessionStorage.removeItem(THREAD_STORE);
        return;
      }
      if (Object.keys(conversationThreads).length) {
        sessionStorage.setItem(THREAD_STORE, JSON.stringify(conversationThreads));
      } else {
        sessionStorage.removeItem(THREAD_STORE);
      }
    } catch {
      /* private mode: threads just will not survive a reload */
    }
  };
  const pack = () => agents.find((agent) => agent.name === currentAgent) || null;
  const agentTitle = () => pack()?.title || PLAIN_CHAT.title;
  const settings = loadSettings();
  let workspaceTheme = null;
  // Read once, at mount. `settings.agent` is rewritten by the first background
  // saveSettings, which turns "never chosen" into "plain chat" and made the
  // landing assistant depend on which fetch happened to finish first.
  let preferredAgent = settings.agent;
  el("savehistory").checked = settings.history;
  el("savekey").checked = settings.credentialStore;
  el("theme").value = settings.theme;
  applyThemePreference(settings.theme);
  if (!settings.history) {
    conversationThreads = {};
    saveThreads();
  }

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
      // `undefined` means "never chosen", which is not the same as plain chat.
      agent: typeof saved.agent === "string" ? saved.agent : undefined,
      history: saved.history !== false,
      credentialStore: saved.credentialStore === true,
      theme: ["system", ...UI_THEME_IDS].includes(saved.theme) ? saved.theme : "system",
    };
  }

  function chosenModel() {
    const select = el("model");
    return select.value === "__custom__" ? el("modelcustom").value.trim() : select.value;
  }

  const remembered = (id) => settings.byProvider[id] || {};
  const promptSlot = (agent = currentAgent) => agent || "chat";

  function capabilityPreference(kind) {
    const model = chosenModel();
    const stored = remembered(el("provider").value).capabilities;
    const value = stored?.[model]?.[kind];
    return ["auto", "supported", "unsupported"].includes(value) ? value : "auto";
  }

  function effectiveCapability(kind) {
    const control = el(kind === "tools" ? "toolcap" : "visioncap");
    if (control.value === "supported") return true;
    if (control.value === "unsupported") return false;
    const detected = modelDetails[chosenModel()]?.[kind];
    return typeof detected === "boolean" ? detected : null;
  }

  function syncCapabilityControls() {
    el("toolcap").value = capabilityPreference("tools");
    el("visioncap").value = capabilityPreference("vision");
    renderCapabilities();
  }

  function capabilityDescription(kind) {
    const value = effectiveCapability(kind);
    if (value === true) return "supported";
    if (value === false) return "not supported";
    return "unknown, attempted when used";
  }

  function renderCapabilities() {
    const tools = effectiveCapability("tools");
    el("tools").disabled = tools === false;
    el("capstate").textContent =
      `Tool calling: ${capabilityDescription("tools")}. Image input: ${capabilityDescription("vision")}.`;
  }

  function saveSettings() {
    const id = el("provider").value;
    const model = chosenModel();
    const base_url = el("baseurl").value.trim();
    settings.provider = id;
    settings.byProvider[id] = { ...remembered(id), model, base_url };
    const capabilities = settings.byProvider[id].capabilities || {};
    capabilities[model] = {
      tools: el("toolcap").value,
      vision: el("visioncap").value,
    };
    settings.byProvider[id].capabilities = capabilities;
    settings.prompts[promptSlot()] = el("system").value;
    settings.tools = el("tools").checked;
    settings.temp = el("temp").value;
    settings.maxtok = el("maxtok").value;
    settings.agent = currentAgent;
    settings.history = el("savehistory").checked;
    settings.credentialStore = el("savekey").checked;
    settings.theme = el("theme").value;
    try {
      localStorage.setItem(STORE, JSON.stringify(settings));
    } catch {
      // Storage can be unavailable in private mode or full.
    }

    const key = el("key").value.trim();
    if (!id || !model) {
      render();
      return settingsApplyQueue;
    }
    const storeCredential = settings.credentialStore;
    const connection = JSON.stringify([id, model, base_url]);
    const requestSignature = JSON.stringify([id, model, base_url, key, storeCredential]);
    if ((queuedConnection === requestSignature) || (!key && appliedConnection === connection)) {
      render();
      return settingsApplyQueue;
    }
    const revision = ++settingsApplyRevision;
    queuedConnection = requestSignature;
    settingsApplyQueue = settingsApplyQueue.catch(() => {}).then(async () => {
      if (revision !== settingsApplyRevision) return;
      const response = await postJSON("/api/chat/select", {
        provider: id,
        model,
        base_url,
        api_key: key || undefined,
      });
      if (!response.ok) throw new Error(`Could not apply AI settings (HTTP ${response.status})`);
      if (revision !== settingsApplyRevision) return;
      const p = providers.find((row) => row.id === id);
      if (key && storeCredential) {
        const stored = await postJSON("/api/chat/credentials", {
          provider: id,
          action: "store",
          api_key: key,
        });
        const payload = await stored.json();
        if (!stored.ok) throw new Error(payload.hint || payload.error || "Could not save key");
        if (revision !== settingsApplyRevision) return;
        if (p) p.key_from_env = "keyring";
        note("Key saved in the operating-system credential store.");
      }
      if (revision !== settingsApplyRevision) return;
      appliedConnection = connection;
      if (!key) return;
      // The console has it now, so the DOM drops its only browser copy.
      if (p) p.has_key = true;
      if (provider()?.id === id && el("key").value.trim() === key) {
        el("key").value = "";
        el("key").type = "password";
        act("toggle-key").textContent = "Show typed key";
      }
    }).catch((error) => {
      if (revision === settingsApplyRevision) {
        note(error.message || "Could not save settings", true);
      }
    }).finally(() => {
      if (revision === settingsApplyRevision) {
        queuedConnection = "";
        render();
      }
    });
    render();
    return settingsApplyQueue;
  }

  /* Only fork when the context actually cannot survive the change.
   *
   * Plain chat re-sends its whole visible transcript on every request, so
   * asking a cheap model and then escalating the same question to a strong one
   * just works; throwing the conversation away was UI policy with nothing
   * behind it. An agent turn lives in a server thread built under the old
   * configuration, so that one still starts clean.
   */
  function forkConversationForConfigurationChange(
    message = "Assistant configuration changed. A fresh conversation is ready.",
    { keep = "" } = {},
  ) {
    if (!currentAgent && turns.length && keep) {
      note(keep);
      el("announce").textContent = keep;
      return;
    }
    startConversation(true, { focus: false });
    const footnote = log.querySelector(".chat-empty-note");
    if (footnote) footnote.textContent = message;
    el("announce").textContent = message;
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
    // The selection lives in the attachment tray now, beside the files it is
    // context alongside.
    renderAttachments();
    const mode = sessionMode();
    const autonomy = sessionAutonomy();
    el("session-mode").value = mode;
    el("session-mode").className = mode;
    el("session-mode").title = MODE_NOTE[mode] || "";
    el("session-autonomy").value = autonomy;
    el("session-autonomy").className = autonomy;
    el("session-autonomy").title = AUTONOMY_NOTE[autonomy] || "";
    // Save is the one thing on this row an assistant can never do, so it only
    // appears when there is something for a person to decide about.
    const save = act("save-model");
    save.hidden = !sessionStatus.dirty;
    save.title = "Write the in-memory changes to the IFC file. Assistants cannot do this.";

    const scope = el("side-scope");
    if (scope) {
      const open = sessionStatus.model || "";
      scope.textContent = open || "No model";
      scope.title = open ? `Assistants and conversations for ${open}` : "No IFC model is open";
    }
    const rows = Array.isArray(sessionStatus.models) ? sessionStatus.models : [];
    const picker = el("ifcmodel");
    const signature = rows.map((row) => `${row.id}:${row.name}:${row.active}`).join("|");
    if (picker.dataset.signature !== signature) {
      picker.innerHTML = "";
      for (const row of rows) {
        const option = document.createElement("option");
        option.value = row.id;
        option.textContent = row.name || row.id;
        picker.appendChild(option);
      }
      picker.dataset.signature = signature;
    }
    const active = rows.find((row) => row.id === sessionStatus.view_model_id)
      || rows.find((row) => row.active)
      || rows[0];
    if (active) picker.value = active.id;
    el("ifcmodel-wrap").hidden = rows.length < 2;
    el("modelname").closest("button").dataset.route = route.kind;
    el("modelname").closest("button").title = `${route.detail} Open AI settings.`;
    el("privacy").textContent = route.detail;
  }

  function applyThemePreference(value, { notifyViewer = false } = {}) {
    const theme = ["system", ...UI_THEME_IDS].includes(value) ? value : "system";
    const resolved = theme === "system"
      ? (workspaceTheme || "blue")
      : theme;
    root.dataset.theme = resolved;
    // The standalone Agent page has no viewer root to carry these tokens.
    // Keeping the document and component on the same resolved value also
    // prevents the workspace dialog/backdrop from retaining an older palette.
    document.documentElement.dataset.consoleTheme = resolved;
    if (el("theme")) el("theme").value = theme === "system" ? resolved : theme;
    if (notifyViewer) {
      document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
        detail: { action: "set-theme", theme },
      }));
    }
  }

  function rememberThemePreference(theme) {
    if (!UI_THEME_IDS.includes(theme)) return;
    settings.theme = theme;
    try {
      localStorage.setItem(STORE, JSON.stringify(settings));
    } catch {
      /* private mode: the live theme still remains synchronized */
    }
  }

  // Two independent questions, so two controls. Mode is what the assistant
  // may touch; autonomy is whether it stops and asks before touching it.
  // Neither of them can put the file on disk: that is the Save control, and
  // only a person reaches it.
  const sessionMode = () => (sessionStatus.mode === "edit" ? "edit" : "ask");
  const sessionAutonomy = () => (sessionStatus.ai_autonomy ? "auto" : "approval");

  const MODE_NOTE = {
    ask: "Ask mode. The assistant can inspect the model and run read-only code, but cannot change anything.",
    edit: "Edit mode. Changes stay in memory; only you can write them to the IFC file.",
  };
  const AUTONOMY_NOTE = {
    approval: "Approval. The assistant stops and asks before every protected tool call.",
    auto: "Auto. The assistant runs its tools and code without stopping to ask.",
  };

  async function applySessionStance(patch, noteText, tone) {
    const before = { mode: sessionMode(), autonomy: sessionAutonomy() };
    const controls = [el("session-mode"), el("session-autonomy")];
    for (const control of controls) control.disabled = true;
    try {
      const response = await postJSON("/api/session/mode", { ...patch, confirmed: true });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      sessionStatus.mode = payload.mode || sessionStatus.mode;
      sessionStatus.ai_autonomy = Boolean(payload.ai_autonomy);
      sessionStatus.dirty = Boolean(payload.dirty);
      renderContext();
      note(noteText, tone);
    } catch (exc) {
      el("session-mode").value = before.mode;
      el("session-autonomy").value = before.autonomy;
      note(`Could not change the session: ${exc.message || exc}`, true);
    } finally {
      for (const control of controls) control.disabled = false;
    }
  }

  async function changeSessionMode(mode) {
    const next = mode === "edit" ? "edit" : "ask";
    if (next === sessionMode()) return;
    await applySessionStance({ mode: next }, MODE_NOTE[next], next === "edit" ? "warn" : false);
  }

  async function changeSessionAutonomy(value) {
    const next = value === "auto" ? "auto" : "approval";
    if (next === sessionAutonomy()) return;
    await applySessionStance(
      { autonomy: next },
      AUTONOMY_NOTE[next],
      next === "auto" ? "warn" : false,
    );
  }

  async function saveModelFile() {
    const button = act("save-model");
    button.disabled = true;
    el("save-label").textContent = "Saving...";
    try {
      const response = await postJSON("/api/session/save", {});
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      sessionStatus.dirty = Boolean(payload.dirty);
      note(payload.saved ? "Saved to the IFC file." : "Nothing to save.");
      refreshContext();
    } catch (exc) {
      note(`Could not save: ${exc.message || exc}`, true);
    } finally {
      button.disabled = false;
      el("save-label").textContent = "Save";
      renderContext();
    }
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
    el("modelname").textContent = p ? (model ? `${short} · ${model}` : `${short} · no model`) : "chat off";
    el("modelname").title = p ? `${p.label}${model ? " · " + model : ""}` : "";
    const active = pack();
    el("title").textContent = agentTitle();
    el("avatar").textContent = initialsOf(agentTitle());
    // The compact header identifies the active agent; capability counts and
    // write policy belong in Agent workspace, not beneath the agent name.
    el("reach").textContent = "";
    el("identity").title = active
      ? active.description
      : "Plain chat over the open model. Open the workspace to see its reach.";
    input.placeholder = active ? `Ask ${active.title.toLowerCase()}...` : "Ask about the model...";
    send.disabled = resetInProgress || uploadsInFlight() || (!ready && !busy);
    el("export").hidden = turns.length === 0;
    if (!turns.length && !busy && (!log.children.length || log.querySelector(".chat-empty"))) {
      empty();
    }
    if (p) {
      el("note").textContent = p.key_from_env
        ? `${p.note} Key found in ${p.key_from_env}.`
        : p.note;
      el("keyfield").hidden = !p.needs_key;
      el("keystate").textContent = p.key_from_env === "keyring"
        ? "Stored by the operating system under service ifc-console. The browser cannot reveal it; paste a new key to replace it."
        : p.key_from_env
          ? `Read from ${p.key_from_env} in the console process. The browser cannot reveal it; paste a key here to override it for this run.`
          : p.has_key
            ? "Held only in the running console memory. Paste a new key to replace it."
            : "Paste a key for this run. Enable secure storage below to save it in the operating-system credential store.";
      act("delete-key").hidden = p.key_from_env !== "keyring";
      act("toggle-key").disabled = !el("key").value;
      renderCapabilities();
    }
    if (!p) el("hint").textContent = "chat is off; type /chat in the console";
    else if (!model) el("hint").innerHTML = 'choose a model in <b>Agent workspace</b>';
    else if (!hasKey(p)) el("hint").innerHTML = 'add an API key in <b>Agent workspace</b>';
    else if (uploadsInFlight()) el("hint").textContent = "indexing the attachment...";
    else if (busy) el("hint").innerHTML = "<b>Enter</b> queues · <b>Esc</b> stops";
    else el("hint").textContent = "";
    renderContext();
  }

  async function refreshContext() {
    const ticket = ++contextRequest;
    try {
      const response = await api("/api/status");
      if (!response.ok) return;
      const nextStatus = await response.json();
      if (ticket !== contextRequest) return;
      const activeModel = Array.isArray(nextStatus.models)
        ? nextStatus.models.find((model) => model?.active)
        : null;
      const modelIdentity = `${activeModel?.id || nextStatus.model || "none"}:${nextStatus.fingerprint || "unknown"}`;
      const projectIdentity = String(nextStatus.project_scope || "session").slice(0, 40);
      const nextScope = `project:${projectIdentity}:model:${String(modelIdentity).slice(0, 110)}`;
      const changedScope = Boolean(historyScope && historyScope !== nextScope);
      if (changedScope) {
        invalidateActiveRun();
        saveHistory();
      }
      historyScope = nextScope;
      historyStore.setScope(nextScope);
      sessionStatus = nextStatus;
      if (UI_THEME_IDS.includes(nextStatus.theme)) {
        workspaceTheme = nextStatus.theme;
        if (settings.theme === "system") applyThemePreference("system");
      }
      if (changedScope) startConversation(false, { focus: false });
      renderContext();
      renderSidebar();
      if (!turns.length && !busy && (!log.children.length || log.querySelector(".chat-empty"))) {
        empty();
      }
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
    el("key").type = "password";
    act("toggle-key").textContent = "Show typed key";
    modelDetails = {};
    setModelOptions([], mine.model || payload.selected.model || provider()?.suggested_model || "");
    syncCapabilityControls();
    render();
    if (hasKey(provider())) loadModels({ quiet: true });
    // no model yet is not an error worth a modal on open: the empty state
    // offers the button, and the dialog would cover the panel every time.
  }

  // ----------------------------------------------------------------- sidebar
  function sideAgentButton(agent) {
    const open = document.createElement("button");
    open.className = "chat-side-item t-press" + (agent.active ? " active" : "");
    open.type = "button";
    open.title = agent.description || agent.title;
    open.innerHTML = "<i></i><span></span>";
    open.querySelector("i").textContent = agent.initials;
    open.querySelector("span").textContent = agent.title;
    open.addEventListener("click", () => {
      if (agent.name !== currentAgent) switchAgent(agent.name);
      closeSideIfOverlay();
    });
    if (agent.active) open.setAttribute("aria-current", "true");
    return open;
  }

  function sidebarState() {
    return sidebarModel({
      // Plain chat is listed like any other surface. Without a row for it the
      // panel had no way back once an assistant had been chosen.
      agents: [...agents, PLAIN_CHAT],
      records: settings.history ? historyStore.list() : [],
      currentAgent,
      currentConversationId,
    });
  }

  function renderSidebar() {
    armedDelete = null;
    const model = sidebarState();
    const host = el("side-agents");
    host.innerHTML = "";
    for (const group of model.agentGroups) {
      const section = document.createElement("div");
      section.className = "chat-side-group";
      const label = document.createElement("span");
      label.className = "chat-side-label";
      label.textContent = group.label;
      section.appendChild(label);
      const list = document.createElement("div");
      list.className = "chat-side-list";
      for (const agent of group.agents) {
        const row = document.createElement("div");
        row.className = "chat-side-row" + (agent.active ? " active" : "");
        row.appendChild(sideAgentButton(agent));
        if (agent.deletable) row.appendChild(deleteButton(agent));
        list.appendChild(row);
      }
      section.appendChild(list);
      host.appendChild(section);
    }
    renderSideHistory(model);
  }

  // A row delete is irreversible and sits a few pixels from "open this".
  // The first click arms the button, the second acts, and anything else
  // disarms it, which is the same two-step shape as Delete all.
  function disarmDelete() {
    const armed = armedDelete;
    armedDelete = null;
    if (!armed || !armed.button.isConnected) return;
    armed.button.classList.remove("armed");
    armed.button.innerHTML = I.trash;
    armed.button.title = armed.label;
    armed.button.setAttribute("aria-label", armed.label);
  }

  function sideDeleteButton(label, confirmLabel, onConfirm) {
    const remove = document.createElement("button");
    remove.className = "chat-side-delete";
    remove.type = "button";
    remove.title = label;
    remove.setAttribute("aria-label", label);
    remove.innerHTML = I.trash;
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      if (armedDelete?.button === remove) {
        disarmDelete();
        void onConfirm();
        return;
      }
      disarmDelete();
      armedDelete = { button: remove, label };
      remove.classList.add("armed");
      remove.textContent = "Delete?";
      remove.title = confirmLabel;
      remove.setAttribute("aria-label", confirmLabel);
      el("announce").textContent = `${confirmLabel} Select it again to confirm.`;
    });
    return remove;
  }

  function deleteButton(agent) {
    return sideDeleteButton(
      `Delete ${agent.title}`,
      `Delete ${agent.title} permanently?`,
      () => deleteCustomAgent(agent),
    );
  }

  function renderSideHistory(model = sidebarState()) {
    const list = el("side-history");
    if (!list) return;
    armedDelete = null;
    renderHistoryControls();
    list.innerHTML = "";
    if (!settings.history) {
      const blank = document.createElement("span");
      blank.className = "chat-side-empty";
      blank.textContent = "History is off";
      list.appendChild(blank);
      return;
    }
    if (!model.conversationGroups.length) {
      const blank = document.createElement("span");
      blank.className = "chat-side-empty";
      blank.textContent = "Nothing saved yet";
      list.appendChild(blank);
      return;
    }
    for (const group of model.conversationGroups) {
      const when = document.createElement("span");
      when.className = "chat-side-when";
      when.textContent = group.label;
      list.appendChild(when);
      const rows = document.createElement("div");
      rows.className = "chat-side-list";
      for (const record of group.records) {
        const row = document.createElement("div");
        row.className = "chat-side-row" + (record.active ? " active" : "");
        const button = document.createElement("button");
        button.className = "chat-side-item chat-side-conversation t-press";
        button.type = "button";
        button.title = `${record.agent_title}: ${record.title} (${record.turns.length} turns)`;
        button.innerHTML = `<i>${I.chat}</i><span></span>`;
        button.querySelector("span").textContent = record.title;
        button.addEventListener("click", () => {
          selectHistory(record);
          closeSideIfOverlay();
        });
        if (record.active) button.setAttribute("aria-current", "true");
        row.appendChild(button);
        row.appendChild(sideDeleteButton(
          `Delete ${record.title}`,
          `Delete the conversation ${record.title} permanently?`,
          () => removeHistory(record),
        ));
        rows.appendChild(row);
      }
      list.appendChild(rows);
    }
  }

  function renderHistoryControls(message = "") {
    const visibleCount = historyStore.list().length;
    const count = historyStore.all().length;
    const countNode = el("history-count");
    if (countNode) countNode.textContent = String(visibleCount);
    const state = el("history-state");
    if (state) {
      state.textContent = message || (count
        ? `${count} conversation${count === 1 ? "" : "s"} saved in this browser across the open models.`
        : "No browser transcripts. Delete all also clears any project-local assistant context.");
    }
    const remove = act("request-clear-history");
    if (remove) remove.disabled = false;
  }

  async function deleteCustomAgent(agent) {
    try {
      const response = await postJSON("/api/agents/custom/delete", { name: agent.name });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (currentAgent === agent.name) switchAgent(defaultAgent());
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

  // The general assistant is the one to land on: it holds every block, so a
  // first question works without choosing anything. Plain chat stays one click
  // away in the sidebar for anyone who wants the bare loop.
  function defaultAgent() {
    if (agents.some((agent) => agent.name === "general")) return "general";
    return agents[0]?.name ?? "";
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
    const wanted = preferredAgent === undefined
      ? defaultAgent()
      : (preferredAgent === "" || agents.some((a) => a.name === preferredAgent)
          ? preferredAgent
          : defaultAgent());
    if (wanted !== currentAgent) switchAgent(wanted);
    else {
      render();
      syncReferenceFiles();
      loadWorkspace();
    }
    loadBlocks();
    renderSidebar();
  }

  async function loadBlocks() {
    try {
      const response = await api("/api/agents/blocks");
      const payload = await response.json();
      if (!response.ok) return;
      agentBlocks = Array.isArray(payload.blocks) ? payload.blocks : [];
      renderBuilderBlocks();
      renderBuilderPresets();
      renderStudio();
    } catch {
      agentBlocks = [];
    }
  }

  function switchAgent(name, { workspaceFocus = "nav" } = {}) {
    invalidateActiveRun();
    saveHistory();
    const outgoing = conversationRecord();
    if (!settings.history && outgoing.thread_id) void forgetAgentThread(outgoing);
    // A handoff to a specialist should not cost the scoping that led to it.
    // The slice is offered in the empty state, so nothing crosses threads
    // unless a person asks for it.
    const previousTitle = agentTitle();
    const slice = carrySlice(turns, { label: previousTitle });
    carryOffer = slice && name !== currentAgent ? { title: previousTitle, text: slice } : null;
    settings.prompts[promptSlot()] = el("system").value;
    currentAgent = name;
    preferredAgent = name;
    el("system").value = settings.prompts[promptSlot()] || "";
    turns = [];
    log.innerHTML = "";
    // Choosing an assistant starts a new conversation. Reopening prior context
    // is an explicit action in Conversations, never a side effect of a row click.
    currentConversationId = conversationId();
    approvalAllowlist.clear();
    empty();
    saveSettings();
    syncReferenceFiles();
    pendingAttachments = [];
    renderAttachments();
    renderSidebar();
    workspace = null;
    resetContentLibrary();
    const workspaceLoad = loadWorkspace();
    if (workspaceOpen && workspaceView === "content") void loadContentLibrary();
    if (workspaceOpen) {
      const restoreWorkspaceFocus = () => {
        if (!workspaceOpen || currentAgent !== name) return;
        const target = workspaceFocus === "agent-select"
          ? root.querySelector(".chat-ws-compact-agent-switcher select")
          : workspaceFocus === "agent"
            ? root.querySelector('.chat-workspace-agent[aria-current="true"]')
            : el("workspace-nav").querySelector(".active");
        target?.focus({ preventScroll: true });
      };
      requestAnimationFrame(restoreWorkspaceFocus);
      void workspaceLoad.then(() => {
        requestAnimationFrame(restoreWorkspaceFocus);
      });
    } else input.focus();
  }

  const uploadsInFlight = () => pendingAttachments.some((item) => item.pending);

  function viewerSelections() {
    if (Array.isArray(sessionStatus.selections)) {
      return sessionStatus.selections.filter((row) => (
        row && typeof row.model_id === "string" && Array.isArray(row.guids) && row.guids.length
      ));
    }
    const guids = Array.isArray(sessionStatus.selection) ? sessionStatus.selection : [];
    if (!guids.length || !sessionStatus.view_model_id) return [];
    const model = (Array.isArray(sessionStatus.models) ? sessionStatus.models : [])
      .find((row) => row.id === sessionStatus.view_model_id);
    return [{
      model_id: sessionStatus.view_model_id,
      model: model?.name || sessionStatus.model || "IFC",
      count: guids.length,
      guids,
    }];
  }

  const viewerSelectionCount = () => viewerSelections()
    .reduce((total, row) => total + row.guids.length, 0);

  // One control for "add something to this message". The paperclip and the
  // camera used to sit in the rail beside the model and mode selectors, which
  // mixed one-off context in with standing configuration.
  function plusOptions() {
    const usesFiles = Boolean(pack() && (pack().features || []).includes("files"));
    const selected = viewerSelectionCount();
    return [
      {
        icon: I.clip,
        label: "Attach a file",
        note: "This message only",
        available: usesFiles,
        run: () => el("file").click(),
      },
      {
        icon: I.camera,
        label: "Attach the current 3D view",
        note: effectiveCapability("vision") === false
          ? "The selected model is configured for text only"
          : "Sends what you can see",
        available: usesFiles && viewerLinked && effectiveCapability("vision") !== false,
        run: () => captureViewerEvidence(),
      },
      {
        icon: I.file,
        label: "Mention project content",
        note: "Also saved views and the 3D selection",
        available: usesFiles || viewerLinked,
        run: () => insertAtCaret("@"),
      },
      {
        icon: I.cube,
        label: selected ? "Frame the 3D selection" : "Select elements in the 3D view",
        note: selected ? `${selected} in context` : "Nothing selected yet",
        available: viewerLinked,
        run: () => {
          if (!selected) {
            note("Click elements in the 3D view to narrow what the tools see.");
            return;
          }
          const selectedModel = viewerSelections().find(
            (row) => row.model_id === sessionStatus.view_model_id,
          ) || viewerSelections()[0];
          document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
            detail: selectedModel.model_id === sessionStatus.view_model_id
              ? { action: "focus-selection" }
              : { action: "set-model", model_id: selectedModel.model_id },
          }));
        },
      },
      {
        icon: I.workspace,
        label: "Open Agent workspace",
        note: "Agents, content, models",
        available: true,
        run: () => openWorkspace(act("plus"), "agent"),
      },
    ].filter((option) => option.available);
  }

  function closePlusMenu({ restoreFocus = false } = {}) {
    const menu = el("plus-menu");
    if (menu.hidden) return;
    menu.hidden = true;
    act("plus")?.setAttribute("aria-expanded", "false");
    if (restoreFocus) focusQuietly(act("plus"));
  }

  function openPlusMenu() {
    const menu = el("plus-menu");
    menu.innerHTML = "";
    const options = plusOptions();
    if (!options.length) {
      note("This assistant takes no extra message context.");
      return;
    }
    for (const option of options) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "chat-plus-item t-press";
      item.setAttribute("role", "menuitem");
      item.innerHTML = `<i>${option.icon}</i><span><b></b><small></small></span>`;
      item.querySelector("b").textContent = option.label;
      item.querySelector("small").textContent = option.note;
      item.addEventListener("click", () => {
        closePlusMenu();
        option.run();
      });
      menu.appendChild(item);
    }
    menu.hidden = false;
    act("plus")?.setAttribute("aria-expanded", "true");
    requestAnimationFrame(() => focusQuietly(menu.querySelector("button")));
  }

  function insertAtCaret(text) {
    input.focus();
    const at = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, at);
    const needsSpace = before && !/\s$/.test(before);
    input.setRangeText(`${needsSpace ? " " : ""}${text}`, at, input.selectionEnd ?? at, "end");
    grow();
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  // Two composer affordances share one popup: `@` names something the run
  // should look at, `/` runs a panel setting. Both resolve against what the
  // panel already holds, so neither needs a round trip to offer a list.
  const STATIC_SLASH_COMMANDS = [
    {
      name: "agent",
      hint: "Switch assistant",
      run: () => openWorkspace(input, "agent"),
    },
    { name: "model", hint: "Choose the AI model", run: () => openSettings(input) },
    { name: "content", hint: "Manage project content", run: () => openWorkspace(input, "content") },
    { name: "tools", hint: "Inspect the tool surface", run: () => openWorkspace(input, "tools") },
    { name: "pipeline", hint: "How this agent works", run: () => openWorkspace(input, "agent") },
    { name: "new", hint: "Start a new conversation", run: () => startConversation() },
    { name: "clear", hint: "Start a new conversation", run: () => startConversation() },
    { name: "export", hint: "Download this conversation", run: () => exportConversation() },
    {
      name: "ask",
      hint: "Switch the session to ask mode",
      run: () => { void changeSessionMode("ask"); },
    },
    {
      name: "edit",
      hint: "Switch the session to edit mode",
      run: () => { void changeSessionMode("edit"); },
    },
    {
      name: "auto",
      hint: "Let the assistant act without asking",
      run: () => { void changeSessionAutonomy("auto"); },
    },
    {
      name: "approval",
      hint: "Make the assistant ask before protected calls",
      run: () => { void changeSessionAutonomy("approval"); },
    },
    {
      name: "save",
      hint: "Write the in-memory changes to the IFC file",
      run: () => { void saveModelFile(); },
    },
    {
      name: "code",
      hint: "Ask this assistant to run ifcopenshell code",
      run: () => {
        input.focus();
        input.value = "Write and run ifcopenshell code to ";
        grow();
        input.setSelectionRange(input.value.length, input.value.length);
      },
    },
  ];

  let suggestState = null;

  // A skill is a saved measurement procedure the agent can follow, so it reads
  // as a command rather than as something to go and find in the workspace.
  function slashCommands() {
    const loaded = workspace && workspace.name === currentAgent ? workspace.skills : [];
    const skills = (Array.isArray(loaded) ? loaded : [])
      .map((skill) => ({
        name: String(skill.name || "").trim().replace(/\s+/g, "-").toLowerCase(),
        hint: skill.description || "saved procedure",
        run: () => insertAtCaret(`Follow the ${skill.name} skill: `),
      }))
      .filter((row) => row.name && !STATIC_SLASH_COMMANDS.some((item) => item.name === row.name));
    return [...STATIC_SLASH_COMMANDS, ...skills];
  }

  function mentionableFiles() {
    const rows = workspace && workspace.name === currentAgent ? workspace.files : [];
    return Array.isArray(rows) ? rows : [];
  }

  const savedViewNames = () =>
    (Array.isArray(sessionStatus.saved_views) ? sessionStatus.saved_views : [])
      .map((name) => String(name || "").trim())
      .filter(Boolean);

  /* Everything `@` can name, in the order a reader reaches for it.
   *
   * The live 3D selection and the saved views come first because they are the
   * two things the panel and the viewer already agree on and the model cannot
   * see; both resolve from state the panel holds, so neither costs a request.
   */
  function mentionRows() {
    const rows = [];
    const selections = viewerSelections();
    const selectionCount = viewerSelectionCount();
    if (selectionCount) {
      const named = selections.map((row) => {
        const guids = row.guids.slice(0, 25);
        const rest = row.guids.length - guids.length;
        return `${row.model || row.model_id}: ${guids.join(" ")}${rest ? ` and ${rest} more` : ""}`;
      });
      rows.push({
        label: "selection",
        note: `${selectionCount} element${selectionCount === 1 ? "" : "s"} across ${selections.length} IFC file${selections.length === 1 ? "" : "s"}`,
        insert: `@selection [${named.join("; ")}]`,
      });
    }
    for (const name of savedViewNames()) {
      rows.push({ label: `view:${name}`, note: "saved 3D view", insert: `@view:${name}` });
    }
    for (const file of mentionableFiles()) {
      rows.push({
        label: String(file.name || file.path || ""),
        note: file.media === "image" ? "image" : "document",
        file,
      });
    }
    return rows;
  }

  /** The `@name` or `/word` the caret currently sits in, if any. */
  function activeToken() {
    const at = input.selectionStart ?? input.value.length;
    const before = input.value.slice(0, at);
    const mention = /(^|\s)@([^\s@]*)$/.exec(before);
    if (mention) {
      return { kind: "mention", query: mention[2], start: at - mention[2].length - 1, end: at };
    }
    // A command is only a command at the very start of the message; `/` in
    // the middle of a sentence is a slash.
    const command = /^\/([a-z-]*)$/.exec(before);
    if (command) return { kind: "command", query: command[1], start: 0, end: at };
    return null;
  }

  function suggestionsFor(token) {
    const needle = token.query.toLowerCase();
    if (token.kind === "command") {
      return slashCommands()
        .filter((item) => item.name.startsWith(needle))
        .slice(0, 8)
        .map((item) => ({ label: `/${item.name}`, note: item.hint, command: item }));
    }
    return mentionRows()
      .filter((row) => !needle || row.label.toLowerCase().includes(needle))
      .slice(0, 8);
  }

  function closeSuggest() {
    if (!suggestState) return;
    suggestState = null;
    el("suggest").hidden = true;
    el("suggest").innerHTML = "";
  }

  function renderSuggest() {
    const box = el("suggest");
    box.innerHTML = "";
    if (!suggestState) return;
    suggestState.items.forEach((item, index) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "chat-suggest-item" + (index === suggestState.active ? " active" : "");
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(index === suggestState.active));
      row.innerHTML = "<b></b><small></small>";
      row.querySelector("b").textContent = item.label;
      row.querySelector("small").textContent = item.note;
      row.addEventListener("mousedown", (event) => {
        event.preventDefault();
        applySuggestion(index);
      });
      box.appendChild(row);
    });
    box.hidden = false;
  }

  function updateSuggest() {
    const token = activeToken();
    if (!token) return closeSuggest();
    const items = suggestionsFor(token);
    if (!items.length) return closeSuggest();
    const keep = suggestState && suggestState.kind === token.kind
      ? Math.min(suggestState.active, items.length - 1)
      : 0;
    suggestState = { ...token, items, active: Math.max(0, keep) };
    renderSuggest();
  }

  function applySuggestion(index = suggestState?.active ?? 0) {
    if (!suggestState) return;
    const item = suggestState.items[index];
    if (!item) return;
    const { start, end } = suggestState;
    closeSuggest();
    if (item.command) {
      input.value = input.value.slice(end).trimStart();
      grow();
      item.command.run();
      return;
    }
    // The 3D selection and the saved views name themselves in the prompt;
    // there is nothing to attach, because the tools already reach them.
    if (item.insert) {
      input.setRangeText(`${item.insert} `, start, end, "end");
      grow();
      input.focus();
      return;
    }
    // A mention both names the file in the prompt and attaches it, so the
    // model is told what to read and the server is told it may.
    input.setRangeText(`@${item.label} `, start, end, "end");
    grow();
    const path = String(item.file.path || "");
    if (path && !pendingAttachments.some((entry) => entry.path === path)) {
      pendingAttachments.push({
        path,
        media: item.file.media === "image" ? "image" : "document",
        name: item.label,
      });
      renderAttachments();
      render();
    }
    input.focus();
  }

  function captureViewerEvidence() {
    pendingCaptureCommand = `chat-evidence-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
      detail: { action: "capture-evidence", format: "png", commandId: pendingCaptureCommand },
    }));
  }

  function renderAttachments() {
    const tray = el("attachments");
    const selections = viewerSelections();
    const selected = viewerSelectionCount();
    tray.hidden = !pendingAttachments.length && !selected && !queuedPrompt;
    tray.innerHTML = "";
    // A message typed mid-answer is waiting, not lost, and it can be taken
    // back before the run it is queued behind finishes.
    if (queuedPrompt) {
      const chip = document.createElement("span");
      chip.className = "chat-attachment-chip queued";
      chip.innerHTML =
        "<span></span>"
        + `<button type="button" class="chat-attachment-remove" data-act="drop-queued"`
        + ` aria-label="Take the queued message back">${I.close}</button>`;
      chip.querySelector("span").textContent = `queued: ${queuedPrompt}`;
      chip.title = `Sends as soon as this response finishes:\n${queuedPrompt}`;
      tray.appendChild(chip);
    }
    // The viewer selection reads as context, so it belongs with the other
    // context and not in the control rail. Nothing is shown for "no
    // selection": the whole model is always available, so saying so every
    // time was noise dressed up as state.
    for (const selectedModel of selections) {
      const count = selectedModel.guids.length;
      const chip = document.createElement("span");
      chip.className = "chat-attachment-chip selection";
      chip.innerHTML =
        `<i>${I.cube}</i><span></span>`
        + `<button type="button" class="chat-attachment-remove" data-act="drop-selection"`
        + ` aria-label="Clear this IFC selection">${I.close}</button>`;
      chip.querySelector("button").dataset.modelId = selectedModel.model_id;
      chip.querySelector("span").textContent =
        `${selectedModel.model || selectedModel.model_id} · ${count}`;
      chip.title = `${count} selected IFC element${count === 1 ? "" : "s"} from this file go to the tools`
        + " with this message. Click the name to open that IFC selection.";
      chip.querySelector("span").addEventListener("click", () => {
        document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
          detail: selectedModel.model_id === sessionStatus.view_model_id
            ? { action: "focus-selection" }
            : { action: "set-model", model_id: selectedModel.model_id },
        }));
      });
      tray.appendChild(chip);
    }
    for (const [index, attachment] of pendingAttachments.entries()) {
      const chip = document.createElement("span");
      chip.className = `chat-attachment-chip ${attachment.media}`
        + (attachment.pending ? " pending" : "");
      // A chip that is still indexing has no path yet, so it offers no remove
      // control: dropping it would leave the upload running with no owner.
      chip.innerHTML = attachment.pending
        ? '<i class="chat-attachment-spin" aria-hidden="true"></i><span></span>'
        : `<span></span><button type="button" class="chat-attachment-remove" data-index="${index}" aria-label="Remove attachment">${I.close}</button>`;
      chip.querySelector("span").textContent = attachment.name;
      if (attachment.pending) chip.title = `Indexing ${attachment.name}...`;
      tray.appendChild(chip);
    }
  }

  // The file list itself lives in the workspace payload. This call exists for
  // its side effect: it re-indexes the project references and reports a
  // corpus problem where the user is looking rather than at the next question.
  async function syncReferenceFiles() {
    const agent = currentAgent;
    if (!agent || !(pack()?.features || []).includes("files")) return;
    try {
      const response = await api(`/api/agents/files?agent=${encodeURIComponent(agent)}`);
      const payload = await response.json();
      if (agent !== currentAgent) return;
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (payload.problem) note(payload.problem, true);
    } catch (exc) {
      if (agent === currentAgent) note(exc.message || String(exc), true);
    }
  }

  function note(text, tone = false) {
    if (!turns.length && log.querySelector(".chat-empty")) log.innerHTML = "";
    const line = document.createElement("div");
    const kind = tone === true ? "bad" : tone === false ? "" : String(tone);
    line.className = "chat-note" + (kind ? ` ${kind}` : "");
    line.setAttribute("role", "status");
    line.textContent = text;
    log.appendChild(line);
    scroll();
  }

  async function uploadFiles(files) {
    const agent = currentAgent;
    const conversation = currentConversationId;
    for (const file of files) {
      // Indexing a PDF takes seconds. The chip appears first so the composer
      // shows the work, and send waits for it rather than dropping a path
      // that has not been assigned yet.
      const placeholder = {
        pending: true,
        name: file.name,
        media: /\.(png|jpe?g)$/i.test(file.name) ? "image" : "document",
      };
      pendingAttachments.push(placeholder);
      renderAttachments();
      render();
      const drop = () => {
        const at = pendingAttachments.indexOf(placeholder);
        if (at >= 0) pendingAttachments.splice(at, 1);
      };
      try {
        const query = `agent=${encodeURIComponent(agent)}&name=${encodeURIComponent(file.name)}`;
        const response = await api(`/api/agents/upload?${query}`, {
          method: "POST",
          body: file,
        });
        const payload = await response.json();
        if (agent !== currentAgent || conversation !== currentConversationId) {
          drop();
          renderAttachments();
          render();
          return;
        }
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        if (payload.indexed && payload.attachment?.path) {
          delete placeholder.pending;
          placeholder.path = payload.attachment.path;
          placeholder.media = payload.attachment.media || placeholder.media;
        } else {
          drop();
          note(
            payload.indexed
              ? `${file.name}: indexed without an attachable path.`
              : `${file.name}: saved locally but not indexed: ${payload.error}`
                  + (payload.hint ? ` ${payload.hint}` : ""),
            true,
          );
        }
      } catch (exc) {
        drop();
        if (agent === currentAgent && conversation === currentConversationId) {
          note(`${file.name}: ${exc.message || exc}`, true);
        }
      }
      renderAttachments();
      render();
    }
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
    syncCapabilityControls();
  }

  // Switching provider mid-load must not let the older answer win.
  let modelRequest = 0;

  async function loadModels({ quiet = false } = {}) {
    const p = provider();
    if (!p) return;
    const ticket = ++modelRequest;
    const requestedProvider = p.id;
    const requestedBaseUrl = el("baseurl").value.trim();
    const requestedKey = el("key").value.trim();
    const button = act("models");
    button.classList.add("spin");
    if (!quiet) el("note").textContent = "loading models...";
    try {
      const response = await postJSON("/api/chat/models", {
        provider: requestedProvider,
        base_url: requestedBaseUrl || undefined,
        api_key: requestedKey || undefined,
      });
      const payload = await response.json();
      if (
        ticket !== modelRequest
        || provider()?.id !== requestedProvider
        || el("baseurl").value.trim() !== requestedBaseUrl
        || el("key").value.trim() !== requestedKey
      ) return;
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      modelDetails = payload.model_details && typeof payload.model_details === "object"
        ? payload.model_details
        : {};
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

  // ---------------------------------------------------------------- settings
  // Global model settings are one view in Agent workspace. The composer,
  // top bar, and sidebar all lead to the same surface.
  function openSettings(trigger = document.activeElement) {
    settingsReturnFocus = root.contains(trigger) ? trigger : input;
    openWorkspace(trigger, "models");
    for (const button of root.querySelectorAll('[data-act="settings"]')) {
      button.setAttribute("aria-expanded", "true");
    }
    requestAnimationFrame(() => focusQuietly(el("provider")));
  }

  function closeSettings({ restoreFocus = true } = {}) {
    for (const button of root.querySelectorAll('[data-act="settings"]')) {
      button.setAttribute("aria-expanded", "false");
    }
    const target = settingsReturnFocus && settingsReturnFocus.isConnected
      ? settingsReturnFocus
      : workspaceReturnFocus && workspaceReturnFocus.isConnected
        ? workspaceReturnFocus
        : input;
    settingsReturnFocus = null;
    closeWorkspace({ restoreFocus: false });
    if (restoreFocus) focusQuietly(overlayReturnTarget(target));
  }

  function readStudioDraft() {
    const boxes = [...el("builder-blocks").querySelectorAll("input[type=checkbox]")];
    const checked = boxes.length
      ? boxes.filter((box) => box.checked).map((box) => box.value)
      : studioDraft.blocks;
    const selected = [
      ...studioDraft.blocks.filter((name) => checked.includes(name)),
      ...checked.filter((name) => !studioDraft.blocks.includes(name)),
    ];
    return normalizeStudioDraft({
      ...studioDraft,
      title: el("builder-title").value,
      description: el("builder-description").value,
      instructions: el("builder-instructions").value,
      blocks: selected,
      starters: el("builder-starters").value,
      workflow: {
        strategy: el("builder-strategy").value,
        max_tool_rounds: el("builder-rounds").value,
        max_tool_calls: el("builder-calls").value,
      },
    });
  }

  function writeStudioDraft(value) {
    studioDraft = normalizeStudioDraft(value);
    el("builder-title").value = studioDraft.title;
    el("builder-description").value = studioDraft.description;
    el("builder-instructions").value = studioDraft.instructions;
    el("builder-starters").value = studioDraft.starters.join("\n");
    el("builder-strategy").value = studioDraft.workflow.strategy;
    el("builder-rounds").value = String(studioDraft.workflow.max_tool_rounds);
    el("builder-calls").value = String(studioDraft.workflow.max_tool_calls);
    renderBuilderBlocks();
    renderStudio();
  }

  function renderBuilderPresets() {
    const row = el("builder-presets");
    if (!row) return;
    row.innerHTML = "";
    const blank = { title: "Blank", description: "Pick blocks yourself.", blocks: [] };
    for (const agent of [blank, ...agents.filter((item) => item.kind !== "custom")]) {
      const button = document.createElement("button");
      button.className = "chat-preset t-press";
      button.type = "button";
      button.title = agent.description || agent.title;
      button.innerHTML = "<b></b><small></small>";
      button.querySelector("b").textContent = agent.title;
      button.querySelector("small").textContent = agent.blocks?.length
        ? `${agent.blocks.length} blocks`
        : "start clean";
      button.classList.toggle(
        "active",
        Boolean(agent.name && studioDraft.sourceName === agent.name),
      );
      button.addEventListener("click", () => {
        const next = agent.name ? createStudioDraft(agent) : createStudioDraft();
        writeStudioDraft(next);
        el("studio-capabilities").open = !next.blocks.length;
        for (const other of row.children) other.classList.remove("active");
        button.classList.add("active");
      });
      row.appendChild(button);
    }
  }

  function moveStudioBlock(name, delta) {
    studioDraft = readStudioDraft();
    const from = studioDraft.blocks.indexOf(name);
    const next = reorderSelectedBlocks(studioDraft, from, from + delta);
    writeStudioDraft(next);
    requestAnimationFrame(() => {
      focusQuietly([...el("builder-blocks").querySelectorAll("input[type=checkbox]")]
        .find((box) => box.value === name));
    });
  }

  function renderBuilderBlocks() {
    const grid = el("builder-blocks");
    if (!grid) return;
    grid.innerHTML = "";
    const order = new Map(studioDraft.blocks.map((name, index) => [name, index]));
    const rows = [...agentBlocks].sort((left, right) => {
      const leftOrder = order.has(left.name) ? order.get(left.name) : Number.MAX_SAFE_INTEGER;
      const rightOrder = order.has(right.name) ? order.get(right.name) : Number.MAX_SAFE_INTEGER;
      return leftOrder - rightOrder;
    });
    for (const block of rows) {
      const selectedIndex = order.get(block.name);
      const card = document.createElement("div");
      card.className = "chat-block" + (selectedIndex === undefined ? "" : " selected");
      const choice = document.createElement("label");
      choice.className = "chat-block-choice";
      choice.innerHTML = `<input type="checkbox" value="${esc(block.name)}"><span><i></i><b></b><small></small></span>`;
      const box = choice.querySelector("input");
      box.checked = selectedIndex !== undefined;
      choice.querySelector("i").textContent = selectedIndex === undefined
        ? "+"
        : String(selectedIndex + 1).padStart(2, "0");
      choice.querySelector("b").textContent = block.title;
      choice.querySelector("small").textContent = block.description;
      box.addEventListener("change", () => {
        studioDraft = readStudioDraft();
        renderBuilderBlocks();
        renderStudio();
        requestAnimationFrame(() => {
          focusQuietly([...el("builder-blocks").querySelectorAll("input[type=checkbox]")]
            .find((candidate) => candidate.value === block.name));
        });
      });
      card.appendChild(choice);
      if (selectedIndex !== undefined) {
        const controls = document.createElement("div");
        controls.className = "chat-block-order";
        for (const [label, delta, icon] of [
          ["Move earlier", -1, I.up],
          ["Move later", 1, I.down],
        ]) {
          const button = document.createElement("button");
          button.type = "button";
          button.className = "chat-icon";
          button.title = label;
          button.setAttribute("aria-label", `${label}: ${block.title}`);
          button.innerHTML = icon;
          button.disabled = delta < 0 ? selectedIndex === 0 : selectedIndex === studioDraft.blocks.length - 1;
          button.addEventListener("click", () => moveStudioBlock(block.name, delta));
          controls.appendChild(button);
        }
        card.appendChild(controls);
      }
      grid.appendChild(card);
    }
  }

  function persistStudioDraft() {
    if (studioSaveTimer) window.clearTimeout(studioSaveTimer);
    studioSaveTimer = 0;
    studioStore.save(studioDraft);
  }

  function scheduleStudioDraftSave() {
    if (studioSaveTimer) window.clearTimeout(studioSaveTimer);
    studioSaveTimer = window.setTimeout(persistStudioDraft, 180);
  }

  function renderStudio() {
    if (el("builder-modal").hidden) return;
    studioDraft = readStudioDraft();
    const model = studioModel(studioDraft, agentBlocks);
    scheduleStudioDraftSave();
    el("studio-tool-count").textContent = String(model.toolCount);
    el("studio-stage-count").textContent = String(model.reachableStages.length);
    el("studio-block-count").textContent = String(model.selectedBlocks.length);
    el("builder-count").textContent = model.selectedBlocks.length
      ? `${model.selectedBlocks.length} selected, ordered by priority`
      : "Pick at least one";
    el("studio-draft-status").textContent = studioDraft.name
      ? `Editing ${studioDraft.title || "custom assistant"}`
      : "Draft saved locally";
    act("save-builder").textContent = studioDraft.name ? "Save assistant" : "Create assistant";
  }

  function openBuilder(trigger = document.activeElement, source = null) {
    studioReturnFocus = root.contains(trigger) ? trigger : input;
    const saved = source ? null : studioStore.load();
    studioDraft = source ? createStudioDraft(source) : (saved || createStudioDraft());
    el("builder-error").textContent = "";
    renderBuilderPresets();
    // Populate the still-hidden form first. Opening the view calls
    // renderStudio(), which reads the form back into the draft.
    writeStudioDraft(studioDraft);
    openWorkspace(trigger, "builder");
    el("studio-capabilities").open = !studioDraft.blocks.length;
    if (!agentBlocks.length) loadBlocks();
    requestAnimationFrame(() => focusQuietly(el("builder-title")));
  }

  function closeBuilder({ restoreFocus = true } = {}) {
    setWorkspaceView("agent");
    const target = studioReturnFocus && studioReturnFocus.isConnected
      ? studioReturnFocus : el("workspace-nav").querySelector('[data-workspace-view="agent"]');
    studioReturnFocus = null;
    if (restoreFocus) focusQuietly(overlayReturnTarget(target));
  }

  async function saveBuilder() {
    studioDraft = readStudioDraft();
    const model = studioModel(studioDraft, agentBlocks);
    const error = el("builder-error");
    if (!model.ready) {
      error.textContent = model.errors.join(" ");
      if (!model.selectedBlocks.length) el("studio-capabilities").open = true;
      const target = !studioDraft.title
        ? el("builder-title")
        : !studioDraft.description
          ? el("builder-description")
          : !model.selectedBlocks.length
            ? el("builder-blocks").querySelector("input")
            : el("builder-instructions");
      target?.focus();
      return;
    }
    const button = act("save-builder");
    const editing = Boolean(studioDraft.name);
    button.disabled = true;
    button.textContent = editing ? "Saving..." : "Creating...";
    try {
      const response = await postJSON("/api/agents/custom", studioPayload(studioDraft));
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      const editedCurrent = editing && payload.agent.name === currentAgent;
      settings.agent = preferredAgent = payload.agent.name;
      if (editedCurrent) {
        startConversation(true, { focus: false });
        workspaceRequest += 1;
        workspace = null;
      }
      closeBuilder();
      studioStore.clear();
      await loadAgents();
      if (editedCurrent) await loadWorkspace({ force: true });
      note(`${payload.agent.title} is ready. Its workflow is limited to the reviewed capabilities.`);
    } catch (exc) {
      error.textContent = exc.message || String(exc);
    } finally {
      button.disabled = false;
      button.textContent = editing ? "Save assistant" : "Create assistant";
    }
  }

  // -------------------------------------------------------------- workspace
  // Everything about the current assistant, out of the transcript's way: what
  // it is, which blocks and tools it holds, what it may write, the files it
  // can see, and the instructions that shape it. Plain chat is described here
  // too, so the dedicated inspector control always opens something useful.
  async function loadWorkspace({ force = false } = {}) {
    const agent = currentAgent;
    if (!force && workspace && workspace.name === agent) {
      renderWorkspace();
      return;
    }
    const ticket = ++workspaceRequest;
    workspaceError = "";
    renderWorkspace();
    try {
      const query =
        `agent=${encodeURIComponent(agent)}` +
        `&instructions=${encodeURIComponent(el("system").value.trim().slice(0, 12000))}`;
      const response = await api(`/api/agents/workspace?${query}`);
      const payload = await response.json();
      if (ticket !== workspaceRequest || agent !== currentAgent) return;
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      workspace = workspaceModel(payload);
    } catch (exc) {
      if (ticket !== workspaceRequest) return;
      workspace = null;
      workspaceError = exc.message || String(exc);
    }
    renderWorkspace();
    el("reach").textContent = "";
  }

  function setWorkspaceView(view, { focus = false } = {}) {
    const detailViews = ["agent", "capabilities", "tools", "skills"];
    const next = [...detailViews, "content", "models", "app", "builder"].includes(view)
      ? view
      : "agent";
    const changed = next !== workspaceView;
    if (workspaceView === "builder" && next !== "builder" && !el("builder-modal").hidden) {
      studioDraft = readStudioDraft();
      persistStudioDraft();
    }
    if (["models", "app"].includes(workspaceView) && !["models", "app"].includes(next)
        && !el("modal").hidden) {
      saveSettings();
    }
    workspaceView = next;
    el("workspace-pane").hidden = !detailViews.includes(next);
    el("workspace-foot").hidden = next !== "agent";
    el("content-pane").hidden = next !== "content";
    el("modal").hidden = !["models", "app"].includes(next);
    el("settings-models").hidden = next !== "models";
    el("settings-app").hidden = next !== "app";
    el("privacy").hidden = next === "app";
    el("builder-modal").hidden = next !== "builder";
    const labelledPane = detailViews.includes(next)
      ? el("workspace-pane")
      : next === "content"
        ? el("content-pane")
        : ["models", "app"].includes(next)
          ? el("modal")
          : null;
    if (labelledPane) labelledPane.setAttribute("aria-labelledby", `chat-workspace-tab-${next}`);
    for (const button of el("workspace-nav").querySelectorAll("[data-workspace-view]")) {
      const activeView = next === "builder" ? "agent" : next;
      const active = button.dataset.workspaceView === activeView;
      button.classList.toggle("active", active);
      button.setAttribute("aria-current", active ? "page" : "false");
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    }
    for (const button of root.querySelectorAll('[data-act="settings"]')) {
      button.setAttribute("aria-expanded", String(next === "models" && workspaceOpen));
    }
    if (detailViews.includes(next)) renderWorkspace();
    else if (next === "content") renderContentWorkspace();
    else if (next === "builder") renderStudio();
    if (changed) {
      const scroller = detailViews.includes(next)
        ? el("ws-body")
        : next === "content"
          ? el("content-body")
          : ["models", "app"].includes(next)
            ? el("modal").querySelector(".chat-dialog-body")
            : el("builder-modal").querySelector(".chat-studio-editor");
      if (scroller) scroller.scrollTop = 0;
    }
    if (focus) {
      requestAnimationFrame(() => {
        const selected = el("workspace-nav").querySelector(`[data-workspace-view="${next}"]`);
        if (selected) {
          focusQuietly(selected);
          return;
        }
        const pane = detailViews.includes(next)
          ? el("workspace-pane")
          : next === "content"
            ? el("content-pane")
            : ["models", "app"].includes(next)
              ? el("modal")
              : el("builder-modal");
        focusQuietly(pane.querySelector(
          "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled)",
        ));
      });
    }
  }

  function openWorkspace(trigger = document.activeElement, view = "agent") {
    if (!workspaceOpen) {
      workspaceReturnFocus = trigger instanceof HTMLElement && root.contains(trigger)
        ? trigger
        : el("identity");
    }
    if (view === "overview") view = "agent";
    if (view === "instructions") view = "agent";
    // Pipeline is part of an agent now, not a place of its own.
    if (view === "pipeline") view = "agent";
    if (view === "settings") view = "models";
    if (sideOpen) {
      setSide(false, { restoreFocus: false, moveFocus: false });
    }
    const dialog = el("workspace");
    workspaceOpen = true;
    syncShellLayout();
    root.classList.add("workspace-open");
    dialog.inert = false;
    dialog.removeAttribute("aria-hidden");
    if (!dialog.open) dialog.showModal();
    for (const button of root.querySelectorAll('[data-act="workspace"]')) {
      button.setAttribute("aria-expanded", "true");
    }
    setWorkspaceView(view);
    loadWorkspace();
    requestAnimationFrame(() => {
      focusQuietly(el("workspace-nav").querySelector("[role=tab].active"));
    });
  }

  function closeWorkspace({ restoreFocus = true } = {}) {
    if (workspaceView === "builder" && !el("builder-modal").hidden) {
      studioDraft = readStudioDraft();
      persistStudioDraft();
    }
    if (["models", "app"].includes(workspaceView) && !el("modal").hidden) saveSettings();
    workspaceOpen = false;
    syncShellLayout();
    root.classList.remove("workspace-open");
    const dialog = el("workspace");
    if (dialog.open) dialog.close();
    dialog.inert = true;
    dialog.setAttribute("aria-hidden", "true");
    for (const button of root.querySelectorAll('[data-act="workspace"]')) {
      button.setAttribute("aria-expanded", "false");
    }
    for (const button of root.querySelectorAll('[data-act="settings"]')) {
      button.setAttribute("aria-expanded", "false");
    }
    const target = workspaceReturnFocus;
    settingsReturnFocus = null;
    workspaceReturnFocus = null;
    if (restoreFocus) focusQuietly(overlayReturnTarget(target));
  }

  function toggleWorkspace(trigger) {
    if (workspaceOpen) closeWorkspace();
    else openWorkspace(trigger, "agent");
  }

  const wsNode = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  function renderWorkspaceAgentList() {
    const list = el("workspace-agents");
    if (!list) return;
    list.innerHTML = "";
    for (const agent of [...agents, PLAIN_CHAT]) {
      const button = wsNode(
        "button",
        "chat-workspace-agent" + (agent.name === currentAgent ? " active" : ""),
      );
      button.type = "button";
      button.title = agent.description || agent.title;
      button.setAttribute("aria-current", agent.name === currentAgent ? "true" : "false");
      button.appendChild(wsNode("span", "chat-workspace-agent-mark", initialsOf(agent.title)));
      button.appendChild(wsNode("span", "", agent.title));
      button.addEventListener("click", () => {
        setWorkspaceView("agent");
        switchAgent(agent.name, { workspaceFocus: "agent" });
      });
      list.appendChild(button);
    }
  }

  function wsPageHeading(body, eyebrow, title, description) {
    const head = wsNode("header", "chat-ws-page-head");
    head.appendChild(wsNode("span", "chat-ws-eyebrow", eyebrow));
    head.appendChild(wsNode("h2", "", title));
    if (description) head.appendChild(wsNode("p", "", description));
    body.appendChild(head);
  }

  /** One toggle shape, used for every foldable section on the agent page. */
  function wsFold(body, title, hint, action, fill) {
    const fold = wsNode("details", "chat-ws-disclosure chat-ws-fold");
    const summary = wsNode("summary", "");
    const copy = wsNode("span", "");
    copy.append(wsNode("b", "", title), wsNode("small", "", hint));
    const state = wsNode("span", "chat-ws-disclosure-state", action);
    summary.append(copy, state);
    const inner = wsNode("div", "chat-ws-disclosure-body chat-ws-fold-body");
    inner.tabIndex = 0;
    inner.setAttribute("role", "region");
    inner.setAttribute("aria-label", `${title} details`);
    fold.append(summary, inner);
    body.appendChild(fold);
    fill(inner);
    fold.addEventListener("toggle", () => {
      state.textContent = fold.open ? (action === "Edit" ? "Close" : "Hide") : action;
    });
    return fold;
  }

  function wsOverview(body) {
    const model = workspace;
    const compactSwitcher = wsNode("label", "chat-ws-compact-agent-switcher");
    compactSwitcher.appendChild(wsNode("span", "", "Assistant"));
    const compactSelect = document.createElement("select");
    compactSelect.setAttribute("aria-label", "Active assistant");
    for (const agent of [...agents, PLAIN_CHAT]) {
      const option = document.createElement("option");
      option.value = agent.name;
      option.textContent = agent.title;
      compactSelect.appendChild(option);
    }
    compactSelect.value = currentAgent;
    compactSelect.addEventListener("change", () => {
      switchAgent(compactSelect.value, { workspaceFocus: "agent-select" });
    });
    compactSwitcher.appendChild(compactSelect);
    body.appendChild(compactSwitcher);
    wsPageHeading(
      body,
      model.kind === "custom" ? "Custom assistant" : "Active assistant",
      model.title,
      model.summary || model.description,
    );

    // One strip of marks instead of six labelled tiles: the icon carries the
    // category, the tooltip and the accessible name carry the words.
    const facts = wsNode("div", "chat-ws-facts");
    const rounds = model.limits.max_tool_rounds;
    const timeout = model.limits.timeout_s;
    for (const [icon, label, value, tone] of [
      [I.tools, "Tools available", String(model.tools.length), ""],
      [I.file, "Project content", model.usesFiles ? `${model.fileGroups.total}` : "off", ""],
      [I.cube, "3D viewer", model.viewer ? "connected" : "off", model.viewer ? "ok" : ""],
      [
        I.notes,
        model.canWriteModel
          ? "Model changes land in "
            + ((model.writePolicy?.property_sets || []).join(" and ")
              || "reserved IfcConsole_AI_ property sets")
            + " with a provenance record, and never on disk: only you can save"
          : "Model changes",
        model.canWriteModel ? "preview only" : "read-only",
        model.canWriteModel ? "warn" : "ok",
      ],
      [I.pipeline, "Tool rounds per answer", rounds ? String(rounds) : "", ""],
      [I.refresh, "Answer timeout", timeout ? `${timeout}s` : "", ""],
    ]) {
      if (!value) continue;
      const fact = wsNode("div", `chat-ws-fact ${tone}`.trim());
      fact.title = label.includes(" ") && label.length > 24 ? label : `${label}: ${value}`;
      fact.setAttribute("aria-label", fact.title);
      const mark = wsNode("i", "");
      mark.innerHTML = icon;
      fact.append(mark, wsNode("b", "", value));
      facts.appendChild(fact);
    }
    body.appendChild(facts);

    // Everything below is something you go and open.
    const examples = model.suggestedQuestions;
    if (examples.length) {
      wsFold(
        body,
        "Suggested questions",
        `${examples.length} example prompt${examples.length === 1 ? "" : "s"}`,
        "Show",
        (inner) => {
          const list = wsNode("div", "chat-ws-examples");
          for (const example of examples) {
            const card = wsNode("button", "chat-ws-example t-press");
            card.type = "button";
            card.title = [example.prompt, example.note].filter(Boolean).join("\n");
            const copy = wsNode("span", "chat-ws-example-copy");
            copy.appendChild(wsNode("small", "", example.title));
            copy.appendChild(wsNode("b", "", example.prompt));
            const use = wsNode("span", "chat-ws-example-use");
            use.innerHTML = I.send;
            card.append(copy, use);
            card.addEventListener("click", () => {
              input.value = example.prompt;
              grow();
              closeWorkspace();
              input.focus();
            });
            list.appendChild(card);
          }
          inner.appendChild(list);
        },
      );
    }

    wsPipeline(body);

    wsFold(
      body,
      "Instructions",
      "Project-specific method and output format",
      "Edit",
      (inner) => {
        wsInstructions(inner);
        if (model.role) {
          const guardrails = wsNode("details", "chat-ws-nested-disclosure");
          guardrails.append(
            wsNode("summary", "", "Built-in role and guardrails"),
            wsNode("pre", "", model.role),
          );
          inner.appendChild(guardrails);
        }
      },
    );

    if (!model.builtin && model.name) {
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

  // A pipeline belongs to one agent: the stages it can reach depend on the
  // blocks it holds. Kept as its own nav page it read as a property of the
  // panel, and comparing two agents meant leaving and coming back.
  function wsPipeline(body) {
    const model = workspace;
    const strategy = model.workflow?.strategy || "adaptive";
    const strategyName = strategy.replaceAll("-", " ");
    const strategyLabel = strategyName.charAt(0).toUpperCase() + strategyName.slice(1);
    const available = model.reachableStages.length;
    const total = model.stages.length;
    const stageSummary = available === total
      ? `${total} stages available`
      : `${available} of ${total} stages available`;
    let pipeline = null;
    wsFold(
      body,
      "How it works",
      `${strategyLabel} path · ${stageSummary}`,
      "Show",
      (inner) => {
        inner.appendChild(wsNode(
          "p",
          "chat-ws-lead",
          "For each question, the assistant uses only the stages it needs. It scopes the task,"
            + " gathers evidence, chooses a method, verifies the result, and prepares reviewable"
            + " proposals. Safety policy checks every tool call.",
        ));
        pipeline = wsNode("div", "chat-ws-pipeline chat-ws-pipeline-detail");
        inner.appendChild(pipeline);
      },
    );
    model.stages.forEach((stage, index) => {
      const step = wsNode("details", "chat-ws-step" + (stage.available ? "" : " off"));
      const stepSummary = wsNode("summary", "");
      stepSummary.appendChild(
        wsNode("span", "chat-ws-step-number", String(index + 1).padStart(2, "0")),
      );
      const stepCopy = wsNode("span", "chat-ws-step-copy");
      stepCopy.append(wsNode("b", "", stage.label), wsNode("small", "", stage.hint));
      stepSummary.append(stepCopy, wsNode(
        "span",
        "chat-ws-step-tools",
        stage.available ? `${stage.tools.length} tools` : "not available",
      ));
      const detail = wsNode("div", "chat-ws-step-detail");
      detail.appendChild(wsNode(
        "p",
        "",
        stage.available
          ? `The assistant may use these ${stage.label.toLowerCase()} tools when the question calls for them.`
          : "This stage has no available tools in the current installation.",
      ));
      const tools = wsNode("div", "chat-ws-code-list");
      for (const name of stage.tools || []) tools.appendChild(wsNode("code", "", name));
      detail.appendChild(tools);
      step.append(stepSummary, detail);
      pipeline.appendChild(step);
    });
  }

  function wsCapabilities(body) {
    const model = workspace;
    wsPageHeading(
      body,
      "Capabilities",
      `What ${model.title} knows how to do`,
      "Open a capability to see its purpose, included tools, and anything unavailable in this viewer.",
    );
    if (!model.blocks.length) {
      body.appendChild(wsNode("p", "chat-ws-lead", "This assistant has no capability blocks."));
      return;
    }
    const list = wsNode("div", "chat-ws-disclosures");
    for (const block of model.blocks) {
      const details = wsNode("details", "chat-ws-disclosure" + (block.available ? "" : " off"));
      const summary = wsNode("summary", "");
      const copy = wsNode("span", "");
      copy.append(wsNode("b", "", block.title), wsNode("small", "", block.description));
      summary.append(copy, wsNode(
        "span",
        "chat-ws-disclosure-state",
        block.available ? `${block.tools.length} tools` : "unavailable",
      ));
      const detail = wsNode("div", "chat-ws-disclosure-body");
      detail.appendChild(wsNode("p", "", block.description));
      const chips = wsNode("div", "chat-ws-tags");
      for (const feature of block.features || []) chips.appendChild(wsNode("span", "chat-ws-tag", feature));
      if (block.viewer_only) chips.appendChild(wsNode("span", "chat-ws-tag", "needs 3D viewer"));
      if (block.advanced) chips.appendChild(wsNode("span", "chat-ws-tag", "advanced"));
      detail.appendChild(chips);
      if (block.tools?.length) {
        detail.appendChild(wsNode("h3", "", "Included tools"));
        const tools = wsNode("div", "chat-ws-code-list");
        for (const name of block.tools) tools.appendChild(wsNode("code", "", name));
        detail.appendChild(tools);
      }
      if (block.missing?.length) {
        detail.appendChild(wsNode("h3", "", "Unavailable tools"));
        const missing = wsNode("div", "chat-ws-code-list off");
        for (const name of block.missing) missing.appendChild(wsNode("code", "", name));
        detail.appendChild(missing);
      }
      details.append(summary, detail);
      list.appendChild(details);
    }
    body.appendChild(list);
  }

  async function importSkillFiles(files) {
    let failure = "";
    for (const file of files) {
      try {
        const response = await api(
          `/api/agents/skills/import?name=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file },
        );
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      } catch (exc) {
        failure = `${file.name}: ${exc.message || exc}`;
        break;
      }
    }
    await loadWorkspace({ force: true });
    if (failure) {
      // set after the reload, which clears the field it reports through
      workspaceError = failure;
      workspace = null;
      renderWorkspace();
    }
  }

  function wsSkills(body) {
    const model = workspace;
    const count = model.skills.length;
    wsPageHeading(
      body,
      "Skills",
      count ? `${count} saved procedure${count === 1 ? "" : "s"}` : "Saved procedures",
      "Reusable measurement procedures stored as markdown in "
        + ".ifc-console/agents/skills. Agents check them at the start of a task "
        + "and follow the one that matches.",
    );
    const importRow = wsNode("div", "chat-ws-skill-import");
    const importBtn = wsNode("button", "chat-btn t-press", "Import .md skills");
    importBtn.type = "button";
    const picker = document.createElement("input");
    picker.type = "file";
    picker.accept = ".md,.markdown";
    picker.multiple = true;
    picker.hidden = true;
    picker.addEventListener("change", () => {
      const files = [...picker.files];
      picker.value = "";
      if (files.length) void importSkillFiles(files);
    });
    importBtn.addEventListener("click", () => picker.click());
    importRow.append(importBtn, picker);
    importRow.appendChild(wsNode(
      "small",
      "chat-ws-muted",
      "Write skills anywhere (any LLM, any editor) and drop the markdown here.",
    ));
    body.appendChild(importRow);
    if (!count) {
      body.appendChild(wsNode(
        "p",
        "chat-ws-lead",
        "No skills yet. After an agent solves a novel measurement well, ask it "
          + "to record the method with save_agent_skill, import .md files here, "
          + "or drop them into the skills folder yourself.",
      ));
      return;
    }
    const list = wsNode("div", "chat-ws-disclosures");
    for (const skill of model.skills) {
      const details = wsNode("details", "chat-ws-disclosure");
      const summary = wsNode("summary", "");
      const copy = wsNode("span", "");
      copy.append(wsNode("b", "", skill.name), wsNode("small", "", skill.description || ""));
      summary.append(copy, wsNode(
        "span",
        "chat-ws-disclosure-state",
        formatBytes(skill.size_bytes),
      ));
      const detail = wsNode("div", "chat-ws-disclosure-body");
      if (skill.description) detail.appendChild(wsNode("p", "", skill.description));
      const chips = wsNode("div", "chat-ws-tags");
      if (skill.applies_to) chips.appendChild(wsNode("span", "chat-ws-tag", skill.applies_to));
      if (skill.updated_at) {
        chips.appendChild(wsNode("span", "chat-ws-tag", `updated ${String(skill.updated_at).slice(0, 10)}`));
      }
      detail.appendChild(chips);
      const where = wsNode("div", "chat-ws-code-list");
      where.appendChild(wsNode("code", "", skill.path));
      detail.appendChild(where);
      details.append(summary, detail);
      list.appendChild(details);
    }
    body.appendChild(list);
  }

  function schemaType(schema) {
    if (!schema || typeof schema !== "object") return "any";
    if (typeof schema.$ref === "string") return schema.$ref.split("/").at(-1) || "object";
    if (Array.isArray(schema.type)) return schema.type.join(" | ");
    if (schema.type) return String(schema.type);
    const variants = schema.anyOf || schema.oneOf;
    if (Array.isArray(variants)) {
      return variants.map((item) => schemaType(item)).filter(Boolean).join(" | ") || "any";
    }
    if (schema.enum) return "enum";
    return schema.properties ? "object" : "any";
  }

  function wsToolArguments(tool) {
    const schema = tool.input_schema && typeof tool.input_schema === "object"
      ? tool.input_schema
      : {};
    const properties = schema.properties && typeof schema.properties === "object"
      ? schema.properties
      : {};
    const required = new Set(Array.isArray(schema.required) ? schema.required : []);
    const wrap = wsNode("div", "chat-ws-tool-arguments");
    wrap.appendChild(wsNode("h3", "", "Arguments"));
    if (!Object.keys(properties).length) {
      wrap.appendChild(wsNode("p", "chat-ws-muted", "No arguments."));
      return wrap;
    }
    for (const [name, definition] of Object.entries(properties)) {
      const row = wsNode("div", "chat-ws-argument");
      const head = wsNode("div", "chat-ws-argument-head");
      head.append(
        wsNode("code", "", name),
        wsNode("span", "chat-ws-tag", schemaType(definition)),
        wsNode("span", `chat-ws-tag ${required.has(name) ? "required" : ""}`.trim(), required.has(name) ? "required" : "optional"),
      );
      row.appendChild(head);
      if (definition?.description) row.appendChild(wsNode("p", "", definition.description));
      if (Array.isArray(definition?.enum)) {
        row.appendChild(wsNode("small", "", `Allowed: ${definition.enum.map(String).join(", ")}`));
      }
      if (definition && Object.prototype.hasOwnProperty.call(definition, "default")) {
        row.appendChild(wsNode("small", "", `Default: ${JSON.stringify(definition.default)}`));
      }
      wrap.appendChild(row);
    }
    return wrap;
  }

  function wsToolRow(tool) {
    const row = wsNode("details", "chat-ws-tool" + (tool.writes_model ? " writes" : ""));
    const head = wsNode("summary", "chat-ws-tool-head");
    head.appendChild(wsNode("code", "", tool.name));
    if (tool.writes_model) head.appendChild(wsNode("span", "chat-ws-tag write", "preview"));
    if (tool.requires_approval) {
      head.appendChild(wsNode("span", "chat-ws-tag approval", "approval"));
    }
    const detail = wsNode("div", "chat-ws-tool-detail");
    row.append(head, detail);
    // An agent holds up to fifty tools and each argument schema is a dozen
    // nodes. Building them all to show a name list costs a visible pause, so
    // a row fills itself the first time it is opened.
    row.addEventListener("toggle", () => {
      if (!row.open || detail.childElementCount) return;
      detail.appendChild(wsNode("p", "", tool.description || tool.summary));
      const meta = wsNode("div", "chat-ws-tags");
      if (tool.stage_label) meta.appendChild(wsNode("span", "chat-ws-tag", tool.stage_label));
      if (tool.source) meta.appendChild(wsNode("span", "chat-ws-tag", tool.source));
      for (const capability of tool.required_capabilities || []) {
        meta.appendChild(wsNode("span", "chat-ws-tag", capability));
      }
      for (const tag of tool.tags || []) meta.appendChild(wsNode("span", "chat-ws-tag", tag));
      detail.append(meta, wsToolArguments(tool));
    });
    return row;
  }

  function wsTools(body) {
    const model = workspace;
    wsPageHeading(
      body,
      "Tools",
      `${model.tools.length} tools available now`,
      "Rows stay compact until you open one. Expanded details come directly from the tool contract the assistant receives.",
    );
    const controls = wsNode("div", "chat-content-controls");
    const search = wsNode("label", "chat-content-search");
    search.innerHTML = I.search;
    const query = document.createElement("input");
    query.type = "search";
    query.value = toolSearch;
    query.placeholder = "Filter tools";
    query.setAttribute("aria-label", "Filter tools by name or description");
    search.appendChild(query);
    const count = wsNode("span", "chat-content-all", "");
    controls.append(search, count);
    body.appendChild(controls);

    const results = wsNode("div", "chat-ws-tool-results");
    body.appendChild(results);

    const matches = (tool, needle) =>
      !needle
      || `${tool.name || ""} ${tool.summary || ""} ${tool.description || ""}`
        .toLowerCase()
        .includes(needle);

    const drawTools = () => {
      results.innerHTML = "";
      const needle = query.value.trim().toLowerCase();
      let shown = 0;
      for (const group of model.stageGroups) {
        const tools = group.tools.filter((tool) => matches(tool, needle));
        if (!tools.length) continue;
        shown += tools.length;
        results.appendChild(wsNode("div", "chat-ws-section", group.label));
        const list = wsNode("div", "chat-ws-tools");
        for (const tool of tools) list.appendChild(wsToolRow(tool));
        results.appendChild(list);
      }
      const missing = model.unavailable.filter(
        (name) => !needle || String(name).toLowerCase().includes(needle),
      );
      if (missing.length) {
        results.appendChild(wsNode("div", "chat-ws-section", "Not available here"));
        const list = wsNode("div", "chat-ws-tools");
        for (const name of missing) {
          const row = wsNode("div", "chat-ws-tool off");
          row.appendChild(wsNode("code", "", name));
          list.appendChild(row);
        }
        results.appendChild(list);
      }
      if (!shown && !missing.length) {
        results.appendChild(wsNode("p", "chat-ws-lead", "No tool matches this filter."));
      }
      count.textContent = needle ? `${shown} of ${model.tools.length}` : "";
    };

    query.addEventListener("input", () => {
      toolSearch = query.value;
      drawTools();
    });
    drawTools();
  }

  function contentRows() {
    if (!contentLibrary) return [];
    if (Array.isArray(contentLibrary.files)) return contentLibrary.files;
    if (Array.isArray(contentLibrary.content)) return contentLibrary.content;
    return [];
  }

  function resetContentLibrary() {
    contentLibrary = null;
    contentLibraryAgent = "";
    contentLibraryError = "";
    contentLibraryLoadingAgent = "";
    contentLibraryRequest += 1;
  }

  function contentAccess() {
    const raw = contentLibrary?.access;
    if (!raw || typeof raw !== "object") return { mode: "all", paths: [] };
    const paths = Array.isArray(raw.paths)
      ? raw.paths
      : Array.isArray(raw.selected_paths)
        ? raw.selected_paths
        : [];
    return { mode: raw.mode === "selected" ? "selected" : "all", paths };
  }

  // The workspace payload already carries the library and this agent's access
  // in the exact shape /api/agents/content returns, so opening Content can
  // paint from it instead of waiting on a second round trip.
  function seedContentFromWorkspace() {
    const agent = currentAgent;
    if (contentLibrary && contentLibraryAgent === agent) return true;
    const content = workspace && workspace.name === agent ? workspace.content : null;
    if (!content?.enabled || !Array.isArray(content.files)) return false;
    contentLibrary = {
      files: content.files,
      access: content.access,
      usable: content.usable,
    };
    contentLibraryAgent = agent;
    contentLibraryError = "";
    return true;
  }

  async function loadContentLibrary({ force = false } = {}) {
    const agent = currentAgent;
    if (contentLibraryLoadingAgent === agent) return;
    if (!force && seedContentFromWorkspace()) {
      renderContentWorkspace();
      return;
    }
    if (!force && contentLibrary && contentLibraryAgent === agent) {
      renderContentWorkspace();
      return;
    }
    const ticket = ++contentLibraryRequest;
    contentLibraryLoadingAgent = agent;
    contentLibraryError = "";
    renderContentWorkspace();
    try {
      const query = agent ? `?agent=${encodeURIComponent(agent)}` : "";
      const response = await api(`/api/agents/content${query}`);
      const payload = await response.json();
      if (ticket !== contentLibraryRequest || agent !== currentAgent) return;
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      contentLibrary = payload;
      contentLibraryAgent = agent;
    } catch (exc) {
      if (ticket !== contentLibraryRequest || agent !== currentAgent) return;
      contentLibrary = null;
      contentLibraryAgent = "";
      contentLibraryError = exc.message || String(exc);
    } finally {
      if (ticket === contentLibraryRequest) contentLibraryLoadingAgent = "";
    }
    if (ticket === contentLibraryRequest && agent === currentAgent) renderContentWorkspace();
  }

  function updateContentAccessDraft(agent, mode, paths) {
    if (agent !== currentAgent || contentLibraryAgent !== agent || !contentLibrary) return;
    const selected = new Set(paths);
    contentLibrary = {
      ...contentLibrary,
      access: { mode, paths: [...selected] },
      files: contentRows().map((file) => ({
        ...file,
        allowed: mode === "all" || selected.has(String(file.path || "")),
      })),
    };
    contentLibraryError = "";
    renderContentWorkspace();
  }

  function saveContentAccess(mode, paths) {
    const agent = currentAgent;
    if (!agent) return Promise.resolve();
    const cleanPaths = [...new Set(paths.map(String).filter(Boolean))];
    const revision = (contentAccessRevisions.get(agent) || 0) + 1;
    contentAccessRevisions.set(agent, revision);
    updateContentAccessDraft(agent, mode, cleanPaths);

    const previous = contentAccessQueues.get(agent) || Promise.resolve();
    const task = previous.catch(() => {}).then(async () => {
      if (contentAccessRevisions.get(agent) !== revision) return;
      const response = await postJSON("/api/agents/content/access", {
        agent,
        mode,
        paths: cleanPaths,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      if (contentAccessRevisions.get(agent) !== revision || currentAgent !== agent) return;
      contentLibrary = payload.files || payload.content
        ? payload
        : { ...(contentLibrary || {}), access: payload.access || { mode, paths: cleanPaths } };
      contentLibraryAgent = agent;
      await loadWorkspace({ force: true });
      renderContentWorkspace();
    }).catch((exc) => {
      if (contentAccessRevisions.get(agent) !== revision || currentAgent !== agent) return;
      contentLibraryError = exc.message || String(exc);
      renderContentWorkspace();
    }).finally(() => {
      if (contentAccessQueues.get(agent) === task) contentAccessQueues.delete(agent);
    });
    contentAccessQueues.set(agent, task);
    return task;
  }

  async function uploadWorkspaceContent(files) {
    for (const file of files) {
      contentLibraryError = `Adding ${file.name}...`;
      renderContentWorkspace();
      try {
        const response = await api(
          `/api/agents/content/upload?name=${encodeURIComponent(file.name)}`,
          { method: "POST", body: file },
        );
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
      } catch (exc) {
        contentLibraryError = `${file.name}: ${exc.message || exc}`;
        renderContentWorkspace();
        return;
      }
    }
    contentLibrary = null;
    contentLibraryError = "";
    await loadContentLibrary({ force: true });
  }

  function contentFileRow(file, access, selectedPaths, canAssign, onRange) {
    const row = wsNode("label", "chat-content-row");
    const path = String(file.path || "");
    row.dataset.path = path;
    const allowed = file.allowed === undefined
      ? access.mode === "all" || selectedPaths.has(path)
      : Boolean(file.allowed);
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = allowed;
    box.disabled = !canAssign;
    box.setAttribute("aria-label", `Allow ${agentTitle()} to use ${file.name || path}`);
    const icon = wsNode("span", "chat-ws-file-icon");
    icon.innerHTML = file.media === "image" ? I.image : I.file;
    const meta = wsNode("span", "chat-content-meta");
    meta.appendChild(wsNode("b", "", file.name || path.split(/[\\/]/).pop() || "File"));
    meta.appendChild(
      wsNode(
        "small",
        "",
        `${formatBytes(file.size_bytes)} · ${file.indexed === false ? "not indexed" : "ready"}`,
      ),
    );
    row.append(box, icon, meta);
    // Shift-click extends from the last box touched, the way a file list is
    // expected to behave. Granting twenty manuals one click at a time is
    // twenty round trips to the server.
    box.addEventListener("click", (event) => {
      if (event.shiftKey && onRange?.(path, box.checked)) event.preventDefault();
    });
    box.addEventListener("change", () => {
      const latest = contentAccess();
      const paths = new Set(
        latest.mode === "all"
          ? contentRows().map((item) => String(item.path || ""))
          : latest.paths,
      );
      if (box.checked) paths.add(path);
      else paths.delete(path);
      void saveContentAccess("selected", [...paths].filter(Boolean));
    });
    return row;
  }

  function renderContentWorkspace() {
    const body = el("content-body");
    if (!body || workspaceView !== "content") return;
    body.innerHTML = "";
    const head = wsNode("div", "chat-content-head");
    const copy = wsNode("div", "chat-content-copy");
    copy.appendChild(wsNode("b", "", "Workspace content"));
    copy.appendChild(
      wsNode(
        "small",
        "",
        `Upload once, then choose what ${agentTitle()} may use in every conversation.`,
      ),
    );
    const add = wsNode("button", "chat-btn primary t-press", "Add files");
    add.type = "button";
    add.addEventListener("click", () => el("content-file").click());
    head.append(copy, add);
    body.appendChild(head);

    if (contentLibraryAgent !== currentAgent) contentLibrary = null;
    if (!contentLibrary && !contentLibraryError) seedContentFromWorkspace();
    if (!contentLibrary && !contentLibraryError) {
      const state = wsNode("div", "chat-ws-state loading");
      state.appendChild(wsNode("i", ""));
      state.appendChild(wsNode("span", "", "Reading workspace content..."));
      body.appendChild(state);
      void loadContentLibrary();
      return;
    }
    if (contentLibraryError) {
      const status = wsNode("div", "chat-content-status");
      status.classList.toggle("bad", !contentLibraryError.startsWith("Adding "));
      status.textContent = contentLibraryError;
      body.appendChild(status);
    }
    if (!contentLibrary) return;

    const files = contentRows();
    const access = contentAccess();
    const selectedPaths = new Set(access.paths.map(String));
    const canAssign = Boolean(currentAgent && (pack()?.features || []).includes("files"));
    const controls = wsNode("div", "chat-content-controls");
    const search = wsNode("label", "chat-content-search");
    search.innerHTML = I.search;
    const query = document.createElement("input");
    query.type = "search";
    query.value = contentSearch;
    query.placeholder = "Search content";
    query.setAttribute("aria-label", "Search workspace content");
    search.appendChild(query);
    const all = wsNode("label", "chat-content-all");
    const allBox = document.createElement("input");
    allBox.type = "checkbox";
    allBox.checked = access.mode === "all";
    allBox.disabled = !canAssign;
    all.append(allBox, document.createTextNode(` All for ${agentTitle()}`));
    controls.append(search, all);
    body.appendChild(controls);

    // Bulk actions operate on what the filter is showing, so "search then
    // grant" is one gesture rather than one click per manual.
    let shown = files;
    const bulk = wsNode("div", "chat-content-bulk");
    const tally = wsNode("span", "chat-content-tally", "");
    const bulkButton = (label, title, grant) => {
      const button = wsNode("button", "chat-content-bulk-action t-press", label);
      button.type = "button";
      button.title = title;
      button.addEventListener("click", () => {
        const latest = contentAccess();
        const paths = new Set(
          latest.mode === "all" ? files.map((file) => String(file.path || "")) : latest.paths,
        );
        for (const file of shown) {
          const path = String(file.path || "");
          if (!path) continue;
          if (grant) paths.add(path);
          else paths.delete(path);
        }
        void saveContentAccess("selected", [...paths].filter(Boolean));
      });
      return button;
    };
    const grantShown = bulkButton("Select shown", "Grant every file the filter is showing", true);
    const revokeShown = bulkButton("Clear shown", "Remove every file the filter is showing", false);
    bulk.append(tally, grantShown, revokeShown);
    body.appendChild(bulk);

    const list = wsNode("div", "chat-content-list");
    // Anchor for shift-click, kept across redraws of the same list.
    let anchorPath = "";
    const extendRange = (path, wasChecked) => {
      if (!canAssign || !anchorPath || anchorPath === path) return false;
      const order = shown.map((file) => String(file.path || ""));
      const from = order.indexOf(anchorPath);
      const to = order.indexOf(path);
      if (from < 0 || to < 0) return false;
      const span = order.slice(Math.min(from, to), Math.max(from, to) + 1);
      const latest = contentAccess();
      const paths = new Set(
        latest.mode === "all" ? files.map((file) => String(file.path || "")) : latest.paths,
      );
      // The click that started this is prevented, so `wasChecked` is still
      // the state before it: extending means moving the span to the opposite.
      for (const item of span) {
        if (wasChecked) paths.delete(item);
        else paths.add(item);
      }
      void saveContentAccess("selected", [...paths].filter(Boolean));
      return true;
    };

    const drawRows = () => {
      list.innerHTML = "";
      const needle = query.value.trim().toLowerCase();
      shown = files.filter((file) =>
        !needle || `${file.name || ""} ${file.path || ""}`.toLowerCase().includes(needle)
      );
      const granted = shown.filter((file) =>
        file.allowed === undefined
          ? access.mode === "all" || selectedPaths.has(String(file.path || ""))
          : Boolean(file.allowed),
      ).length;
      tally.textContent = shown.length
        ? `${granted} of ${shown.length} available to ${agentTitle()}`
        : "";
      grantShown.disabled = !canAssign || !shown.length || granted === shown.length;
      revokeShown.disabled = !canAssign || !shown.length || granted === 0;
      if (!shown.length) {
        list.appendChild(
          wsNode(
            "p",
            "chat-ws-lead",
            files.length ? "No content matches this search." : "No workspace content yet.",
          ),
        );
        return;
      }
      for (const file of shown) {
        const row = contentFileRow(file, access, selectedPaths, canAssign, extendRange);
        row.querySelector("input")?.addEventListener("mousedown", (event) => {
          if (!event.shiftKey) anchorPath = String(file.path || "");
        });
        list.appendChild(row);
      }
    };
    query.addEventListener("input", () => {
      contentSearch = query.value;
      drawRows();
    });
    allBox.addEventListener("change", () => {
      const latest = contentAccess();
      void saveContentAccess(
        allBox.checked ? "all" : "selected",
        allBox.checked
          ? []
          : latest.mode === "all"
            ? files.map((file) => file.path)
            : latest.paths,
      );
    });
    body.appendChild(list);
    drawRows();
    if (!canAssign) {
      body.appendChild(
        wsNode(
          "p",
          "chat-content-footnote",
          currentAgent
            ? `${agentTitle()} does not use document content. Edit its capabilities to enable files.`
            : "Choose an assistant to configure persistent content access.",
        ),
      );
    }
  }

  function wsInstructions(body) {
    body.appendChild(
      wsNode(
        "p",
        "chat-ws-lead",
        "Standing instructions for this assistant only, stored in this browser. " +
          "Provider, model, and API key live under Models."
      )
    );
    const field = wsNode("div", "chat-field");
    const label = wsNode("label", "", "Standing instructions");
    label.setAttribute("for", "chat-ws-instructions");
    field.appendChild(label);
    const area = document.createElement("textarea");
    area.id = "chat-ws-instructions";
    area.rows = 10;
    area.placeholder =
      "How to calculate a property, which document defines it, the output format you want...";
    const original = el("system").value;
    area.value = original;
    field.appendChild(area);
    field.appendChild(
      wsNode(
        "p",
        "chat-help",
        "Added to this assistant's system prompt. The block safety, evidence, " +
          "and approval rules always stay above it."
      )
    );
    const status = wsNode("p", "chat-ws-instruction-state", "No unsaved changes");
    const actions = wsNode("div", "chat-ws-instruction-actions");
    const cancel = wsNode("button", "chat-btn t-press", "Cancel");
    const save = wsNode("button", "chat-btn primary t-press", "Save instructions");
    cancel.type = "button";
    save.type = "button";
    cancel.disabled = true;
    save.disabled = true;
    const syncDirty = () => {
      const dirty = area.value !== original;
      cancel.disabled = !dirty;
      save.disabled = !dirty;
      status.textContent = dirty
        ? "Unsaved changes. Saving starts a fresh assistant context."
        : "No unsaved changes";
      status.classList.toggle("dirty", dirty);
    };
    area.addEventListener("input", syncDirty);
    cancel.addEventListener("click", () => {
      area.value = original;
      syncDirty();
      area.focus();
    });
    save.addEventListener("click", () => {
      el("system").value = area.value;
      saveSettings();
      forkConversationForConfigurationChange(
        "Standing instructions saved. A fresh conversation is ready.",
      );
      loadWorkspace({ force: true });
    });
    actions.append(cancel, save);
    field.append(status, actions);
    body.appendChild(field);
  }

  function renderWorkspace() {
    const body = el("ws-body");
    renderWorkspaceAgentList();
    body.innerHTML = "";
    if (!workspace) {
      body.removeAttribute("aria-labelledby");
      el("ws-title").textContent = workspaceError ? "Inspector unavailable" : "Loading...";
      el("ws-avatar").textContent = workspaceError ? "!" : "...";
      el("ws-reach").textContent = "";
      if (workspaceError) {
        const state = wsNode("div", "chat-ws-state bad");
        state.appendChild(wsNode("b", "", "Assistant details could not be loaded"));
        state.appendChild(wsNode("p", "", workspaceError));
        const retry = wsNode("button", "chat-btn primary t-press", "Try again");
        retry.type = "button";
        retry.addEventListener("click", () => loadWorkspace({ force: true }));
        state.appendChild(retry);
        body.appendChild(state);
      } else {
        const state = wsNode("div", "chat-ws-state loading");
        state.appendChild(wsNode("i", ""));
        state.appendChild(wsNode("span", "", "Reading tools, files, and policy..."));
        body.appendChild(state);
      }
      return;
    }
    el("ws-title").textContent = workspace.title;
    el("ws-avatar").textContent = initialsOf(workspace.title);
    el("ws-reach").textContent = reachSentence(workspace);
    act("studio-current").textContent = workspace.builtin ? "Duplicate agent" : "Edit agent";
    act("studio-current").hidden = workspace.plain;
    const draw = {
      agent: wsOverview,
      capabilities: wsCapabilities,
      tools: wsTools,
      skills: wsSkills,
    }[workspaceView] || wsOverview;
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
      ? "Choose a provider and a model."
      : !chosenModel()
        ? `Choose a model for ${selectedProvider.label}.`
        : !hasKey(selectedProvider)
          ? `Add the API key required by ${selectedProvider.label}.`
          : "Finish the model setup.";
    const body = ready
      ? `<p class="chat-empty-lead">${esc(lead)}</p>
         <div class="chat-starters" aria-label="Suggested questions">
           ${starters.map((s) =>
             `<button class="chat-starter"><span>${esc(s)}</span>${I.send}</button>`
           ).join("")}
         </div>`
      : `<p class="chat-empty-lead">Connect a model once, then ask grounded questions about the IFC file open in the console.</p>
         <div class="chat-setup">
           <span class="chat-setup-status" aria-hidden="true"></span>
           <div><b>${esc(setupLead)}</b><small>Provider credentials stay in the running console unless you choose secure storage.</small></div>
           <button class="chat-btn primary" data-act="settings"
                   aria-controls="chat-workspace" aria-expanded="false">Open Agent workspace</button>
         </div>`;
    log.innerHTML = `
      <div class="chat-empty">
        <span class="chat-empty-mark" aria-hidden="true">${I.agent}</span>
        <span class="chat-empty-eyebrow">${esc(agentTitle())}</span>
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
    // Switching assistant no longer costs the scoping that led to the switch.
    // The offer is a button, not a hidden prompt prefix, so what crosses is
    // visible and editable before it is sent.
    const carried = carryOffer;
    if (carried) {
      const offer = document.createElement("div");
      offer.className = "chat-carry";
      offer.innerHTML = "<span></span><button type='button' class='chat-btn t-press'></button>";
      offer.querySelector("span").textContent = `Carry the last turns from ${carried.title}?`;
      const take = offer.querySelector("button");
      take.textContent = "Carry the context";
      take.title = "Puts the previous turns in the composer, where you can edit them";
      take.addEventListener("click", () => {
        const draft = input.value.trim();
        input.value = draft ? `${carried.text}\n\n${draft}` : `${carried.text}\n\n`;
        if (carryOffer === carried) carryOffer = null;
        offer.remove();
        grow();
        input.focus();
        input.setSelectionRange(input.value.length, input.value.length);
      });
      log.querySelector(".chat-empty")?.appendChild(offer);
    }
  }

  // The server thread an agent conversation runs in is append-only, so it and
  // a rewound transcript would silently diverge. Plain chat re-sends what is
  // on screen, so there it is simply true.
  const canEditTurns = () => !currentAgent || !conversationThreads[currentConversationId];

  function syncTurnControls() {
    const allowed = canEditTurns();
    for (const button of log.querySelectorAll(".chat-turn-edit")) button.hidden = !allowed;
  }

  /* Put an earlier request back in the composer and drop everything after it.
   *
   * A typo used to cost a full retype plus a wasted agent run. `text` is
   * checked against the turn so a stale button cannot truncate a transcript
   * that has since been reloaded or rewound by another edit.
   */
  function editTurn(index, text) {
    if (busy) {
      note("Stop the current response before editing an earlier message.", true);
      return;
    }
    if (turns[index]?.role !== "user" || turns[index].text !== text) {
      note("That message has moved. Reopen the conversation and try again.", true);
      return;
    }
    if (!canEditTurns()) {
      note("This assistant keeps its history on the console, so earlier messages cannot be rewritten yet.", true);
      return;
    }
    pendingAttachments = (turns[index].attachments || []).filter((item) => item.path);
    turns.length = index;
    input.value = text;
    paintTranscript();
    renderAttachments();
    render();
    saveHistory();
    grow();
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  }

  function addUser(text, attachments = [], { animate = true, index = turns.length } = {}) {
    if (!turns.length) log.innerHTML = "";
    const div = document.createElement("div");
    div.className = "chat-msg user" + (animate ? " is-new" : "");
    // The mark alone says who spoke; the name and the word "Request" were the
    // same on every turn. The accessible name still carries it.
    const head = document.createElement("div");
    head.className = "chat-turn-head user";
    head.innerHTML =
      `<span class="chat-turn-avatar" role="img" aria-label="You" title="You">${I.user}</span>`;
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = text;
    div.append(head, bubble);
    if (attachments.length) {
      const row = document.createElement("div");
      row.className = "chat-user-attachments";
      for (const item of attachments) {
        const chip = document.createElement("span");
        chip.innerHTML = I.file;
        chip.appendChild(document.createTextNode(item.name || item.path));
        row.appendChild(chip);
      }
      div.appendChild(row);
    }
    const tools = document.createElement("div");
    tools.className = "chat-turn-tools";
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "chat-turn-edit";
    edit.textContent = "edit";
    edit.title = "Edit this message and send it again";
    edit.setAttribute("aria-label", "Edit this message and send it again");
    edit.hidden = !canEditTurns();
    edit.addEventListener("click", () => editTurn(index, text));
    tools.append(edit, copyButton(text, "chat-copy chat-turn-copy", "this message"));
    div.appendChild(tools);
    log.appendChild(div);
    scroll();
  }

  function addAssistant({ animate = true } = {}) {
    const div = document.createElement("div");
    div.className = "chat-msg assistant" + (animate ? " is-new" : "");
    div.innerHTML = `
      <div class="chat-turn-head assistant">
        <span class="chat-turn-avatar" role="img" aria-label="${esc(agentTitle())}"
              title="${esc(agentTitle())}">${I.agent}</span>
      </div>
      <div class="chat-body">
        <div class="chat-stream"></div>
        <div class="chat-step" hidden><i></i><span></span></div>
        <div class="chat-stats"></div>
      </div>`;
    log.appendChild(div);
    scroll();
    return {
      root: div,
      stream: div.querySelector(".chat-stream"),
      step: div.querySelector(".chat-step"),
      stepLabel: div.querySelector(".chat-step span"),
      stats: div.querySelector(".chat-stats"),
    };
  }

  // ------------------------------------------------------------------ blocks
  // One node per block, created once and repainted only when its revision
  // changes. This is what puts a tool card exactly where the model called it
  // rather than in a pile above the answer.
  function blockNode(block) {
    if (block.kind === "tool") return toolNode();
    if (block.kind === "proposal") return proposalNode(block.proposal);
    if (block.kind === "approval") return approvalNode();
    if (block.kind === "reasoning") return reasoningNode();
    return document.createElement("div");
  }

  // The run is stopped while this is on screen, so it says what is being
  // asked for and offers one refusal plus two clear approval scopes. Deny is
  // not an error path: a denied call comes back as a refusal the model can use.
  // The readable summary is here, at the moment of the decision, and not on
  // the card the console emits once the call has already run.
  function approvalNode() {
    const card = document.createElement("details");
    card.className = "chat-approval";
    card.innerHTML = `
      <summary class="chat-approval-head">
        <span class="chat-approval-mark">${I.capability}</span>
        <div class="chat-approval-copy">
          <b>Approval needed</b>
          <code></code>
        </div>
        <span class="chat-approval-state"></span>
        <span class="chat-approval-toggle" aria-hidden="true"></span>
      </summary>
      <div class="chat-approval-body">
        <p class="chat-approval-headline"></p>
        <dl class="chat-approval-facts"></dl>
        <div class="chat-approval-caps"></div>
        <details class="chat-approval-args">
          <summary><span class="chat-approval-args-label">Arguments</span></summary>
          <pre tabindex="0"><code></code></pre>
        </details>
        <div class="chat-approval-actions">
          <button type="button" class="chat-btn chat-approval-deny">Deny</button>
          <button type="button" class="chat-btn chat-approval-allow">Approve once</button>
          <button type="button" class="chat-btn primary chat-approval-always">Always allow this tool</button>
        </div>
      </div>`;
    return card;
  }

  // A standing decision is bound to the exact capability set the person read,
  // so a later call to the same tool asking for more still stops and asks.
  const capabilitySignature = (block) =>
    (block.capabilities || []).map(String).sort().join(",");

  async function decideApproval(block, approved, card) {
    const buttons = [...card.querySelectorAll("button")];
    for (const button of buttons) button.disabled = true;
    try {
      const response = await postJSON("/api/agents/approve", {
        request_id: block.requestId,
        approved,
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
    } catch (exc) {
      for (const button of buttons) button.disabled = false;
      note(`Could not send that decision: ${exc.message || exc}`, true);
    }
  }

  function paintApproval(node, block) {
    node.className = `chat-approval ${block.state}`;
    if (node.dataset.approvalState !== block.state) {
      node.open = block.state === "waiting";
      node.dataset.approvalState = block.state;
    }
    node.querySelector(".chat-approval-copy b").textContent =
      block.state === "waiting" ? "Approval needed" : "Approval";
    node.querySelector(".chat-approval-copy code").textContent = block.name;
    const state = node.querySelector(".chat-approval-state");
    const word = block.state === "waiting"
      ? "waiting for you"
      : block.state === "approved" ? "approved" : "denied";
    // Who decided matters most when nobody did: "denied · run stopped" is the
    // difference between a refusal and a card that outlived its run.
    state.textContent = block.decidedBy ? `${word} · ${block.decidedBy}` : word;
    state.title = block.reason || "";
    // What the reviewer is actually allowing, in the words of the change
    // rather than the tool's JSON. The fold below still holds every argument.
    const digest = approvalDigest(block);
    const headline = node.querySelector(".chat-approval-headline");
    headline.textContent = digest.headline;
    headline.hidden = digest.headline === `Run ${digest.name}`;
    const facts = node.querySelector(".chat-approval-facts");
    facts.innerHTML = "";
    for (const fact of digest.facts) {
      const term = document.createElement("dt");
      term.textContent = fact.label;
      const detail = document.createElement("dd");
      detail.textContent = fact.value;
      facts.append(term, detail);
    }
    facts.hidden = !digest.facts.length;
    const caps = node.querySelector(".chat-approval-caps");
    caps.innerHTML = "";
    for (const capability of block.capabilities || []) {
      const tag = document.createElement("code");
      tag.textContent = capability;
      caps.appendChild(tag);
    }
    caps.hidden = !caps.children.length;
    const args = node.querySelector(".chat-approval-args");
    const preview = approvalArgumentPreview(block);
    args.hidden = !preview.text || preview.text === "{}";
    args.classList.toggle("code", preview.code);
    args.querySelector(".chat-approval-args-label").textContent = preview.label;
    if (!args.hidden) args.querySelector("code").textContent = preview.text;
    const actions = node.querySelector(".chat-approval-actions");
    // A restored transcript shows what was decided; the future it answered was
    // resolved long ago, so it carries no live control.
    const live = block.state === "waiting" && Boolean(block.requestId);
    actions.hidden = !live;
    const always = node.querySelector(".chat-approval-always");
    always.title = `Always allow ${block.name} in this conversation for these capabilities.`;
    if (live && !node.dataset.wired) {
      node.dataset.wired = "1";
      node.querySelector(".chat-approval-allow").addEventListener("click", () => {
        decideApproval(block, true, node);
      });
      always.addEventListener("click", () => {
        approvalAllowlist.set(block.name, capabilitySignature(block));
        decideApproval(block, true, node);
      });
      node.querySelector(".chat-approval-deny")
        .addEventListener("click", () => decideApproval(block, false, node));
    }
    // The same call again, under a decision already taken. The console still
    // makes the call; only the prompt is skipped.
    if (live && !node.dataset.standing
        && approvalAllowlist.get(block.name) === capabilitySignature(block)) {
      node.dataset.standing = "1";
      state.textContent = "approved · standing decision";
      actions.hidden = true;
      void decideApproval(block, true, node);
    }
  }

  function reasoningNode() {
    const details = document.createElement("details");
    details.className = "chat-think";
    details.innerHTML =
      '<summary><span class="chat-think-label">Thought for a moment</span></summary>' +
      '<div class="chat-think-body"></div>';
    return details;
  }

  function toolNode() {
    const details = document.createElement("details");
    details.className = "chat-tool-card";
    details.innerHTML = `
      <summary>
        <span class="chat-tool-signal" aria-hidden="true"><i></i></span>
        <span class="chat-tool-stage"></span>
        <code class="chat-tool-name"></code>
        <span class="chat-tool-state"></span>
        <progress class="chat-tool-progress" max="1" value="0" hidden></progress>
        <time class="chat-tool-time"></time>
        <i class="chat-tool-caret" aria-hidden="true">${I.chevron}</i>
      </summary>
      <div class="chat-tool-body"></div>`;
    // Large results cost no layout, GUID decoration, or syntax DOM until the
    // reader asks to inspect them. Native details preserves keyboard and
    // screen-reader behaviour without another disclosure component.
    details.addEventListener("toggle", () => {
      if (details.open && details._toolBlock) paintToolBody(details, details._toolBlock);
    });
    return details;
  }

  // The richest source of element ids in a transcript is the tool result the
  // answer was written from, so the ids in it are chips too, not dead text.
  function toolPart(title, text, { sourceCode = false } = {}) {
    const part = document.createElement("div");
    part.className = "chat-tool-part";
    part.classList.toggle("code", sourceCode);
    const label = document.createElement("b");
    label.textContent = title;
    const pre = document.createElement("pre");
    pre.tabIndex = 0;
    const code = document.createElement("code");
    if (sourceCode) code.textContent = text;
    else guidChipsInto(code, text);
    pre.appendChild(code);
    part.append(label, pre);
    return part;
  }

  function paintToolBody(node, block) {
    const body = node.querySelector(".chat-tool-body");
    // Progress can revise the header many times a second; inputs and results
    // only change when the call changes state.
    if (body.dataset.state === block.state) return;
    body.dataset.state = block.state;
    body.innerHTML = "";
    const input = approvalArgumentPreview(block);
    if (input.text && input.text !== "{}") {
      body.appendChild(toolPart(input.code ? "Code" : "Input", input.text, {
        sourceCode: input.code,
      }));
    }
    const hasFullOutput = block.output !== null && block.output !== undefined;
    const output = pretty(hasFullOutput ? block.output : block.preview);
    if (output) {
      body.appendChild(toolPart(
        block.state === "bad" ? "Error" : hasFullOutput ? "Result" : "Output preview",
        output,
      ));
    }
    if (!body.children.length) {
      const blank = document.createElement("p");
      blank.className = "chat-tool-blank";
      blank.textContent = block.state === "running"
        ? "Waiting for the console..."
        : "The tool returned nothing to show.";
      body.appendChild(blank);
    }
    addCodeCopies(body);
  }

  function paintTool(node, block) {
    const stage = block.stage >= 0 ? stageLabel(block.stage) : "Tool";
    node._toolBlock = block;
    node.className = `chat-tool-card ${block.state}`;
    node.querySelector(".chat-tool-stage").textContent = stage;
    node.querySelector(".chat-tool-name").textContent = block.name;
    const state = node.querySelector(".chat-tool-state");
    state.textContent = toolHeadline(block);
    state.title = block.detail || block.summary || "";
    const progress = node.querySelector(".chat-tool-progress");
    const total = Number(block.progress?.total);
    const showProgress = block.state === "running" && Number.isFinite(total) && total > 0;
    progress.hidden = !showProgress;
    if (showProgress) {
      progress.max = total;
      progress.value = Math.max(0, Math.min(Number(block.progress?.done) || 0, total));
      progress.setAttribute("aria-label", `${progress.value} of ${total}`);
    }
    node.querySelector(".chat-tool-time").textContent = duration(block.ms);
    // The 3D view, reachable from the collapsed row: a result naming forty
    // elements should not cost forty clicks inside the fold.
    const guids = viewerAttached() ? globalIdsIn(block.preview) : [];
    let selectAll = node.querySelector(".chat-tool-select");
    if (guids.length > 1) {
      if (!selectAll) {
        selectAll = document.createElement("button");
        selectAll.type = "button";
        selectAll.className = "chat-tool-select";
        selectAll.addEventListener("click", (event) => {
          // it sits inside the <summary>, which would otherwise fold the card
          event.preventDefault();
          event.stopPropagation();
          selectInViewer(globalIdsIn(block.preview));
        });
        node.querySelector("summary").insertBefore(
          selectAll,
          node.querySelector(".chat-tool-caret"),
        );
      }
      selectAll.textContent = `Select ${guids.length} in 3D`;
      selectAll.title = `Select all ${guids.length} elements this result names`;
      selectAll.hidden = false;
    } else if (selectAll) {
      selectAll.hidden = true;
    }
    // A failure is the one case worth opening on its own: the reader needs the
    // message, not a chevron to find it behind.
    if (block.state === "bad" && !node.dataset.opened) {
      node.open = true;
      node.dataset.opened = "1";
    }
    if (node.open) paintToolBody(node, block);
  }

  function paintBlock(node, block, live) {
    if (block.kind === "tool") {
      paintTool(node, block);
      return;
    }
    if (block.kind === "reasoning") {
      node.querySelector(".chat-think-body").innerHTML = md(block.text.trim());
      const label = node.querySelector(".chat-think-label");
      label.textContent = live ? "Thinking" : "Thought for a moment";
      label.classList.toggle("shimmer", Boolean(live));
      return;
    }
    if (block.kind === "approval") {
      paintApproval(node, block);
      return;
    }
    if (block.kind === "proposal") return;
    node.className = "chat-answer";
    const text = String(block.text ?? "").trim();
    node.innerHTML = md(text) + (live ? '<span class="chat-cursor"></span>' : "");
    if (!live) addCodeCopies(node);
  }

  function syncStream(host, blocks, { live = false } = {}) {
    blocks.forEach((block, index) => {
      let node = host.children[index];
      if (!node) {
        node = blockNode(block);
        host.appendChild(node);
        node.dataset.v = "";
      }
      const isLive = live && index === blocks.length - 1;
      const version = `${block.v ?? 0}:${isLive ? "live" : "done"}`
        + (block.kind === "approval" ? `:${block.state}` : "");
      if (node.dataset.v === version) return;
      node.dataset.v = version;
      paintBlock(node, block, isLive);
    });
  }

  function showStep(view, state) {
    // While text is streaming the cursor already says the run is alive, so the
    // line would be noise. Everywhere else it must be there: the quiet gap
    // between a finished tool and the next token used to look like a hang.
    if (state.blocks.at(-1)?.kind === "text") {
      view.step.hidden = true;
      return;
    }
    // Once a tool has run, the stage is the honest description of what is
    // happening, even while the model is narrating: models routinely talk
    // before and between tool calls, so "is it emitting text" says nothing
    // about what the run is actually doing.
    const waiting = (state.approvals || []).some((item) => item.state === "waiting");
    if (waiting) {
      view.step.hidden = false;
      view.stepLabel.textContent = "Waiting for your approval";
      return;
    }
    const running = state.tools.some((tool) => tool.state === "running");
    const stage = STAGES[state.stage];
    view.step.hidden = false;
    view.stepLabel.textContent = running && stage
      ? STEP_TEXT[stage.id] || stageLabel(state.stage)
      : state.thinking && !state.answering ? "Thinking" : "Working";
  }

  const nearBottom = () => log.scrollHeight - log.scrollTop - log.clientHeight < 140;
  // Proximity is useful for the jump button, but it is not user intent. A
  // small upward wheel/trackpad gesture still leaves the viewport "near" the
  // bottom; treating that as permission to follow the next token made the log
  // pull against the reader. Once they move upward, streaming keeps painting
  // below them and only resumes following when they return to the bottom.
  let followingOutput = true;
  let lastScrollTop = log.scrollTop;
  // Reading back through a long run used to be one-way: nothing offered the
  // way down again, and the answer kept growing out of sight.
  const jumpButton = el("jump");
  const syncJump = () => {
    jumpButton.hidden = nearBottom();
  };
  const scroll = ({ smooth = false } = {}) => {
    followingOutput = true;
    if (smooth) log.scrollTo({ top: log.scrollHeight, behavior: "smooth" });
    else log.scrollTop = log.scrollHeight;
    lastScrollTop = log.scrollTop;
    syncJump();
  };
  // Wheel fires before the browser updates scrollTop, so it can veto a queued
  // stream repaint immediately. The scroll listener covers touch, keyboard,
  // scrollbar, and other scrolling without confusing a tiny upward move with
  // "still at the bottom".
  log.addEventListener("wheel", (event) => {
    if (event.deltaY < 0) followingOutput = false;
  }, { passive: true });
  log.addEventListener("scroll", () => {
    const top = log.scrollTop;
    if (top < lastScrollTop - 0.5) followingOutput = false;
    else if (top > lastScrollTop + 0.5 && nearBottom()) followingOutput = true;
    lastScrollTop = top;
    syncJump();
  }, { passive: true });

  /* True while the reader is holding a selection inside `host`.
   *
   * Repainting a streamed answer replaces its subtree, which collapses any
   * selection made in it. One skipped tick costs 60ms of freshness. */
  function selectionInside(host) {
    const selection = window.getSelection?.();
    if (!selection || selection.isCollapsed || !selection.anchorNode) return false;
    return host.contains(selection.anchorNode);
  }

  // ------------------------------------------------------------------ copying
  async function copyText(text, button) {
    // The resting label comes from the dataset, not from the button: a second
    // click inside the reset window would otherwise capture "copied" and the
    // control would keep that word for good.
    const label = button.dataset.label || button.textContent;
    button.dataset.label = label;
    if (button.dataset.resetTimer) clearTimeout(Number(button.dataset.resetTimer));
    let outcome = "copied";
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      outcome = "copy failed";
    }
    button.textContent = outcome;
    el("announce").textContent = outcome === "copied" ? "Copied to the clipboard." : outcome;
    button.dataset.resetTimer = String(setTimeout(() => {
      button.textContent = label;
      delete button.dataset.resetTimer;
    }, 1200));
  }

  function copyButton(text, className = "chat-copy", describes = "this answer") {
    const button = document.createElement("button");
    button.className = className;
    button.type = "button";
    button.textContent = "copy";
    button.dataset.label = "copy";
    // Every transcript grows several controls that all read "copy"; the label
    // is what tells them apart out of visual context.
    button.setAttribute("aria-label", `Copy ${describes}`);
    button.addEventListener("click", () => copyText(text, button));
    return button;
  }

  function addCodeCopies(scope) {
    for (const pre of scope.querySelectorAll("pre")) {
      if (pre.querySelector(".chat-code-copy")) continue;
      pre.appendChild(copyButton(
        pre.querySelector("code")?.textContent ?? "",
        "chat-code-copy",
        "this code block",
      ));
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
      scope: historyStore.scope,
      agent: currentAgent,
      agent_title: agentTitle(),
      title: first.replace(/\s+/g, " ").trim().slice(0, 68) || "New conversation",
      updated_at: Date.now(),
      thread_id: currentAgent ? (conversationThreads[currentConversationId] || "") : "",
      turns: turns.slice(-HISTORY_LIMIT),
    };
  }

  function saveHistory() {
    if (!settings.history || !turns.length) return;
    historyStore.save(conversationRecord());
    const retained = new Set(historyStore.all().map((record) => record.id));
    for (const id of Object.keys(conversationThreads)) {
      if (id !== currentConversationId && !retained.has(id)) delete conversationThreads[id];
    }
    saveThreads();
    renderSideHistory();
  }

  function paintTurn(turn, { animate = true } = {}) {
    const view = addAssistant({ animate });
    const blocks = turn.blocks?.length
      ? turn.blocks
      : (turn.text ? [{ kind: "text", text: turn.text, v: 1 }] : []);
    if (blocks.length) syncStream(view.stream, blocks);
    if (turn.error) {
      const error = document.createElement("div");
      error.className = "chat-error";
      error.textContent = turn.error;
      view.stream.appendChild(error);
    }
    if (turn.status === "stopped") view.stats.textContent = "stopped";
    else if (turn.status === "error") view.stats.textContent = "failed";
    if (turn.text) view.stats.appendChild(copyButton(turn.text));
    return view;
  }

  function paintTranscript() {
    log.innerHTML = "";
    turns.forEach((turn, index) => {
      if (turn.role === "user") {
        addUser(turn.text, turn.attachments || [], { animate: false, index });
      } else paintTurn(turn, { animate: false });
    });
    if (!turns.length) empty();
    scroll();
  }

  function selectHistory(record) {
    invalidateActiveRun();
    saveHistory();
    currentAgent = agents.some((agent) => agent.name === record.agent) ? record.agent : "";
    preferredAgent = currentAgent;
    settings.agent = currentAgent;
    el("system").value = settings.prompts[promptSlot()] || "";
    currentConversationId = record.id;
    turns = record.turns;
    approvalAllowlist.clear();
    carryOffer = null;
    pendingAttachments = [];
    resetContentLibrary();
    renderAttachments();
    if (currentAgent && record.thread_id) conversationThreads[record.id] = record.thread_id;
    paintTranscript();
    saveSettings();
    syncReferenceFiles();
    workspace = null;
    loadWorkspace();
    if (workspaceOpen && workspaceView === "content") loadContentLibrary({ force: true });
    renderSidebar();
    render();
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
    if (removed && conversationThreads[record.id] === record.thread_id) {
      delete conversationThreads[record.id];
      saveThreads();
      renderHistoryControls();
    }
    return removed;
  }

  async function removeHistory(record) {
    if (record.id === currentConversationId) invalidateActiveRun();
    const threadRemoved = await forgetAgentThread(record);
    if (!threadRemoved) {
      if (record.id === currentConversationId) paintTranscript();
      note("The conversation is still in use and could not be deleted. Try again.", true);
      return;
    }
    historyStore.remove(record.id);
    if (record.id === currentConversationId) startConversation(false);
    renderSideHistory();
  }

  function startConversation(saveCurrent = true, { focus = true } = {}) {
    invalidateActiveRun();
    if (saveCurrent) saveHistory();
    const outgoing = conversationRecord();
    if (!settings.history && outgoing.thread_id) void forgetAgentThread(outgoing);
    turns = [];
    currentConversationId = conversationId();
    // Both are scoped to the conversation that is ending: a standing approval
    // must not survive into the next one, and a carry is a handoff, not a
    // property of "new chat".
    approvalAllowlist.clear();
    carryOffer = null;
    saveThreads();
    log.innerHTML = "";
    empty();
    renderSideHistory();
    if (focus) input.focus();
  }

  function setHistoryConfirmation(open, { focus = true } = {}) {
    const confirmation = el("history-confirm");
    const request = act("request-clear-history");
    confirmation.hidden = !open;
    request.hidden = open;
    if (focus) {
      if (open) requestAnimationFrame(() => focusQuietly(act("cancel-clear-history")));
      else focusQuietly(request.disabled ? act("close-settings") : request);
    }
  }

  async function clearAllConversations({ initial = false } = {}) {
    resetInProgress = true;
    invalidateActiveRun({ recordStop: false });
    render();
    renderHistoryControls("Deleting browser transcripts and project-local assistant context...");
    try {
      const response = await postJSON("/api/agents/threads/clear", {});
      let payload = {};
      try {
        payload = await response.json();
      } catch {
        payload = {};
      }
      if (!response.ok) {
        throw new Error(payload.error || `Conversation reset failed (HTTP ${response.status}).`);
      }
      const localCleared = historyStore.clear({ includeLegacy: true });
      conversationThreads = {};
      saveThreads();
      pendingAttachments = [];
      renderAttachments();
      turns = [];
      currentConversationId = conversationId();
      log.innerHTML = "";
      empty();
      renderSidebar();
      if (!localCleared) throw new Error("Assistant context was deleted, but browser storage could not be cleared.");
      try {
        localStorage.setItem(HISTORY_RESET, "done");
        historyResetRequired = false;
      } catch {
        historyResetRequired = true;
      }
      renderHistoryControls(
        initial
          ? "Conversation history starts clean."
          : `All conversations deleted${payload.removed_threads ? ` (${payload.removed_threads} project thread${payload.removed_threads === 1 ? "" : "s"})` : ""}.`,
      );
      return true;
    } finally {
      resetInProgress = false;
      render();
    }
  }

  async function finishInitialHistoryReset() {
    if (!historyResetRequired) return;
    try {
      await clearAllConversations({ initial: true });
    } catch (exc) {
      renderHistoryControls(`Clean-start reset could not finish: ${exc.message || exc}`);
    }
  }

  function changeHistoryMode() {
    const previous = settings.history;
    const enabled = el("savehistory").checked;
    if (previous === enabled) return;
    if (previous) saveHistory();
    const outgoing = conversationRecord();
    settings.history = enabled;
    if (!previous && outgoing.thread_id) void forgetAgentThread(outgoing);
    conversationThreads = {};
    saveThreads();
    saveSettings();
    startConversation(false, { focus: false });
    renderSidebar();
    renderHistoryControls(
      enabled
        ? "History is on. A fresh conversation is ready."
        : "History is off. A fresh, ephemeral conversation is ready.",
    );
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

  function proposalNode(event) {
    // One normalizer for the wire shape, shared with the AI SDK boundary, so
    // the card cannot read a field spelling the transport does not produce.
    const proposal = normalizeIfcProposal(event);
    const card = document.createElement("section");
    card.className = "chat-proposal" + (proposal.marked ? "" : " unmarked");
    const value = proposal.value === null || proposal.value === undefined
      ? "measured value" : String(proposal.value);
    card.innerHTML = `
      <div class="chat-proposal-mark">${proposal.marked ? "AI-marked" : "provenance marker missing"}</div>
      <div class="chat-proposal-value"><b></b><span></span></div>
      <div class="chat-proposal-target"></div>
      <dl class="chat-proposal-facts"></dl>
      <p class="chat-proposal-note"></p>
      <button type="button" class="chat-proposal-copy">Copy ChangeSet id</button>`;
    card.querySelector("b").textContent = proposal.propertyName || "Measured value";
    card.querySelector(".chat-proposal-value span").textContent =
      value + (proposal.unit ? ` ${proposal.unit}` : "");
    const elements = proposal.elementCount || proposal.changeCount || 1;
    card.querySelector(".chat-proposal-target").textContent =
      `${proposal.psetName || "IfcConsole_AI_Measurements"} · ${elements} element(s)`;
    const facts = card.querySelector(".chat-proposal-facts");
    for (const [label, text] of [
      ["Method", proposal.method],
      ["Source", proposal.source],
      ["Confidence", proposal.confidence],
      ["ChangeSet", proposal.changeSetId.slice(0, 22)],
    ]) {
      if (!text) continue;
      const term = document.createElement("dt");
      term.textContent = label;
      const detail = document.createElement("dd");
      detail.textContent = text;
      facts.append(term, detail);
    }
    // The panel decides whether the call runs at all, so the card no longer
    // sends the reader elsewhere to approve it. What is left to say is where
    // the value went and that the file on disk is still untouched.
    card.querySelector(".chat-proposal-note").textContent =
      "Held in memory with its provenance record."
      + " Nothing is written to the IFC file until you save.";
    if (proposal.warning) {
      const warning = document.createElement("p");
      warning.className = "chat-proposal-warning";
      warning.textContent = proposal.warning;
      card.appendChild(warning);
    }
    card.querySelector(".chat-proposal-copy").addEventListener("click", (event) =>
      copyText(proposal.changeSetId, event.currentTarget));
    return card;
  }

  // ---------------------------------------------------------------- sending
  /* The turn as history should keep it, including the approvals.
   *
   * transcriptBlocks() has no approval case, so a reopened conversation showed
   * the gated tool with no trace that a human allowed it. The decision is put
   * back by position, over the same filtered, limited list that call documents,
   * and only where both sides agree the block is an approval.
   */
  function storedBlocks(state) {
    const kept = state.blocks
      .filter((block) => block.kind !== "text" || block.text.trim())
      .slice(-TRANSCRIPT_BLOCKS);
    return transcriptBlocks(state, { limit: TRANSCRIPT_BLOCKS }).map((block, index) => {
      const source = kept[index];
      if (block.kind !== "approval" || source?.kind !== "approval") return block;
      return {
        kind: "approval",
        name: source.name,
        capabilities: source.capabilities || [],
        state: source.state,
        decidedBy: source.decidedBy || "",
        reason: source.reason || "",
        args: source.args || "",
      };
    });
  }

  function resetRunChrome() {
    log.setAttribute("aria-busy", "false");
    send.innerHTML = I.send;
    send.classList.remove("stop");
    send.title = "Send";
    send.setAttribute("aria-label", "Send message");
  }

  function runIsCurrent(runIdentity) {
    return activeRun === runIdentity
      && currentConversationId === runIdentity.conversationId
      && currentAgent === runIdentity.agent;
  }

  function invalidateActiveRun({ recordStop = true } = {}) {
    // The queue belongs to the conversation it was typed into, so it dies with
    // it rather than landing in whatever is on screen next.
    queuedPrompt = "";
    if (!activeRun) return false;
    const stale = activeRun;
    if (
      recordStop
      && stale.conversationId === currentConversationId
      && stale.agent === currentAgent
      && turns.at(-1)?.role === "user"
    ) {
      turns.push({
        role: "assistant",
        text: "Response stopped before this conversation changed.",
        blocks: [],
        status: "stopped",
        error: "",
      });
      saveHistory();
    }
    activeRun = null;
    aborter = null;
    busy = false;
    stale.controller.abort();
    resetRunChrome();
    renderAttachments();
    el("announce").textContent = "Previous response stopped.";
    return true;
  }

  async function run({ retry = false } = {}) {
    if (resetInProgress) return;
    const view = addAssistant();
    const runIdentity = {
      id: ++runSequence,
      agent: currentAgent,
      conversationId: currentConversationId,
      controller: new AbortController(),
    };
    activeRun = runIdentity;
    busy = true;
    log.setAttribute("aria-busy", "true");
    el("announce").textContent = "Assistant is responding.";
    send.disabled = false;
    send.innerHTML = I.stop;
    send.classList.add("stop");
    send.title = "Stop";
    send.setAttribute("aria-label", "Stop response");
    aborter = runIdentity.controller;
    // Enter means something else while a run is live, and the composer hint is
    // the only place that says so.
    render();
    // One run object holds every derived fact about this turn: the ordered
    // blocks, which stage is live, which tools ran, what came back. The view
    // reads it; nothing else has to track partial state.
    const state = emptyRun();
    const started = performance.now();
    let firstToken = null;
    // Aborting a fetch is asynchronous, so this run can still be unwinding
    // after the user has switched assistant or started a new chat. Everything
    // that touches shared state below checks it is still the same
    // conversation, or a stopped run would land in the next one.
    const runConversation = runIdentity.conversationId;
    const runAgent = runIdentity.agent;

    // `streaming` is false exactly once, when the run is over: the live line
    // goes away then and never comes back, whatever the last block was.
    const draw = (streaming) => {
      // Replacing the live Markdown subtree can trigger browser scroll
      // anchoring. Hold the reader's exact viewport while auto-follow is
      // paused; the newly inferred text still renders beneath it.
      const heldScrollTop = followingOutput ? null : log.scrollTop;
      syncStream(view.stream, state.blocks, { live: streaming });
      if (streaming) showStep(view, state);
      else view.step.hidden = true;
      if (heldScrollTop !== null) {
        log.scrollTop = heldScrollTop;
        lastScrollTop = log.scrollTop;
      }
    };

    // Re-parsing the whole answer per token is quadratic and fights the user
    // for the selection. A timer, not requestAnimationFrame: a background tab
    // stops painting frames entirely and the answer would sit invisible.
    let repaint = 0;
    let repaintShouldScroll = false;
    const schedule = (stick = false) => {
      repaintShouldScroll ||= stick;
      repaint ||= setTimeout(() => {
        repaint = 0;
        if (!runIsCurrent(runIdentity)) return;
        // Never take a selection away from the reader mid-sentence; the next
        // tick paints the same answer once they let go.
        if (selectionInside(view.stream)) {
          schedule();
          return;
        }
        // A reader may scroll up during this 60ms batching window. Their input
        // owns the viewport even if it moved only a few pixels and remains
        // geometrically close to the bottom.
        const shouldScroll = repaintShouldScroll && followingOutput;
        repaintShouldScroll = false;
        draw(true);
        if (shouldScroll) scroll();
      }, 60);
    };

    try {
      const shared = {
        provider: el("provider").value,
        model: chosenModel(),
        base_url: el("baseurl").value.trim() || undefined,
        api_key: el("key").value.trim() || undefined,
        tools_supported: effectiveCapability("tools") ?? undefined,
        vision_supported: effectiveCapability("vision") ?? undefined,
        temperature: el("temp").value === "" ? undefined : parseFloat(el("temp").value),
        max_tokens: el("maxtok").value === "" ? undefined : parseInt(el("maxtok").value, 10),
      };
      const lastUser = turns.findLast((turn) => turn.role === "user");
      const retryInExistingAgentThread = retry
        && runAgent
        && Boolean(conversationThreads[runConversation]);
      const requestMessages = boundedChatTurns(turns);
      const requestBody = runAgent
        ? agentChatRequest(requestMessages, {
            ...shared,
            agent: runAgent,
            prompt: retryInExistingAgentThread
              ? "Retry the preceding user request and return a complete answer."
              : (lastUser?.text ?? ""),
            thread_id: conversationThreads[runConversation] || undefined,
            persist_history: settings.history,
            additional_instructions: el("system").value.trim() || undefined,
            attachments: retryInExistingAgentThread
              ? []
              : (lastUser?.attachments?.map((item) => item.path) || []),
          })
        : plainChatRequest(requestMessages, {
            ...shared,
            system: el("system").value.trim() || undefined,
            tools: el("tools").checked,
          });
      const response = await postJSON(
        runAgent ? "/api/agents/stream" : "/api/chat/stream",
        requestBody,
        runIdentity.controller.signal
      );
      if (!response.ok) {
        let detail = "";
        try {
          const payload = await response.json();
          detail = payload.hint || payload.error || "";
        } catch {
          detail = "";
        }
        throw new Error(detail || `Chat unavailable (HTTP ${response.status}).`);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!runIsCurrent(runIdentity)) {
          await reader.cancel();
          return;
        }
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = decodeIfcSSE(buffer);
        buffer = rest;
        for (const event of events) {
          const stick = followingOutput;
          applyEvent(state, event, { now: performance.now() });
          if (event.type === "content" || event.type === "reasoning") {
            firstToken ??= performance.now();
            schedule(stick);
          } else if (event.type === "tool_progress") {
            // Progress may be high frequency; share the same bounded repaint
            // cadence as tokens instead of forcing layout for every update.
            schedule(stick);
          } else if (
            event.type === "tool_call" ||
            event.type === "tool_result" ||
            event.type === "proposal" ||
            // The run is blocked on this one: if it is not painted now, it is
            // never painted, because nothing further arrives until it is
            // answered.
            event.type === "approval" ||
            event.type === "approval_decided"
          ) {
            // A tool landing is the thing the reader is waiting for: paint it
            // immediately rather than on the next text tick.
            draw(true);
          } else if (event.type === "thread") {
            conversationThreads[runConversation] = event.id;
            saveThreads();
            // The first user turn was archived before the server assigned an
            // id. Save again now so deletion can always find durable context.
            saveHistory();
            // From here the console owns the history, so the panel can no
            // longer rewind it.
            syncTurnControls();
          }
          showStep(view, state);
          // The log grows without firing a scroll event, so the way back down
          // has to be offered from here as well.
          // Deferred events scroll once with their batched paint. Avoiding a
          // scroll write for every token removes layout churn and flicker.
          if (stick && followingOutput && !repaint) scroll();
          else syncJump();
        }
      }
    } catch (exc) {
      if (exc.name !== "AbortError") state.error = String(exc.message || exc);
    }

    // `repaintShouldScroll` describes an earlier moment. Only current scroll
    // position decides whether the final paint follows the response.
    const finishStick = followingOutput;
    clearTimeout(repaint);
    repaintShouldScroll = false;
    if (!runIsCurrent(runIdentity)) return;
    const stopped = runIdentity.controller.signal.aborted;
    // Nothing more can arrive on this stream, so nothing on it may still offer
    // a control: the console has already denied every approval left waiting.
    settleRun(state, { stopped });
    draw(false);
    if (finishStick) scroll();
    else syncJump();

    const text = state.blocks
      .filter((block) => block.kind === "text")
      .map((block) => block.text.trim())
      .filter(Boolean)
      .join("\n\n");
    if (state.error) {
      const box = document.createElement("div");
      box.className = "chat-error";
      box.textContent = state.error;
      // Retry is offered whatever the run managed to write first. Gating it on
      // an empty answer left the common case, two paragraphs and then a
      // provider error, with a red line and nothing to do about it.
      box.insertAdjacentHTML(
        "beforeend",
        '<button class="chat-retry" type="button" data-act="retry">retry</button>'
      );
      view.stream.appendChild(box);
    }
    const stoppedMessage = stopped && !text ? "Response stopped before content." : "";
    if (stoppedMessage) {
      const stoppedBox = document.createElement("div");
      stoppedBox.className = "chat-answer stopped";
      stoppedBox.textContent = stoppedMessage;
      view.stream.appendChild(stoppedBox);
    }
    const transcriptText = text || stoppedMessage;
    if ((transcriptText || state.blocks.length || state.error) && runIsCurrent(runIdentity)) {
      turns.push({
        role: "assistant",
        text: transcriptText,
        blocks: storedBlocks(state),
        status: state.error ? "error" : stopped ? "stopped" : "complete",
        error: state.error || "",
      });
      saveHistory();
    }

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
    if (ran) {
      const failed = state.tools.filter((tool) => tool.state === "bad").length;
      bits.push(`${ran} tool call${ran === 1 ? "" : "s"}${failed ? `, ${failed} failed` : ""}`);
    }
    view.stats.textContent = bits.join(" · ");
    // A turn cut short by the token cap or by Stop has more to say, and the
    // cap used to be reported as one word in this line with no way to act on
    // it. Asking for the rest should not mean retyping the question.
    if (text && (state.finishReason === "length" || stopped)) {
      const more = document.createElement("button");
      more.type = "button";
      more.className = "chat-continue";
      more.dataset.act = "continue";
      more.textContent = "continue";
      more.title = state.finishReason === "length"
        ? "The answer hit the token cap. Ask for the rest."
        : "Ask the assistant to carry on from where it stopped.";
      view.stats.appendChild(more);
    }
    if (text) view.stats.appendChild(copyButton(text));

    activeRun = null;
    busy = false;
    el("announce").textContent = stopped
      ? "Assistant response stopped."
      : state.error && !text ? "Assistant response failed." : "Assistant response ready.";
    aborter = null;
    resetRunChrome();
    render();
    refreshContext();
    input.focus();
    // The follow-up typed while this turn was streaming. It goes out only now,
    // so the model sees the finished turn ahead of it. A draft typed after it
    // was queued has never been submitted, so that one waits for its Enter and
    // the queued message joins it in the composer rather than being sent past
    // it out of order.
    if (queuedPrompt) {
      const draft = input.value.trim();
      input.value = draft ? `${queuedPrompt}\n\n${input.value}` : queuedPrompt;
      queuedPrompt = "";
      grow();
      renderAttachments();
      if (draft) {
        input.focus();
        note("The queued message is back in the composer, ahead of your draft.");
      } else await submit();
    }
  }

  async function submit() {
    if (resetInProgress || uploadsInFlight()) return;
    const text = input.value.trim();
    if (text.length > PROMPT_LIMIT) {
      note("This message is too long. Shorten it to 100,000 characters before sending.", true);
      return;
    }
    const intent = composerIntent({ busy, text });
    if (intent === "ignore") return;
    if (intent === "stop") {
      aborter?.abort();
      return;
    }
    if (intent === "queue") {
      queuedPrompt = text;
      input.value = "";
      grow();
      closeSuggest();
      renderAttachments();
      render();
      el("announce").textContent = "Message queued. It sends when this response finishes.";
      return;
    }
    if (!chosenModel() || !hasKey(provider())) {
      openSettings();
      return;
    }
    input.value = "";
    grow();
    closeSuggest();
    carryOffer = null;
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
  input.addEventListener("input", () => {
    grow();
    updateSuggest();
  });
  input.addEventListener("blur", () => closeSuggest());

  /* Files arrive the way they do in every other panel: pasted or dropped.
   *
   * Until now the only route was the plus menu, even though uploadFiles()
   * already takes a File[] and the viewer-screenshot path builds one by hand.
   * The refusals are explicit: a silent no-op reads as a broken drop target.
   */
  function acceptFiles(files) {
    const rows = [...files].filter(Boolean);
    if (!rows.length) return;
    if (!currentAgent || !(pack()?.features || []).includes("files")) {
      note(`${agentTitle()} takes no attachments. Choose an assistant that reads project content.`, true);
      return;
    }
    void uploadFiles(rows);
  }

  input.addEventListener("paste", (event) => {
    const files = [...(event.clipboardData?.files || [])];
    if (!files.length) return;
    event.preventDefault();
    acceptFiles(files);
  });

  const composer = root.querySelector(".chat-composer");
  const carriesFiles = (event) => [...(event.dataTransfer?.types || [])].includes("Files");
  composer.addEventListener("dragover", (event) => {
    if (!carriesFiles(event)) return;
    event.preventDefault();
    composer.classList.add("drop-target");
  });
  composer.addEventListener("dragleave", (event) => {
    // dragleave also fires crossing between children, which flickers the state
    if (composer.contains(event.relatedTarget)) return;
    composer.classList.remove("drop-target");
  });
  composer.addEventListener("dragend", () => composer.classList.remove("drop-target"));
  composer.addEventListener("drop", (event) => {
    composer.classList.remove("drop-target");
    if (!carriesFiles(event)) return;
    event.preventDefault();
    acceptFiles(event.dataTransfer?.files || []);
  });
  input.addEventListener("keydown", (event) => {
    if (suggestState) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const count = suggestState.items.length;
        suggestState.active = (suggestState.active + step + count) % count;
        renderSuggest();
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        applySuggestion();
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeSuggest();
        return;
      }
    }
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  // Whatever is on top owns Tab and Escape, in the order it sits on screen.
  function topLayer() {
    if (workspaceOpen) return el("workspace");
    if (sideOpen && !sideIsInline()) return el("side");
    return null;
  }

  root.addEventListener("keydown", (event) => {
    const layer = topLayer();
    if (layer && event.key === "Tab") {
      const focusable = [...layer.querySelectorAll(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
      )].filter((node) => node.tabIndex >= 0 && !node.hidden && node.offsetParent !== null);
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
    if (suggestState) closeSuggest();
    else if (!el("plus-menu").hidden) closePlusMenu({ restoreFocus: true });
    else if (armedDelete) disarmDelete();
    else if (!el("modal").hidden) closeSettings();
    else if (!el("builder-modal").hidden) closeBuilder();
    else if (workspaceOpen) closeWorkspace();
    else if (sideOpen && !sideIsInline()) {
      setSide(false);
      focusQuietly(act("toggle-side"));
    } else if (busy) aborter?.abort();
    else return;
    event.stopPropagation();
  });

  root.addEventListener("click", async (event) => {
    // Anything that is not the armed button itself cancels the pending delete.
    if (!event.target.closest(".chat-side-delete")) disarmDelete();
    if (!event.target.closest(".chat-plus-menu, .chat-plus")) closePlusMenu();
    const workspaceNav = event.target.closest("[data-workspace-view]");
    if (workspaceNav) {
      setWorkspaceView(workspaceNav.dataset.workspaceView, { focus: true });
      if (workspaceNav.dataset.workspaceView === "content") void loadContentLibrary();
      return;
    }
    const actionButton = event.target.closest("[data-act]");
    const action = actionButton?.dataset.act;
    if (action === "send") {
      // While a run is live this control reads Stop, so it stops, whatever is
      // in the composer. Queueing is the keyboard's job.
      if (busy) aborter?.abort();
      else submit();
    }
    else if (action === "workspace") toggleWorkspace(actionButton);
    else if (action === "close-workspace") closeWorkspace();
    else if (action === "close-overlays") {
      if (workspaceOpen) closeWorkspace();
      else if (sideOpen && !sideIsInline()) {
        setSide(false);
        focusQuietly(act("toggle-side"));
      } else input.focus();
    }
    else if (action === "toggle-side") setSide(!sideOpen, { trigger: actionButton });
    else if (action === "settings") openSettings(actionButton);
    else if (action === "export") exportConversation();
    else if (action === "builder") openBuilder(actionButton);
    else if (action === "studio-current") {
      if (!workspace || workspace.name !== currentAgent) await loadWorkspace({ force: true });
      openBuilder(actionButton, workspace || pack());
    }
    else if (action === "close-settings") closeSettings();
    else if (action === "close-builder") closeBuilder();
    else if (action === "save-builder") saveBuilder();
    else if (action === "models") loadModels();
    else if (action === "toggle-key") {
      const key = el("key");
      const showing = key.type === "text";
      key.type = showing ? "password" : "text";
      actionButton.textContent = showing ? "Show typed key" : "Hide typed key";
      key.focus({ preventScroll: true });
    }
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
    else if (action === "save-model") void saveModelFile();
    else if (action === "plus") {
      if (el("plus-menu").hidden) openPlusMenu();
      else closePlusMenu({ restoreFocus: true });
    }
    else if (action === "drop-selection") {
      document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
        detail: { action: "clear-model-selection", model_id: actionButton.dataset.modelId },
      }));
    }
    else if (action === "drop-queued") {
      // Taking it back returns the text rather than deleting it, and the
      // composer may hold a second follow-up by now.
      input.value = input.value.trim() ? `${queuedPrompt}\n\n${input.value}` : queuedPrompt;
      queuedPrompt = "";
      grow();
      renderAttachments();
      input.focus();
    }
    else if (action === "clear") {
      startConversation();
      closeSideIfOverlay();
    } else if (action === "request-clear-history") {
      setHistoryConfirmation(true);
    } else if (action === "cancel-clear-history") {
      setHistoryConfirmation(false);
    } else if (action === "confirm-clear-history") {
      const button = act("confirm-clear-history");
      button.disabled = true;
      button.textContent = "Deleting...";
      try {
        await clearAllConversations();
        setHistoryConfirmation(false);
      } catch (exc) {
        renderHistoryControls(`Could not delete conversations: ${exc.message || exc}`);
      } finally {
        button.disabled = false;
        button.textContent = "Delete conversations";
      }
    } else if (action === "retry" && !busy) {
      const previous = log.lastElementChild;
      if (previous?.classList.contains("assistant")) {
        // What the failed run did manage to write is evidence, not rubbish:
        // it stays above the retry instead of vanishing with the error.
        const kept = previous.querySelector(
          ".chat-answer:not(.stopped), .chat-tool-card, .chat-think, .chat-approval, .chat-proposal",
        );
        if (kept) {
          previous.classList.add("superseded");
          previous.querySelector(".chat-retry")?.remove();
          previous.querySelector(".chat-continue")?.remove();
          const stats = previous.querySelector(".chat-stats");
          if (stats) stats.textContent = "superseded by a retry";
        } else previous.remove();
      }
      if (turns.at(-1)?.role === "assistant") turns.pop();
      run({ retry: true });
    } else if (action === "continue" && !busy) {
      const ask = "Continue from where you stopped. Do not repeat what you already wrote.";
      actionButton.remove();
      addUser(ask, []);
      turns.push({ role: "user", text: ask, attachments: [] });
      saveHistory();
      run();
    } else if (action === "jump") {
      scroll({ smooth: true });
      input.focus({ preventScroll: true });
    }
    // Only an attachment chip carries an index. The selection and the queued
    // message wear the same control, and Number(undefined) splices at 0.
    const remove = event.target.closest(".chat-attachment-remove[data-index]");
    if (remove) {
      pendingAttachments.splice(Number(remove.dataset.index), 1);
      renderAttachments();
    }
  });

  el("builder-modal").addEventListener("input", () => {
    el("builder-error").textContent = "";
    renderStudio();
  });
  el("builder-modal").addEventListener("change", () => {
    el("builder-error").textContent = "";
    renderStudio();
  });
  el("builder-modal").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeBuilder();
  });

  root.querySelector(".chat-studio-editor").addEventListener("keydown", (event) => {
    if (event.target !== event.currentTarget || event.altKey || event.ctrlKey || event.metaKey) {
      return;
    }
    const scroller = event.currentTarget;
    const page = Math.max(120, Math.round(scroller.clientHeight * 0.82));
    if (event.key === "PageDown" || event.key === "PageUp") {
      event.preventDefault();
      scroller.scrollBy({ top: event.key === "PageDown" ? page : -page, behavior: "smooth" });
    } else if (event.key === "Home" || event.key === "End") {
      event.preventDefault();
      scroller.scrollTo({ top: event.key === "Home" ? 0 : scroller.scrollHeight, behavior: "smooth" });
    }
  });

  el("workspace-nav").addEventListener("keydown", (event) => {
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    if (!event.target.matches('[role="tab"]')) return;
    const tabs = [...el("workspace-nav").querySelectorAll('[role="tab"]')];
    if (!tabs.length) return;
    event.preventDefault();
    const current = Math.max(0, tabs.indexOf(document.activeElement));
    const next = event.key === "Home"
      ? 0
      : event.key === "End"
        ? tabs.length - 1
        : (current + (event.key === "ArrowDown" ? 1 : -1) + tabs.length) % tabs.length;
    setWorkspaceView(tabs[next].dataset.workspaceView, { focus: true });
  });

  el("workspace").addEventListener("cancel", (event) => {
    event.preventDefault();
    closeWorkspace();
  });

  // A click on a dialog's ::backdrop targets the dialog itself, so "outside"
  // means outside its own box. Pointer coordinates, not the target, because a
  // <select> dropdown paints over the sheet and would read as an outside hit.
  el("workspace").addEventListener("mousedown", (event) => {
    if (event.button !== 0 || event.target !== el("workspace")) return;
    const box = el("workspace").getBoundingClientRect();
    const outside = event.clientX < box.left || event.clientX > box.right
      || event.clientY < box.top || event.clientY > box.bottom;
    if (!outside) return;
    // Agent setup holds an unsaved draft; make dismissing it deliberate.
    if (workspaceView === "builder") return;
    closeWorkspace();
  });

  el("file").addEventListener("change", async () => {
    const files = [...el("file").files];
    el("file").value = "";
    if (files.length && currentAgent) await uploadFiles(files);
  });
  el("model").addEventListener("change", () => {
    el("modelcustom").hidden = el("model").value !== "__custom__";
    if (!el("modelcustom").hidden) el("modelcustom").focus();
    syncCapabilityControls();
    saveSettings();
    forkConversationForConfigurationChange(
      "AI model changed. A fresh conversation is ready.",
      { keep: `Model changed to ${chosenModel() || "none"}. Later turns use it.` },
    );
  });
  el("content-file").addEventListener("change", async () => {
    const files = [...el("content-file").files];
    el("content-file").value = "";
    if (files.length) await uploadWorkspaceContent(files);
  });
  el("ifcmodel").addEventListener("change", () => {
    document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
      detail: { action: "set-model", modelId: el("ifcmodel").value },
    }));
  });
  el("session-mode").addEventListener("change", () => {
    void changeSessionMode(el("session-mode").value);
  });
  el("session-autonomy").addEventListener("change", () => {
    void changeSessionAutonomy(el("session-autonomy").value);
  });
  el("theme").addEventListener("change", () => {
    settings.theme = el("theme").value;
    applyThemePreference(settings.theme, { notifyViewer: true });
    saveSettings();
  });
  el("provider").addEventListener("change", () => {
    modelRequest += 1;
    modelDetails = {};
    const p = provider();
    const mine = remembered(el("provider").value);
    el("key").value = "";
    el("key").type = "password";
    act("toggle-key").textContent = "Show typed key";
    el("baseurl").value = mine.base_url || "";
    setModelOptions([], mine.model || p?.suggested_model || "");
    saveSettings();
    forkConversationForConfigurationChange(
      "AI provider changed. A fresh conversation is ready.",
      { keep: `Provider changed to ${p?.label || el("provider").value}. Later turns use it.` },
    );
    if (hasKey(p)) loadModels({ quiet: true });
  });
  for (const role of ["modelcustom", "baseurl", "tools", "toolcap", "visioncap"]) {
    el(role).addEventListener("change", () => {
      if (role === "modelcustom") syncCapabilityControls();
      saveSettings();
      renderCapabilities();
      forkConversationForConfigurationChange(undefined, {
        keep: "Assistant configuration changed. Later turns use it.",
      });
    });
  }
  for (const role of ["savekey", "temp", "maxtok", "key"]) {
    el(role).addEventListener("change", saveSettings);
  }
  el("key").addEventListener("input", render);
  for (const role of ["baseurl", "key"]) {
    el(role).addEventListener("input", () => { modelRequest += 1; });
  }
  el("savehistory").addEventListener("change", changeHistoryMode);
  el("system").addEventListener("change", () => {
    saveSettings();
    forkConversationForConfigurationChange(
      "Standing instructions changed. A fresh conversation is ready.",
      { keep: "Standing instructions changed. Later turns use them." },
    );
    loadWorkspace({ force: true });
  });

  document.addEventListener("ifc-console:viewer-context", (event) => {
    const detail = event.detail;
    if (!detail || typeof detail !== "object") return;
    const viewerOpen = detail.open !== false;
    viewerLinked = viewerOpen;
    const model = detail.model && typeof detail.model === "object" ? detail.model : null;
    const selection = detail.selection && typeof detail.selection === "object"
      ? detail.selection
      : null;
    const selections = Array.isArray(detail.selections) ? detail.selections : null;
    const viewerTheme = typeof detail.theme === "string"
      ? detail.theme
      : detail.theme?.resolved;
    sessionStatus = {
      ...sessionStatus,
      model: model?.name || sessionStatus.model,
      view_model_id: viewerOpen ? (model?.id || sessionStatus.view_model_id) : null,
      models: Array.isArray(detail.models) ? detail.models : sessionStatus.models,
      selection: viewerOpen && Array.isArray(selection?.guids)
        ? selection.guids : viewerOpen ? sessionStatus.selection : [],
      selections: viewerOpen
        ? (selections || sessionStatus.selections || [])
        : [],
      // The viewer already publishes its saved views with every context frame,
      // so `@view:` costs nothing beyond reading them.
      saved_views: Array.isArray(detail.savedViews) ? detail.savedViews : sessionStatus.saved_views,
      mode: detail.mode || sessionStatus.mode,
      ai_autonomy: sessionStatus.ai_autonomy,
      dirty: sessionStatus.dirty,
      viewer_theme: viewerTheme || sessionStatus.viewer_theme,
    };
    if (UI_THEME_IDS.includes(viewerTheme)) {
      workspaceTheme = viewerTheme;
      // The dock and Agent workspace are part of this viewer, not a separately
      // themed widget. A viewer change therefore always wins and is remembered
      // so a later lazy mount cannot flash the previous Agent palette.
      if (root.dataset.theme !== viewerTheme) applyThemePreference(viewerTheme);
      if (settings.theme !== viewerTheme) rememberThemePreference(viewerTheme);
    }
    renderContext();
  });

  document.addEventListener("ifc-console:viewer-result", async (event) => {
    const detail = event.detail;
    if (detail?.ok && detail.action === "get-context" && detail.result) {
      document.dispatchEvent(new CustomEvent("ifc-console:viewer-context", {
        detail: detail.result,
      }));
      return;
    }
    if (String(detail?.commandId || "").startsWith("chat-guid-") && !detail.ok) {
      note(`Could not show that IFC element: ${detail.error || "element unavailable"}`, true);
      return;
    }
    if (!detail || detail.commandId !== pendingCaptureCommand) return;
    pendingCaptureCommand = "";
    if (!detail.ok) {
      note(`Could not capture the 3D view: ${detail.error || "viewer unavailable"}`, true);
      return;
    }
    const result = detail.result;
    if (!result?.dataUrl || !String(result.mime || "").startsWith("image/")) {
      note("The viewer returned no image evidence.", true);
      return;
    }
    try {
      const comma = result.dataUrl.indexOf(",");
      const header = result.dataUrl.slice(0, comma);
      if (comma < 0 || !/;base64$/i.test(header)) {
        throw new Error("viewer returned an invalid screenshot payload");
      }
      const binary = atob(result.dataUrl.slice(comma + 1));
      const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
      const blob = new Blob([bytes], { type: result.mime });
      const stem = String(result.modelName || "ifc-view")
        .replace(/[^a-z0-9_-]+/gi, "-")
        .replace(/^-|-$/g, "")
        .toLowerCase() || "ifc-view";
      const file = new File([blob], `${stem}-evidence.png`, { type: result.mime });
      await uploadFiles([file]);
    } catch (exc) {
      note(`Could not attach the 3D view: ${exc.message || exc}`, true);
    }
  });

  document.dispatchEvent(new CustomEvent("ifc-console:viewer-command", {
    detail: { action: "get-context" },
  }));

  // The sidebar starts where it was left, and open by default on a surface
  // wide enough to hold it without covering the conversation.
  let savedSide = null;
  try {
    savedSide = localStorage.getItem(SIDE_STORE);
  } catch {
    savedSide = null;
  }
  setSide(savedSide === "open" || (savedSide === null && shellWidth() >= SIDE_INLINE_WIDTH), {
    remember: false,
    moveFocus: false,
  });

  const urlAgent = new URLSearchParams(location.search).get("agent");
  const openBuilderFromUrl = new URLSearchParams(location.search).get("builder") === "1";
  if (urlAgent) settings.agent = preferredAgent = urlAgent;
  currentAgent = preferredAgent || "";
  currentConversationId = conversationId();
  empty();
  renderSidebar();
  finishInitialHistoryReset();
  refreshContext();
  loadProviders();
  loadCapabilities();
  loadAgents().then(() => {
    if (openBuilderFromUrl) openBuilder(act("builder"));
  });
  return {
    focus: () => input.focus(),
    ask: (text) => {
      input.value = text;
      submit();
    },
    refresh: refreshContext,
    setVisible: (visible) => {
      if (!visible) {
        if (!el("modal").hidden) closeSettings({ restoreFocus: false });
        if (!el("builder-modal").hidden) closeBuilder({ restoreFocus: false });
        if (workspaceOpen) {
          workspaceReturnFocus = null;
          closeWorkspace({ restoreFocus: false });
        }
        if (sideOpen && !sideIsInline()) {
          setSide(false, { remember: false, restoreFocus: false, moveFocus: false });
        }
        setHistoryConfirmation(false, { focus: false });
      }
      requestAnimationFrame(syncShellLayout);
    },
  };
}
