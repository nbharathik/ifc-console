/* Workflow surface: one library, two views.
 *
 * Library - every workflow as a row on the left and a page on the right: what
 *           it does, its stages, its exact prompt, and one Run control.
 * Runs    - every run of this session, streamed as it happens and readable
 *           afterwards, with follow-up questions in the same thread.
 *
 * A run asks for nothing but a click. Scope comes from the viewer, everything
 * else comes from the workflow's system prompt plus one optional prompt the
 * user types for that run, so no workflow has to grow a form. Authoring is a
 * page of the same surface, reached from Edit or New workflow.
 *
 * Rendering is incremental where it matters: a streaming run appends entries
 * and patches the one paragraph that is growing, rather than rebuilding the
 * thread on every token. Everything from the server or the model is escaped
 * or rendered through the panel's Markdown renderer; nothing untrusted lands
 * in innerHTML as is.
 */
import { md, esc } from "/agents/static/chat_markdown.js";
import {
  authenticatedOptions,
  clipText,
  inputValue,
  missingInputs,
  normalizeViewerContext,
  parseEventStream,
  selectionCount,
} from "/agents/static/workflows_model.js";

const hash = new URLSearchParams(location.hash.replace(/^#/, ""));
const workflowToken = hash.get("t") || sessionStorage.getItem("ifc-console-token") || "";
if (workflowToken) sessionStorage.setItem("ifc-console-token", workflowToken);
if (hash.has("t")) history.replaceState(null, "", location.pathname + location.search);

function workflowApi(path, options = {}) {
  return fetch(path, authenticatedOptions(workflowToken, options));
}

// Bounds on what one session keeps. A run's tool results are the largest
// thing here; their detail is clipped at receipt so a long run cannot grow
// the page without limit.
const RUN_LIMIT = 30;
const ENTRY_LIMIT = 300;
const DETAIL_LIMIT = 6000;
const STAGE_ICONS = { tool: "⚙", agent: "✦", gate: "◆", export: "≡" };
const STAGE_WORDS = { tool: "tool", agent: "agent", gate: "decision", export: "report" };

const TEMPLATE = `
<div class="wf-shell" data-section="launch" data-list-open="true">
  <header class="wf-topbar">
    <div class="wf-brand">
      <span class="wf-mark" aria-hidden="true">WF</span>
      <div><b>Workflows</b><small data-role="topbar-note"></small></div>
    </div>
    <nav class="wf-tabs" role="tablist" aria-label="Workflow views">
      <button data-act="section" data-section="launch" role="tab" type="button" aria-selected="true">Library</button>
      <button data-act="section" data-section="runs" role="tab" type="button" aria-selected="false">Runs<i data-role="running-count"></i></button>
    </nav>
    <div class="wf-topbar-actions">
      <div class="wf-scope-toggle" role="group" aria-label="Run scope" data-role="scope"></div>
      <button class="wf-secondary" data-act="new-workflow" type="button">New workflow</button>
      <button class="wf-agent-switch" data-act="switch-agent" type="button" title="Back to the Agent panel">
        <span aria-hidden="true">&#x2726;</span><small>Agent</small>
      </button>
    </div>
  </header>
  <div class="wf-body">
    <aside class="wf-list-pane" aria-label="Workflow list">
      <div class="wf-list-tools">
        <label class="wf-search" data-role="search-wrap">
          <input data-role="search" type="search" placeholder="Find a workflow" autocomplete="off" aria-label="Find a workflow">
          <kbd>/</kbd>
        </label>
      </div>
      <div class="wf-list" data-role="list"></div>
      <footer class="wf-list-foot" data-role="list-foot"></footer>
    </aside>
    <main class="wf-main">
      <header class="wf-main-head">
        <button class="wf-square wf-menu" data-act="toggle-list" type="button" aria-label="Toggle the list">&#x2630;</button>
        <div class="wf-main-title">
          <span class="wf-kicker" data-role="main-kicker">Workflow</span>
          <h1 data-role="main-title">Select a workflow</h1>
          <p data-role="main-subtitle"></p>
        </div>
        <div class="wf-main-actions" data-role="main-actions"></div>
      </header>
      <div class="wf-canvas" data-role="canvas"></div>
    </main>
  </div>
</div>`;

const STYLESHEET = "/agents/static/workflows.css";

function ensureStylesheet() {
  if (document.querySelector(`link[href="${STYLESHEET}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = STYLESHEET;
  document.head.append(link);
}

function nowLabel(date = new Date()) {
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function valueText(value) {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return String(value); }
}

function scopeLabel(flow) {
  if (flow.scope === "selection") return "Viewer selection";
  if (flow.scope === "either") return "Model or viewer selection";
  return "Whole model";
}

function compactSentence(value, limit = 150) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) return text;
  return `${text.slice(0, limit - 1).trimEnd()}…`;
}

function flowTags(flow) {
  const tags = [flow.requires_model ? (flow.agents || ["agent"])[0] : "local tools"];
  tags.push(`${flow.steps.length} stage${flow.steps.length === 1 ? "" : "s"}`);
  if (flow.scope === "selection") tags.push("needs a selection");
  if (flow.has_gate) tags.push("asks before saving");
  if (flow.origin === "project") tags.push("yours");
  return tags;
}

function settingRow(key = "", value = "", owner = "editor") {
  return `<div class="wf-setting-row" data-setting-owner="${owner}">
    <input data-setting-key type="text" maxlength="64" value="${esc(key)}" placeholder="key" aria-label="Setting name">
    <input data-setting-value type="text" maxlength="2000" value="${esc(value)}" placeholder="value" aria-label="Setting value">
    <button data-act="remove-setting" type="button" aria-label="Remove setting">&#x2715;</button>
  </div>`;
}

/* Only a hand-written workflow still declares inputs. The built-in ones ask
 * for nothing, so this renders for legacy files and stays out of the way. */
function runInputMarkup(item, flowName, viewerContext, value) {
  const common = `data-run-input="${esc(item.id)}" data-flow="${esc(flowName)}"`;
  let field;
  if (item.type === "choice") {
    field = `<select ${common}>${(item.choices || []).map((choice) => `<option value="${esc(choice)}"${String(value ?? item.default ?? "") === choice ? " selected" : ""}>${esc(choice)}</option>`).join("")}</select>`;
  } else if (item.type === "model") {
    const active = viewerContext.models.find((row) => row.active)?.id;
    const alternatives = viewerContext.models.filter((row) => row.id !== active);
    const models = alternatives.length ? alternatives : viewerContext.models;
    field = `<select ${common}><option value="">Choose attached model</option>${models.map((row) => `<option value="${esc(row.id)}"${String(value ?? "") === row.id ? " selected" : ""}>${esc(row.name)}</option>`).join("")}</select>`;
  } else if (item.type === "boolean") {
    field = `<input ${common} type="checkbox"${value ?? item.default ? " checked" : ""}>`;
  } else {
    field = `<input ${common} type="${item.type === "number" ? "number" : "text"}" value="${esc(String(value ?? item.default ?? ""))}">`;
  }
  return `<label class="wf-run-field"><span>${esc(item.label)}${item.required ? " *" : ""}</span>${field}${item.help ? `<small>${esc(item.help)}</small>` : ""}</label>`;
}

export function mountWorkflows(root, options = {}) {
  ensureStylesheet();
  root.classList.add("wf-host");
  root.innerHTML = TEMPLATE;
  const role = (name) => root.querySelector(`[data-role="${name}"]`);
  const shell = root.querySelector(".wf-shell");
  const canvas = role("canvas");
  const request = options.request || workflowApi;

  let workflows = [];
  let agents = [];
  let skills = [];
  let viewerContext = normalizeViewerContext(options.viewer?.getContext?.() || {});
  let section = "launch";
  let listOpen = true;
  let selectedWorkflow = "";
  let query = "";
  let editorMode = "view";
  let editorError = "";
  let runs = [];
  let selectedRun = "";
  let runView = "activity";
  let launcherScope = "model";
  let launcherError = "";
  const launcherSelection = new Set();
  const launcherNotes = {};
  const launcherInputs = {};
  let paintFrame = 0;
  let listSignature = "";

  function setHeader(kicker, title, subtitle = "", actions = "") {
    role("main-kicker").textContent = kicker;
    role("main-title").textContent = title;
    role("main-subtitle").textContent = subtitle;
    role("main-actions").innerHTML = actions;
  }

  function currentFlow() {
    return workflows.find((flow) => flow.name === selectedWorkflow) || null;
  }

  function currentRun() {
    return runs.find((run) => run.id === selectedRun) || null;
  }

  function activeRuns() {
    return runs.filter((run) => run.asking || ["queued", "running", "waiting"].includes(run.state));
  }

  const editing = () => editorMode !== "view";

  /* ------------------------------------------------------------- top bar */

  function renderScopeToggle() {
    const total = selectionCount(viewerContext);
    const host = role("scope");
    host.hidden = section === "runs";
    host.innerHTML = `
      <button data-act="launcher-scope" data-scope="model" type="button" aria-pressed="${launcherScope === "model"}">Whole model</button>
      <button data-act="launcher-scope" data-scope="selection" type="button" aria-pressed="${launcherScope === "selection"}"${total ? "" : " disabled"} title="${total ? "Run on the elements selected in the 3D view" : "Select elements in the 3D view first"}">Selection${total ? ` (${total})` : ""}</button>`;
  }

  function renderNav() {
    shell.dataset.section = editing() ? "workflows" : section;
    shell.dataset.listOpen = String(listOpen);
    root.querySelectorAll('[data-act="section"]').forEach((button) => {
      button.setAttribute("aria-selected", String(button.dataset.section === section));
    });
    const active = activeRuns().length;
    role("running-count").textContent = active ? String(active) : "";
    const model = viewerContext.models.find((row) => row.active) || viewerContext.models[0];
    role("topbar-note").textContent = model?.name
      ? `${workflows.length} for ${model.name}`
      : `${workflows.length} available`;
    renderScopeToggle();
  }

  /* --------------------------------------------------------------- list */

  function matches(flow, needle) {
    if (!needle) return true;
    return [flow.title, flow.description, ...(flow.tags || [])].join(" ").toLowerCase().includes(needle);
  }

  function workflowRows() {
    return workflows.filter((flow) => matches(flow, query.trim().toLowerCase()));
  }

  function workflowRowMarkup(flow) {
    const selected = launcherSelection.has(flow.name);
    return `<div class="wf-name-row${flow.name === selectedWorkflow ? " is-active" : ""}${selected ? " is-checked" : ""}" data-workflow="${esc(flow.name)}">
      <button class="wf-row-check" data-act="toggle-run-choice" data-workflow="${esc(flow.name)}" type="button" role="checkbox" aria-checked="${selected}" aria-label="Add ${esc(flow.title)} to the batch"><i></i></button>
      <button class="wf-row-open" data-act="select-workflow" data-workflow="${esc(flow.name)}" type="button" aria-current="${flow.name === selectedWorkflow ? "true" : "false"}">
        <i data-kind="${flow.requires_model ? "llm" : "local"}"></i>
        <span><b>${esc(flow.title)}</b><small>${esc(compactSentence(flow.description, 90))}</small></span>
        ${flow.origin === "project" ? `<em>yours</em>` : ""}
      </button>
      <button class="wf-row-run" data-act="run-one" data-workflow="${esc(flow.name)}" type="button" title="Run ${esc(flow.title)}" aria-label="Run ${esc(flow.title)}">&#x25B7;</button>
    </div>`;
  }

  function renderWorkflowList() {
    role("search-wrap").hidden = false;
    const rows = workflowRows();
    if (!rows.length) {
      role("list").innerHTML = `<div class="wf-list-empty"><b>${workflows.length ? "Nothing matches that" : "No workflows"}</b><span>${workflows.length ? "Clear the search to see the whole library." : "Create one from a system prompt."}</span></div>`;
    } else {
      const group = (label, items) => items.length ? `<div class="wf-list-group"><span>${label}</span>${items.map(workflowRowMarkup).join("")}</div>` : "";
      role("list").innerHTML = group("Yours", rows.filter((flow) => flow.origin === "project")) + group("Built in", rows.filter((flow) => flow.origin !== "project"));
    }
    const count = launcherSelection.size;
    role("list-foot").innerHTML = `
      <button class="wf-text-button" data-act="select-all-runs" type="button">${count === workflows.length && count ? "Clear selection" : "Select all"}</button>
      <button class="wf-primary-button" data-act="run-selected" type="button"${count ? "" : " disabled"}>Run ${count || ""} selected</button>`;
    role("list-foot").hidden = false;
  }

  function runStateLabel(run) {
    if (run.asking) return "answering";
    if (run.state === "succeeded") return "finished";
    if (run.state === "waiting") return "decision";
    return run.state;
  }

  function runListSignature() {
    return runs.map((run) => `${run.id}:${run.state}:${run.asking ? 1 : 0}:${run.toolCount}:${run.id === selectedRun ? "*" : ""}`).join("|");
  }

  function renderRunList() {
    role("search-wrap").hidden = true;
    role("list-foot").hidden = true;
    listSignature = runListSignature();
    if (!runs.length) {
      role("list").innerHTML = `<div class="wf-list-empty"><b>No runs yet</b><span>Runs you start appear here and stay readable.</span><button data-act="new-run" type="button">Pick a workflow</button></div>`;
      return;
    }
    const active = activeRuns();
    const recent = runs.filter((run) => !active.includes(run));
    const group = (label, items) => items.length ? `<div class="wf-list-group"><span>${label}</span>${items.map((run) => `<button class="wf-run-row${run.id === selectedRun ? " is-active" : ""}" data-act="select-run" data-run="${run.id}" type="button" aria-current="${run.id === selectedRun ? "true" : "false"}"><i data-state="${run.asking ? "running" : run.state}"></i><span><b>${esc(run.flow.title)}</b><small>${esc(runStateLabel(run))} · ${esc(run.startedLabel)}${run.scope === "selection" ? " · selection" : ""}</small></span><em>${run.toolCount || ""}</em></button>`).join("")}</div>` : "";
    role("list").innerHTML = group("Running", active) + group("Finished", recent);
  }

  function renderList() {
    if (section === "runs") renderRunList();
    else renderWorkflowList();
  }

  function promptLength(flow) {
    return (flow.system_prompt || "").trim().length;
  }

  /* ------------------------------------------------------------- library */

  function noteFor(name) {
    return launcherNotes[name] ?? "";
  }

  function ensureLauncherInputs(flow) {
    if (!launcherInputs[flow.name]) {
      launcherInputs[flow.name] = Object.fromEntries((flow.inputs || []).map((item) => [item.id, item.default ?? (item.type === "boolean" ? false : "")]));
    }
    return launcherInputs[flow.name];
  }

  function captureLaunch() {
    canvas.querySelectorAll("[data-run-note]").forEach((field) => {
      launcherNotes[field.dataset.flow] = field.value;
    });
    canvas.querySelectorAll("[data-run-input]").forEach((field) => {
      const flow = field.dataset.flow;
      launcherInputs[flow] ||= {};
      const spec = workflows.find((item) => item.name === flow)?.inputs?.find((item) => item.id === field.dataset.runInput) || { type: "text" };
      launcherInputs[flow][field.dataset.runInput] = inputValue(spec, field.type === "checkbox" ? field.checked : field.value);
    });
  }

  function resolveRunScope(flow) {
    if (flow.scope === "model") return "model";
    if (flow.scope === "selection") return "selection";
    return launcherScope;
  }

  function scopeSentence(flow) {
    const total = selectionCount(viewerContext);
    const scope = resolveRunScope(flow);
    if (scope === "selection") {
      return total
        ? `Runs on the ${total} element${total === 1 ? "" : "s"} selected in the 3D view.`
        : "Needs a selection: click elements in the 3D view first.";
    }
    const model = viewerContext.models.find((row) => row.active) || viewerContext.models[0];
    return `Runs on the whole model${model?.name ? ` (${model.name})` : ""}.`;
  }

  function pipelineMarkup(flow) {
    return `<ol class="wf-pipeline">${flow.steps.map((step) => `<li data-kind="${esc(step.kind)}"><i aria-hidden="true">${STAGE_ICONS[step.kind] || "•"}</i><b>${esc(step.title || step.id)}</b><small>${esc(STAGE_WORDS[step.kind] || step.kind)}</small></li>`).join("")}</ol>`;
  }

  function renderWorkflowPage(flow) {
    const legacy = flow.inputs || [];
    const values = ensureLauncherInputs(flow);
    const settings = Object.entries(flow.settings || {});
    const scope = resolveRunScope(flow);
    const needsSelection = scope === "selection" && !selectionCount(viewerContext);
    setHeader(
      flow.origin === "project" ? "Your workflow" : "Built-in workflow",
      flow.title,
      scopeLabel(flow),
      `<button class="wf-secondary" data-act="edit-workflow" type="button">Edit</button>`,
    );
    canvas.dataset.view = "detail";
    canvas.innerHTML = `<div class="wf-detail">
      <section class="wf-card wf-hero">
        <div class="wf-hero-text">
          <span class="wf-kicker">${esc((flow.tags || []).join(" · ") || "workflow")}</span>
          <h2>${esc(flow.title)}</h2>
          <p>${esc(flow.description || "Runs a saved procedure against the current IFC context.")}</p>
          <div class="wf-facts">${flowTags(flow).map((tag) => `<span>${esc(tag)}</span>`).join("")}</div>
        </div>
        <div class="wf-hero-run">
          <label class="wf-card-prompt"><span>Prompt for this run <small>optional</small></span>
            <textarea data-run-note data-flow="${esc(flow.name)}" rows="3" maxlength="2000" placeholder="Anything specific this time: a storey, a tolerance, an element class, who reads the report.">${esc(noteFor(flow.name))}</textarea></label>
          <div class="wf-hero-actions">
            <button class="wf-primary-button wf-run-button" data-act="run-one" data-workflow="${esc(flow.name)}" type="button"${needsSelection ? " disabled" : ""}><span aria-hidden="true">&#x25B7;</span>Run</button>
            ${options.onAttach ? `<button class="wf-secondary" data-act="attach-workflow" data-workflow="${esc(flow.name)}" type="button" title="Put this workflow behind the conversation and run it there">Run in chat</button>` : ""}
            ${scope === "selection" && !needsSelection ? `<button class="wf-text-button" data-act="frame-selection" type="button">Show in 3D</button>` : ""}
          </div>
          <p class="wf-hero-scope${needsSelection ? " is-blocked" : ""}">${esc(scopeSentence(flow))}</p>
          <p class="wf-launch-error"${launcherError ? "" : " hidden"}>${esc(launcherError)}</p>
        </div>
      </section>
      <section class="wf-card wf-stages-card">
        <header><span class="wf-kicker">Pipeline</span><h3>${flow.steps.length} stage${flow.steps.length === 1 ? "" : "s"}${flow.has_gate ? ", one human decision" : ""}</h3></header>
        ${pipelineMarkup(flow)}
      </section>
      <details class="wf-prompt-fold">
        <summary><span><i aria-hidden="true"></i><b>System prompt</b><small>${promptLength(flow).toLocaleString()} characters</small></span><em>Open full prompt</em></summary>
        <pre>${esc(flow.system_prompt || "No system prompt. This workflow uses deterministic tool steps.")}</pre>
      </details>
      <div class="wf-detail-grid">
        <section class="wf-card wf-instruction-card">
          <header><span class="wf-kicker">Layered after the prompt</span><h3>Additional instructions</h3></header>
          <p>${flow.additional_instructions ? esc(flow.additional_instructions) : "None. Add project-specific rules only when the base prompt needs them."}</p>
        </section>
        <section class="wf-card wf-default-card">
          <header><span class="wf-kicker">Reusable context</span><h3>Default settings</h3></header>
          ${settings.length ? `<dl>${settings.map(([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>` : `<p>None. Anything one run needs can be typed as its run prompt instead.</p>`}
        </section>
      </div>
      ${legacy.length ? `<details class="wf-card-legacy"><summary>Values this saved workflow still asks for</summary><div class="wf-run-fields">${legacy.map((item) => runInputMarkup(item, flow.name, viewerContext, values[item.id])).join("")}</div></details>` : ""}
    </div>`;
  }

  function renderLibraryMain() {
    const flow = currentFlow();
    if (editorMode === "new") return renderWorkflowForm(null, true);
    if (!flow) {
      setHeader("Workflow library", "Choose a workflow", "Open a row to read what it does and run it.", `<button class="wf-primary-button" data-act="new-workflow" type="button">New workflow</button>`);
      canvas.dataset.view = "page";
      canvas.innerHTML = `<div class="wf-main-empty"><span aria-hidden="true">WF</span><b>Choose a workflow</b><p>The list stays compact; details open here.</p></div>`;
      return;
    }
    if (editorMode === "edit") renderWorkflowForm(flow, false);
    else renderWorkflowPage(flow);
  }

  /* -------------------------------------------------------------- editor */

  function editorSettingsMarkup(settings = {}) {
    const rows = Object.entries(settings);
    return rows.length ? rows.map(([key, value]) => settingRow(key, value, "editor")).join("") : `<div class="wf-settings-empty">No default settings</div>`;
  }

  function renderWorkflowForm(flow, isNew = false) {
    const title = isNew ? "New workflow" : `Edit ${flow.title}`;
    setHeader(isNew ? "Authoring" : "Workflow editor", title, isNew ? "Create a reusable agent procedure." : "Saving a built-in creates a project override.", "");
    const currentAgent = (flow?.agents || [])[0] || agents[0]?.name || "";
    canvas.dataset.view = "page";
    canvas.innerHTML = `<form class="wf-editor-form" data-role="editor-form">
      <section class="wf-form-identity">
        <label><span>Name</span><input name="title" maxlength="100" required value="${esc(flow?.title || "")}" placeholder="Door quality review"></label>
        <label><span>Short purpose</span><input name="description" maxlength="500" value="${esc(flow?.description || "")}" placeholder="What this workflow produces"></label>
        <div class="wf-form-grid">
          <label><span>Agent</span><select name="agent">${agents.map((agent) => `<option value="${esc(agent.name)}"${agent.name === currentAgent ? " selected" : ""}>${esc(agent.title)}</option>`).join("")}</select></label>
          ${isNew ? `<label><span>Skill</span><select name="skill"><option value="">No saved skill</option>${skills.map((skill) => `<option value="${esc(skill.name)}">${esc(skill.name)}</option>`).join("")}</select></label>` : ""}
          <label><span>Scope</span><select name="scope"><option value="either"${flow?.scope === "either" || !flow ? " selected" : ""}>Model or selection</option><option value="model"${flow?.scope === "model" ? " selected" : ""}>Whole model</option><option value="selection"${flow?.scope === "selection" ? " selected" : ""}>Viewer selection</option></select></label>
        </div>
        <section class="wf-form-settings"><header><span>Default settings</span><button data-act="add-setting" data-owner="editor" type="button">+ Add</button></header><div data-role="editor-settings">${editorSettingsMarkup(flow?.settings || {})}</div></section>
      </section>
      <section class="wf-form-prompts">
        <label><span>System prompt</span><textarea name="system_prompt" rows="16" maxlength="20000" ${isNew ? "required" : ""} placeholder="Define the job, evidence, tool use, uncertainty rules, and output format. Say what to do when nothing is specified, so a run never needs a form.">${esc(flow?.system_prompt || "")}</textarea></label>
        <label><span>Additional instructions <small>Optional project rules</small></span><textarea name="additional_instructions" rows="5" maxlength="8000" placeholder="Use project terminology. Never infer missing values.">${esc(flow?.additional_instructions || "")}</textarea></label>
      </section>
      <p class="wf-form-error" data-role="editor-error">${esc(editorError)}</p>
      <footer><button class="wf-secondary" data-act="cancel-edit" type="button">Cancel</button>${options.onCreateAgent ? `<button class="wf-secondary" data-act="create-agent" type="button">Create agent</button>` : ""}<span></span><button class="wf-primary-button" type="submit">${isNew ? "Create workflow" : "Save changes"}</button></footer>
    </form>`;
    canvas.querySelector('[data-role="editor-form"]')?.addEventListener("submit", (event) => {
      event.preventDefault();
      void saveEditor(isNew);
    });
  }

  /* ----------------------------------------------------------------- run */

  function entryNode(entry) {
    const host = document.createElement("div");
    host.innerHTML = runEntryMarkup(entry);
    return host.firstElementChild;
  }

  function runEntryMarkup(entry) {
    if (entry.kind === "user") return `<article class="wf-chat-entry wf-chat-user" data-entry="${entry.id}"><header><span aria-hidden="true">&#x276F;</span><b>You</b><time>${entry.time}</time></header><div>${esc(entry.detail)}</div></article>`;
    if (entry.kind === "content") return `<article class="wf-chat-entry wf-chat-agent" data-entry="${entry.id}"><header><span aria-hidden="true">&#x2726;</span><b>Agent</b><time>${entry.time}</time></header><div data-role="entry-body">${md(entry.detail || "Working…")}</div></article>`;
    if (entry.kind === "context") return `<article class="wf-run-context" data-entry="${entry.id}"><header><span class="wf-kicker">Run context</span><b>${esc(entry.title)}</b></header><p>${esc(entry.summary || "")}</p><details><summary>Prompt and settings</summary><pre data-role="entry-body">${esc(entry.detail)}</pre></details></article>`;
    if (["call", "result", "progress"].includes(entry.kind)) return `<details class="wf-tool-entry" data-entry="${entry.id}"${entry.open ? " open" : ""}><summary><i data-kind="${entry.kind}"></i><b>${esc(entry.title)}</b><time>${entry.time}</time></summary>${entry.detail ? `<pre>${esc(entry.detail)}</pre>` : ""}</details>`;
    return `<div class="wf-run-event" data-entry="${entry.id}" data-kind="${entry.kind}"><i></i><time>${entry.time}</time><b>${esc(entry.title)}</b>${entry.detail ? `<details${entry.open ? " open" : ""}><summary>Inspect</summary><pre>${esc(entry.detail)}</pre></details>` : ""}</div>`;
  }

  function decisionMarkup(run) {
    const cards = [];
    if (run.pendingGate) {
      cards.push(`<section class="wf-run-gate"><span class="wf-kicker">Decision needed</span><b>${esc(run.pendingGate.message)}</b>${run.pendingGate.detail ? `<details><summary>Review detail</summary><div>${md(run.pendingGate.detail)}</div></details>` : ""}<div><button class="wf-primary-button" data-act="answer-gate" data-approved="true" data-run="${run.id}" type="button">Approve</button><button class="wf-secondary" data-act="answer-gate" data-approved="false" data-run="${run.id}" type="button">Stop here</button></div></section>`);
    }
    for (const approval of run.pendingApprovals) {
      cards.push(`<section class="wf-run-gate"><span class="wf-kicker">Approval needed</span><b>${esc(approval.name || "A tool")} wants to run</b>${approval.capabilities?.length ? `<p>Capabilities: ${esc(approval.capabilities.join(", "))}</p>` : ""}${approval.arguments ? `<details><summary>Arguments</summary><pre>${esc(approval.arguments)}</pre></details>` : ""}<div><button class="wf-primary-button" data-act="answer-approval" data-approved="true" data-run="${run.id}" data-request="${esc(approval.request_id)}" type="button">Allow</button><button class="wf-secondary" data-act="answer-approval" data-approved="false" data-run="${run.id}" data-request="${esc(approval.request_id)}" type="button">Deny</button></div></section>`);
    }
    return cards.join("");
  }

  function composerMarkup(run) {
    const busy = run.asking || ["queued", "running", "waiting"].includes(run.state);
    return `<form class="wf-composer" data-role="composer" data-run="${run.id}">
      <textarea data-role="composer-input" rows="1" maxlength="8000" placeholder="${busy ? "The run is still working…" : "Ask a follow-up about this run"}"${busy ? " disabled" : ""}>${esc(run.draft || "")}</textarea>
      <button class="wf-primary-button" type="submit"${busy ? " disabled" : ""}>Ask</button>
    </form>`;
  }

  function stagebarMarkup(run) {
    return run.flow.steps.map((step) => `<span data-step="${esc(step.id)}" data-state="${run.steps[step.id] || "pending"}"><i></i>${esc(step.title || step.id)}</span>`).join("");
  }

  function runHeader(run) {
    const state = runStateLabel(run);
    const busy = run.asking || ["queued", "running", "waiting"].includes(run.state);
    const actions = `<div class="wf-run-tabs" role="tablist"><button data-act="run-view" data-view="activity" type="button" role="tab" aria-selected="${runView === "activity"}">Activity</button><button data-act="run-view" data-view="result" type="button" role="tab" aria-selected="${runView === "result"}"${run.report ? "" : " disabled"}>Result</button></div>${busy ? `<button class="wf-secondary" data-act="stop-run" data-run="${run.id}" type="button">Stop</button>` : `<button class="wf-secondary" data-act="rerun" data-run="${run.id}" type="button">Run again</button>`}`;
    setHeader("Workflow run", run.flow.title, `${state} · ${run.startedLabel} · ${run.toolCount} tools · ${run.usageIn + run.usageOut} tokens`, actions);
  }

  /** Build the run frame once; patchRunDetail keeps it current afterwards. */
  function renderRunDetail(run) {
    const field = canvas.querySelector('[data-role="composer-input"]');
    const hadFocus = field === document.activeElement;
    if (field) run.draft = field.value;
    runHeader(run);
    if (runView === "result") {
      canvas.dataset.view = "result";
      canvas.dataset.run = "";
      canvas.innerHTML = `<div class="wf-run-result">${md(run.report || "No result was produced.")}</div>${composerMarkup(run)}`;
      if (hadFocus) canvas.querySelector('[data-role="composer-input"]')?.focus({ preventScroll: true });
      return;
    }
    canvas.dataset.view = "run";
    canvas.dataset.run = run.id;
    canvas.innerHTML = `<div class="wf-run-stagebar" data-role="stagebar">${stagebarMarkup(run)}</div><div class="wf-run-thread" data-role="thread"><div data-role="entries"></div><div data-role="decisions"></div></div>${composerMarkup(run)}`;
    run.painted = 0;
    run.paintedTurn = -1;
    patchRunDetail(run, { force: true });
    if (hadFocus) canvas.querySelector('[data-role="composer-input"]')?.focus({ preventScroll: true });
  }

  /** Append what is new, repaint the one entry still growing, keep the scroll. */
  function patchRunDetail(run, { force = false } = {}) {
    if (canvas.dataset.run !== run.id) return renderRunDetail(run);
    const thread = canvas.querySelector('[data-role="thread"]');
    const entries = canvas.querySelector('[data-role="entries"]');
    const decisions = canvas.querySelector('[data-role="decisions"]');
    if (!thread || !entries || !decisions) return renderRunDetail(run);
    const follow = force || thread.scrollHeight - thread.scrollTop - thread.clientHeight < 50;
    if (run.entries.length < run.painted) {
      entries.innerHTML = "";
      run.painted = 0;
    }
    if (run.painted < run.entries.length) {
      const fragment = document.createDocumentFragment();
      for (const entry of run.entries.slice(run.painted)) fragment.appendChild(entryNode(entry));
      entries.appendChild(fragment);
      run.painted = run.entries.length;
    }
    for (const entry of run.entries) {
      if (!entry.dirty) continue;
      entry.dirty = false;
      const body = entries.querySelector(`[data-entry="${entry.id}"] [data-role="entry-body"]`);
      if (!body) continue;
      if (entry.kind === "content") body.innerHTML = md(entry.detail || "Working…");
      else body.textContent = entry.detail;
    }
    const stagebar = canvas.querySelector('[data-role="stagebar"]');
    if (stagebar) {
      for (const cell of stagebar.querySelectorAll("[data-step]")) {
        cell.dataset.state = run.steps[cell.dataset.step] || "pending";
      }
    }
    const decisionKey = `${run.pendingGate?.request_id || ""}|${run.pendingApprovals.map((item) => item.request_id).join(",")}`;
    if (decisions.dataset.key !== decisionKey) {
      decisions.dataset.key = decisionKey;
      decisions.innerHTML = decisionMarkup(run);
    }
    const busy = run.asking || ["queued", "running", "waiting"].includes(run.state);
    const composer = canvas.querySelector('[data-role="composer"]');
    if (composer && composer.dataset.busy !== String(busy)) {
      composer.dataset.busy = String(busy);
      const field = composer.querySelector('[data-role="composer-input"]');
      const button = composer.querySelector("button");
      field.disabled = busy;
      field.placeholder = busy ? "The run is still working…" : "Ask a follow-up about this run";
      button.disabled = busy;
    }
    runHeader(run);
    if (follow) thread.scrollTop = thread.scrollHeight;
  }

  function renderRunsMain() {
    const run = currentRun();
    if (!run) {
      setHeader("Runs", runs.length ? "Select a run" : "No runs yet", runs.length ? "Open a run to read it and ask follow-ups." : "Start one from the Library.", `<button class="wf-primary-button" data-act="new-run" type="button">Pick a workflow</button>`);
      canvas.dataset.view = "page";
      canvas.dataset.run = "";
      canvas.innerHTML = `<div class="wf-main-empty"><span aria-hidden="true">&#x25B7;</span><b>${runs.length ? "Choose a run" : "Nothing has run yet"}</b><p>Runs stream here and stay readable after they finish.</p></div>`;
      return;
    }
    renderRunDetail(run);
  }

  function renderMain() {
    if (section === "runs") renderRunsMain();
    else renderLibraryMain();
  }

  function renderAll() {
    renderNav();
    renderList();
    renderMain();
  }

  function schedulePaint() {
    if (paintFrame) return;
    paintFrame = requestAnimationFrame(() => {
      paintFrame = 0;
      renderNav();
      if (section !== "runs") return;
      if (runListSignature() !== listSignature) renderRunList();
      const run = currentRun();
      if (!run) return;
      if (runView === "result") {
        if (run.dirtyReport) {
          run.dirtyReport = false;
          renderRunDetail(run);
        }
        return;
      }
      patchRunDetail(run);
    });
  }

  function collectSettings(owner) {
    const values = {};
    canvas.querySelectorAll(`[data-setting-owner="${owner}"]`).forEach((row) => {
      const key = row.querySelector("[data-setting-key]")?.value.trim() || "";
      const value = row.querySelector("[data-setting-value]")?.value.trim() || "";
      if (key && value) values[key] = value;
    });
    return values;
  }

  function addSetting(owner) {
    const host = canvas.querySelector(`[data-role="${owner}-settings"]`);
    if (!host) return;
    host.querySelector(".wf-settings-empty")?.remove();
    host.insertAdjacentHTML("beforeend", settingRow("", "", owner));
    host.lastElementChild?.querySelector("input")?.focus({ preventScroll: true });
  }

  async function saveEditor(isNew) {
    const form = canvas.querySelector('[data-role="editor-form"]');
    if (!form?.reportValidity()) return;
    const data = Object.fromEntries(new FormData(form));
    data.settings = collectSettings("editor");
    if (isNew) {
      data.prompt = data.system_prompt;
      data.instructions = data.additional_instructions;
      delete data.system_prompt;
      delete data.additional_instructions;
    } else {
      data.workflow = selectedWorkflow;
    }
    editorError = "";
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = "Saving";
    try {
      const response = await request(`/api/agents/workflows/${isNew ? "create" : "update"}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        editorError = payload.hint || payload.error || "The workflow could not be saved";
        renderWorkflowForm(isNew ? null : currentFlow(), isNew);
        return;
      }
      editorMode = "view";
      await load(payload.workflow?.name || selectedWorkflow);
    } catch {
      editorError = "The console could not be reached";
      renderWorkflowForm(isNew ? null : currentFlow(), isNew);
    }
  }

  function newRun(flow) {
    const scope = resolveRunScope(flow);
    const contextName = scope === "selection" ? `${selectionCount(viewerContext)} selected elements` : viewerContext.models.find((row) => row.active)?.name || "Open model";
    return {
      id: `run-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      flow,
      state: "queued",
      startedLabel: nowLabel(),
      scope,
      scopeName: contextName,
      settings: { ...(flow.settings || {}) },
      guidance: noteFor(flow.name).trim(),
      inputs: { ...(launcherInputs[flow.name] || {}) },
      entries: [],
      entrySequence: 0,
      painted: 0,
      steps: {},
      report: "",
      dirtyReport: false,
      toolCount: 0,
      usageIn: 0,
      usageOut: 0,
      aborter: new AbortController(),
      pendingGate: null,
      pendingApprovals: [],
      pendingQuestion: "",
      followUps: [],
      turn: 0,
      asking: false,
      draft: "",
      reasoningShown: false,
    };
  }

  function addRunEntry(run, kind, title, detail = "", extra = {}) {
    run.entries.push({
      id: ++run.entrySequence,
      kind,
      title,
      detail: clipText(valueText(detail), DETAIL_LIMIT),
      time: nowLabel(),
      summary: extra.summary || "",
      open: Boolean(extra.open),
      key: extra.key || "",
      dirty: false,
    });
    if (run.entries.length > ENTRY_LIMIT) {
      run.entries = run.entries.slice(-ENTRY_LIMIT);
      run.painted = 0;
    }
  }

  function applyRunEvent(run, event) {
    run.eventsSeen = (run.eventsSeen || 0) + 1;
    if (event.type === "workflow_started") {
      run.state = "running";
      run.backendId = event.run_id;
      addRunEntry(run, "context", "Scope and instructions attached", event.scope?.text || "", { summary: run.scopeName });
    } else if (event.type === "workflow_context") {
      const context = [event.system_prompt, Object.keys(event.settings || {}).length ? `Run settings\n${valueText(event.settings)}` : "", event.guidance ? `Run prompt\n${event.guidance}` : ""].filter(Boolean).join("\n\n");
      const entry = run.entries.find((item) => item.kind === "context");
      if (entry) {
        entry.detail = clipText(context, DETAIL_LIMIT * 4);
        entry.dirty = true;
      } else addRunEntry(run, "context", "Scope and instructions attached", context, { summary: run.scopeName });
    } else if (event.type === "step_started") {
      run.steps[event.id] = "running";
      addRunEntry(run, "step", event.title || event.id, event.arguments || "");
    } else if (event.type === "step_finished") {
      run.steps[event.id] = event.state;
      addRunEntry(run, event.state === "failed" ? "error" : "step", `${event.title || event.id}: ${event.state}`, event.error || "");
    } else if (event.type === "agent_started") {
      addRunEntry(run, "agent", `${event.agent || "Agent"} started`, event.model || "");
    } else if (event.type === "content") {
      const key = `content:${event.step_id || "agent"}:${run.turn}`;
      let entry = [...run.entries].reverse().find((item) => item.key === key);
      if (!entry) {
        addRunEntry(run, "content", "Agent response", event.text || "", { key });
      } else {
        entry.detail = clipText(entry.detail + (event.text || ""), DETAIL_LIMIT * 8);
        entry.dirty = true;
      }
    } else if (event.type === "reasoning" && !run.reasoningShown) {
      run.reasoningShown = true;
      addRunEntry(run, "reasoning", "Model is working through the task", "Hidden reasoning is not displayed. Observable tool activity remains visible.");
    } else if (event.type === "tool_call") {
      run.toolCount += 1;
      addRunEntry(run, "call", event.name || "Tool call", event.arguments || "");
    } else if (event.type === "tool_progress") {
      addRunEntry(run, "progress", `${event.name || "Tool"}: ${event.done || 0}${event.total ? ` / ${event.total}` : ""}`, event.note || "");
    } else if (event.type === "tool_result") {
      // The preview is what a reader inspects; the full envelope stays on
      // the console and would only make the page heavier.
      const detail = event.ok === false
        ? [event.summary, event.detail].filter(Boolean).join("\n")
        : event.preview || event.summary || "";
      addRunEntry(run, "result", `${event.name || "Tool"} ${event.ok === false ? "failed" : "returned"}${event.rows !== undefined && event.rows !== null ? ` ${event.rows} row(s)` : ""}`, detail);
    } else if (event.type === "usage") {
      run.usageIn += Number(event.in || 0);
      run.usageOut += Number(event.out || 0);
    } else if (event.type === "approval") {
      // Without a card here the run stalls behind a decision nobody can see.
      run.pendingApprovals.push(event);
      addRunEntry(run, "gate", `${event.name || "A tool"} is waiting for approval`, event.arguments || "");
    } else if (event.type === "approval_decided") {
      run.pendingApprovals = run.pendingApprovals.filter((item) => item.id !== event.id);
      addRunEntry(run, event.approved ? "step" : "error", `${event.name || "Tool"} ${event.approved ? "approved" : "denied"}`, event.reason || "");
    } else if (event.type === "gate_requested") {
      run.state = "waiting";
      run.pendingGate = event;
      addRunEntry(run, "gate", "Waiting for a decision", event.message);
    } else if (event.type === "gate_resolved") {
      run.pendingGate = null;
      run.state = event.approved ? "running" : "failed";
    } else if (event.type === "workflow_completed") {
      run.state = event.state;
      run.report = event.summary || "";
      run.dirtyReport = true;
      addRunEntry(run, event.state === "succeeded" ? "complete" : "error", `Workflow ${event.state}`, "");
    } else if (event.type === "follow_up_completed") {
      run.followUps.push({ question: run.pendingQuestion, answer: event.text || "" });
      run.pendingQuestion = "";
      if (event.state === "failed") addRunEntry(run, "error", "Follow-up failed", event.error || "The agent could not answer.", { open: true });
    } else if (event.type === "error") {
      run.state = "failed";
      addRunEntry(run, "error", "Run error", event.text || "Unknown error", { open: true });
    }
    schedulePaint();
  }

  async function readStream(run, response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parsed = parseEventStream(buffer);
      buffer = parsed.rest;
      for (const event of parsed.events) if (event.type !== "done") applyRunEvent(run, event);
    }
  }

  async function executeRun(run) {
    try {
      const response = await request("/api/agents/workflows/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workflow: run.flow.name, inputs: run.inputs, settings: run.settings, scope: run.scope, note: run.guidance }), signal: run.aborter.signal });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        run.state = "failed";
        addRunEntry(run, "error", "Could not start", payload.hint || payload.error || `Server returned ${response.status}`, { open: true });
        return schedulePaint();
      }
      await readStream(run, response);
      if (!["succeeded", "failed"].includes(run.state)) {
        run.state = "failed";
        addRunEntry(run, "error", "Run interrupted", "The event stream ended before completion.");
      }
    } catch (error) {
      run.state = error.name === "AbortError" ? "stopped" : "failed";
      addRunEntry(run, run.state === "stopped" ? "stop" : "error", run.state === "stopped" ? "Run stopped" : "Run failed", error.name === "AbortError" ? "Stopped by user." : error.message || "Unexpected error");
    } finally {
      run.pendingApprovals = [];
      schedulePaint();
    }
  }

  /* A finished run is a conversation, not a receipt: the next question keeps
   * the same workflow prompt, tools, and scope. */
  async function askFollowUp(run, message) {
    run.asking = true;
    run.turn += 1;
    run.pendingQuestion = message;
    run.draft = "";
    run.aborter = new AbortController();
    addRunEntry(run, "user", "You", message);
    if (runView === "result") {
      runView = "activity";
      renderRunDetail(run);
    }
    schedulePaint();
    try {
      const response = await request("/api/agents/workflows/continue", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workflow: run.flow.name,
          run_id: run.backendId || run.id,
          message,
          report: run.report,
          history: run.followUps,
          settings: run.settings,
          scope: run.scope,
          note: run.guidance,
        }),
        signal: run.aborter.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        addRunEntry(run, "error", "Could not ask", payload.hint || payload.error || `Server returned ${response.status}`, { open: true });
        return;
      }
      await readStream(run, response);
    } catch (error) {
      const stopped = error.name === "AbortError";
      addRunEntry(run, stopped ? "stop" : "error", stopped ? "Follow-up stopped" : "Follow-up failed", stopped ? "Stopped by user." : error.message || "Unexpected error");
    } finally {
      run.asking = false;
      run.pendingApprovals = [];
      schedulePaint();
    }
  }

  /* Refusing a run lands on the library page of the workflow that has to
   * change, so the reason sits next to the thing it is about. */
  function refuse(message, flowName = "") {
    launcherError = message;
    section = "launch";
    if (flowName) selectedWorkflow = flowName;
    renderAll();
  }

  function pruneRuns() {
    while (runs.length > RUN_LIMIT) {
      const index = runs.map((run) => run).reverse().findIndex((run) => !activeRuns().includes(run));
      if (index < 0) break;
      const victim = runs[runs.length - 1 - index];
      runs = runs.filter((run) => run !== victim);
      if (selectedRun === victim.id) selectedRun = runs[0]?.id || "";
    }
  }

  function startRuns(selected) {
    launcherError = "";
    if (!selected.length) return refuse("Pick at least one workflow.");
    const unscoped = selected.find((flow) => resolveRunScope(flow) === "selection");
    if (unscoped && !selectionCount(viewerContext)) {
      return refuse("Select elements in the 3D viewer before starting this workflow.", unscoped.name);
    }
    for (const flow of selected) {
      const missing = missingInputs(flow.inputs || [], ensureLauncherInputs(flow));
      if (missing.length) return refuse(`${flow.title}: choose ${missing.join(", ")}.`, flow.name);
    }
    const started = selected.map(newRun);
    runs.unshift(...started);
    pruneRuns();
    selectedRun = started[0].id;
    section = "runs";
    runView = "activity";
    renderAll();
    started.forEach((run) => { void executeRun(run); });
  }

  function runSelected() {
    captureLaunch();
    startRuns(workflows.filter((flow) => launcherSelection.has(flow.name)));
  }

  function runOne(name) {
    captureLaunch();
    const flow = workflows.find((item) => item.name === name);
    if (flow) startRuns([flow]);
  }

  async function decide(run, requestId, approved, onAccepted) {
    try {
      const response = await request("/api/agents/approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: requestId, approved }) });
      if (!response.ok) throw new Error("Decision was not accepted");
      onAccepted();
    } catch (error) {
      addRunEntry(run, "error", "Could not send decision", error.message || "Unknown error");
    }
    schedulePaint();
  }

  function answerGate(run, approved) {
    if (!run.pendingGate) return;
    void decide(run, run.pendingGate.request_id, approved, () => {
      run.pendingGate = null;
      run.state = approved ? "running" : "failed";
    });
  }

  function answerApproval(run, requestId, approved) {
    void decide(run, requestId, approved, () => {
      run.pendingApprovals = run.pendingApprovals.filter((item) => item.request_id !== requestId);
    });
  }

  function rerun(previous) {
    const run = { ...newRun(previous.flow), scope: previous.scope, scopeName: previous.scopeName, settings: { ...previous.settings }, inputs: { ...previous.inputs }, guidance: previous.guidance };
    runs.unshift(run);
    pruneRuns();
    selectedRun = run.id;
    runView = "activity";
    renderAll();
    void executeRun(run);
  }

  function switchAgent() {
    if (options.onClose) {
      options.onClose();
      return;
    }
    const tokenHash = workflowToken ? `#t=${encodeURIComponent(workflowToken)}` : "";
    location.assign(`/viewer?panel=agents${tokenHash}`);
  }

  function goto(next) {
    if (section === "launch") captureLaunch();
    section = next;
    editorMode = "view";
    launcherError = "";
    renderAll();
  }

  async function load(preferred = "") {
    role("list").innerHTML = `<div class="wf-list-empty"><b>Loading</b><span>Reading workflows and agents.</span></div>`;
    try {
      const response = await request("/api/agents/workflows");
      if (!response.ok) throw new Error(response.status === 401 || response.status === 403 ? "Reopen Workflows from IFC Console." : `Server returned ${response.status}`);
      const payload = await response.json();
      workflows = Array.isArray(payload.workflows) ? payload.workflows : [];
      agents = Array.isArray(payload.agents) ? payload.agents : [];
      skills = Array.isArray(payload.skills) ? payload.skills : [];
      if (!options.viewer && payload.viewer) viewerContext = normalizeViewerContext(payload.viewer);
      selectedWorkflow = workflows.find((flow) => flow.name === preferred)?.name || workflows.find((flow) => flow.name === selectedWorkflow)?.name || workflows.find((flow) => flow.name === options.initial)?.name || workflows[0]?.name || "";
      renderAll();
      options.onCatalog?.(workflows);
    } catch (error) {
      setHeader("Workflow library", "Could not load workflows", error.message || "The console could not be reached.", "");
      canvas.dataset.view = "page";
      canvas.innerHTML = `<div class="wf-main-empty"><b>Workflow library unavailable</b><button class="wf-primary-button" data-act="reload" type="button">Try again</button></div>`;
    }
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button || !root.contains(button)) return;
    const action = button.dataset.act;
    if (action === "section") goto(button.dataset.section);
    else if (action === "toggle-list") { listOpen = !listOpen; renderNav(); }
    else if (action === "switch-agent") switchAgent();
    else if (action === "select-workflow") { captureLaunch(); selectedWorkflow = button.dataset.workflow; section = "launch"; editorMode = "view"; launcherError = ""; renderAll(); }
    else if (action === "new-workflow") { captureLaunch(); section = "launch"; editorMode = "new"; editorError = ""; renderAll(); }
    else if (action === "edit-workflow") { captureLaunch(); editorMode = "edit"; editorError = ""; renderNav(); renderLibraryMain(); }
    else if (action === "cancel-edit") { editorMode = "view"; editorError = ""; renderNav(); renderLibraryMain(); }
    else if (action === "create-agent") options.onCreateAgent?.();
    else if (action === "attach-workflow") { captureLaunch(); const flow = workflows.find((item) => item.name === button.dataset.workflow); if (flow) options.onAttach?.(flow.name, { scope: resolveRunScope(flow), note: noteFor(flow.name).trim() }); }
    else if (action === "new-run") goto("launch");
    else if (action === "select-run") { section = "runs"; selectedRun = button.dataset.run; runView = "activity"; renderAll(); }
    else if (action === "toggle-run-choice") { captureLaunch(); const name = button.dataset.workflow; if (launcherSelection.has(name)) launcherSelection.delete(name); else launcherSelection.add(name); renderWorkflowList(); }
    else if (action === "select-all-runs") { captureLaunch(); if (launcherSelection.size === workflows.length) launcherSelection.clear(); else workflows.forEach((flow) => launcherSelection.add(flow.name)); renderWorkflowList(); }
    else if (action === "launcher-scope") { captureLaunch(); launcherScope = button.dataset.scope; renderNav(); if (section === "launch" && !editing()) renderLibraryMain(); }
    else if (action === "frame-selection") options.viewer?.execute?.({ action: "focus-selection" });
    else if (action === "add-setting") addSetting(button.dataset.owner || "editor");
    else if (action === "remove-setting") { button.closest("[data-setting-owner]")?.remove(); }
    else if (action === "run-selected") runSelected();
    else if (action === "run-one") runOne(button.dataset.workflow);
    else if (action === "run-view") { runView = button.dataset.view; const run = currentRun(); if (run) renderRunDetail(run); }
    else if (action === "stop-run") runs.find((run) => run.id === button.dataset.run)?.aborter.abort();
    else if (action === "rerun") { const run = runs.find((item) => item.id === button.dataset.run); if (run) rerun(run); }
    else if (action === "answer-gate") { const run = runs.find((item) => item.id === button.dataset.run); if (run) answerGate(run, button.dataset.approved === "true"); }
    else if (action === "answer-approval") { const run = runs.find((item) => item.id === button.dataset.run); if (run) answerApproval(run, button.dataset.request, button.dataset.approved === "true"); }
    else if (action === "reload") void load();
  });

  root.addEventListener("submit", (event) => {
    const form = event.target.closest('[data-role="composer"]');
    if (!form) return;
    event.preventDefault();
    const field = form.querySelector('[data-role="composer-input"]');
    const message = (field?.value || "").trim();
    const run = runs.find((item) => item.id === form.dataset.run);
    if (run && message) {
      field.value = "";
      void askFollowUp(run, message);
    }
  });

  root.addEventListener("input", (event) => {
    const target = event.target;
    if (target.dataset.role === "search") { query = target.value; renderWorkflowList(); }
    else if (target.dataset.role === "composer-input") { const run = currentRun(); if (run) run.draft = target.value; }
    else if (target.hasAttribute?.("data-run-note")) launcherNotes[target.dataset.flow] = target.value;
  });

  root.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && event.target.dataset?.role === "composer-input") {
      event.preventDefault();
      event.target.form?.requestSubmit();
      return;
    }
    if (event.key !== "/" || /^(INPUT|TEXTAREA|SELECT)$/.test(event.target.tagName)) return;
    event.preventDefault();
    if (section !== "launch") { section = "launch"; editorMode = "view"; renderAll(); }
    listOpen = true;
    renderNav();
    role("search").focus();
  });

  const viewerSignature = () =>
    `${selectionCount(viewerContext)}|${viewerContext.models.map((row) => `${row.id}${row.active ? "*" : ""}`).join(",")}`;
  const onViewer = (value) => {
    const before = viewerSignature();
    viewerContext = normalizeViewerContext(value);
    if (viewerSignature() === before) return;
    // Selection events are chatty and the person may be typing a run prompt,
    // so only the scope control and the scope line move; the page stays.
    renderNav();
    if (section !== "launch" || editing()) return;
    const flow = currentFlow();
    if (!flow) return;
    const line = canvas.querySelector(".wf-hero-scope");
    const run = canvas.querySelector(".wf-run-button");
    if (!line || !run) return;
    const blocked = resolveRunScope(flow) === "selection" && !selectionCount(viewerContext);
    line.textContent = scopeSentence(flow);
    line.classList.toggle("is-blocked", blocked);
    run.disabled = blocked;
  };
  let unsubscribeViewer = null;
  if (options.viewer?.subscribe) {
    unsubscribeViewer = options.viewer.subscribe(onViewer);
  } else {
    const listener = (event) => onViewer(event.detail);
    document.addEventListener("ifc-console:viewer-context", listener);
    unsubscribeViewer = () => document.removeEventListener("ifc-console:viewer-context", listener);
  }

  void load();
  return {
    refresh: load,
    focus() {
      // refresh() may still be in flight, so fall back to the tabs.
      const target = (section === "launch" && role("search")) || root.querySelector('[data-act="section"]');
      target?.focus({ preventScroll: true });
    },
    stop() { runs.forEach((run) => run.aborter?.abort()); },
    create() { section = "launch"; editorMode = "new"; editorError = ""; renderAll(); },
    /** Open the library with a scope already chosen, e.g. from the viewer's selection. */
    launch({ scope = "" } = {}) {
      if (section === "launch") captureLaunch();
      section = "launch";
      editorMode = "view";
      if (scope === "selection" && selectionCount(viewerContext)) launcherScope = "selection";
      else if (scope === "model") launcherScope = "model";
      renderAll();
      this.focus();
    },
    dispose() { runs.forEach((run) => run.aborter?.abort()); if (paintFrame) cancelAnimationFrame(paintFrame); unsubscribeViewer?.(); },
  };
}

export const mountPanel = mountWorkflows;
export default mountWorkflows;
