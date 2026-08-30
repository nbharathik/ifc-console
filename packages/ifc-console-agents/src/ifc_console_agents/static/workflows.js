/* The workflows surface: pick a preconfigured run, fill its inputs, watch it.
 *
 * One component serves both doors. The standalone /workflows page mounts it
 * full width; the agent panel mounts the same thing in an overlay. Neither
 * keeps workflow state of its own, so the two never disagree: the list comes
 * from /api/agents/workflows and a run streams from /api/agents/workflows/run.
 */
import { md, esc } from "/agents/static/chat_markdown.js";
import {
  inputValue,
  missingInputs,
  parseEventStream,
  runOutcome,
} from "/agents/static/workflows_model.js";

const STEP_ICON = {
  tool: "■",
  agent: "◆",
  gate: "▲",
  export: "●",
};

const TEMPLATE = `
<div class="wf-root">
  <aside class="wf-list" data-role="list" aria-label="Workflows">
    <div class="wf-list-head">
      <span class="wf-list-title">Workflows</span>
      <button class="wf-icon" data-act="reload" type="button"
              title="Reload workflows" aria-label="Reload workflows">&#x21BB;</button>
    </div>
    <div class="wf-list-scroll" data-role="cards"></div>
  </aside>

  <section class="wf-run" data-role="run" aria-label="Selected workflow">
    <div class="wf-empty" data-role="empty">
      <p>Pick a workflow to run it.</p>
      <p class="wf-muted">Each one is a saved sequence of steps: tools, an agent
        that reads the results, and a report at the end.</p>
    </div>

    <div class="wf-detail" data-role="detail" hidden>
      <header class="wf-detail-head">
        <div>
          <h2 class="wf-detail-title" data-role="title"></h2>
          <p class="wf-detail-desc" data-role="desc"></p>
        </div>
        <button class="wf-close" data-act="close" type="button"
                title="Close" aria-label="Close workflows" hidden>&#x2715;</button>
      </header>

      <form class="wf-inputs" data-role="inputs" autocomplete="off"></form>

      <div class="wf-actions">
        <button class="wf-run-button" data-act="run" type="button">Run workflow</button>
        <button class="wf-stop" data-act="stop" type="button" hidden>Stop</button>
        <span class="wf-status" data-role="status" role="status"></span>
      </div>

      <ol class="wf-steps" data-role="steps"></ol>

      <div class="wf-output" data-role="output" hidden>
        <h3 class="wf-output-title">Result</h3>
        <div class="wf-output-body" data-role="result"></div>
      </div>
    </div>
  </section>
</div>
`;

const STYLESHEET = "/agents/static/workflows.css";

// The panel loads this component on demand and knows nothing about its
// styles, so the component brings them. The standalone page already links
// the same href, and matching links are not added twice.
function ensureStylesheet() {
  if (document.querySelector(`link[href="${STYLESHEET}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = STYLESHEET;
  document.head.append(link);
}

export function mountWorkflows(root, options = {}) {
  ensureStylesheet();
  root.classList.add("wf-host");
  root.innerHTML = TEMPLATE;

  const role = (name) => root.querySelector(`[data-role="${name}"]`);
  const cards = role("cards");
  const detail = role("detail");
  const empty = role("empty");
  const inputsForm = role("inputs");
  const stepsList = role("steps");
  const statusText = role("status");
  const outputBox = role("output");
  const resultBox = role("result");
  const runButton = root.querySelector('[data-act="run"]');
  const stopButton = root.querySelector('[data-act="stop"]');
  const closeButton = root.querySelector('[data-act="close"]');

  let workflows = [];
  let selected = null;
  let aborter = null;
  let stepNodes = new Map();
  let pendingGate = null;

  if (options.onClose) closeButton.hidden = false;

  function status(text, kind = "") {
    statusText.textContent = text;
    statusText.dataset.kind = kind;
  }

  function renderCards() {
    if (!workflows.length) {
      cards.innerHTML = `<p class="wf-muted wf-pad">No workflows are installed.</p>`;
      return;
    }
    cards.innerHTML = workflows
      .map((flow) => {
        const tags = flow.tags.length
          ? `<span class="wf-tags">${flow.tags.map((tag) => `<i>${esc(tag)}</i>`).join("")}</span>`
          : "";
        const origin =
          flow.origin === "project"
            ? `<span class="wf-origin" title="From this project">project</span>`
            : "";
        return `
          <button class="wf-card${flow.name === selected?.name ? " is-active" : ""}"
                  data-act="pick" data-name="${esc(flow.name)}" type="button">
            <span class="wf-card-top">
              <span class="wf-card-title">${esc(flow.title)}</span>
              ${origin}
            </span>
            <span class="wf-card-desc">${esc(flow.description)}</span>
            <span class="wf-card-foot">
              <span class="wf-card-steps">${flow.steps.length} steps</span>
              ${tags}
            </span>
          </button>`;
      })
      .join("");
  }

  function renderInputs(flow) {
    if (!flow.inputs.length) {
      inputsForm.innerHTML = `<p class="wf-muted">This workflow needs no input.</p>`;
      return;
    }
    inputsForm.innerHTML = flow.inputs
      .map((item) => {
        const id = `wf-in-${esc(item.id)}`;
        const required = item.required ? ` <em class="wf-required">required</em>` : "";
        const help = item.help ? `<span class="wf-help">${esc(item.help)}</span>` : "";
        let field;
        if (item.type === "choice") {
          const options = item.choices
            .map(
              (choice) =>
                `<option value="${esc(choice)}"${
                  String(item.default ?? "") === choice ? " selected" : ""
                }>${esc(choice)}</option>`
            )
            .join("");
          field = `<select id="${id}" data-input="${esc(item.id)}">${options}</select>`;
        } else if (item.type === "boolean") {
          field = `<input id="${id}" data-input="${esc(item.id)}" type="checkbox"${
            item.default ? " checked" : ""
          }>`;
        } else {
          const type = item.type === "number" ? "number" : "text";
          field = `<input id="${id}" data-input="${esc(item.id)}" type="${type}"
                     value="${esc(String(item.default ?? ""))}">`;
        }
        return `
          <label class="wf-field${item.type === "boolean" ? " wf-field-check" : ""}">
            <span class="wf-label" for="${id}">${esc(item.label)}${required}</span>
            ${field}
            ${help}
          </label>`;
      })
      .join("");
  }

  function renderSteps(flow) {
    stepNodes = new Map();
    stepsList.innerHTML = flow.steps
      .map(
        (step) => `
        <li class="wf-step" data-step="${esc(step.id)}" data-state="pending">
          <span class="wf-step-mark" aria-hidden="true">${STEP_ICON[step.kind] || "■"}</span>
          <span class="wf-step-body">
            <span class="wf-step-title">${esc(step.title || step.id)}</span>
            <span class="wf-step-note" data-role="note"></span>
          </span>
          <span class="wf-step-state" data-role="state"></span>
        </li>`
      )
      .join("");
    for (const node of stepsList.querySelectorAll("[data-step]")) {
      stepNodes.set(node.dataset.step, node);
    }
  }

  function select(name) {
    selected = workflows.find((flow) => flow.name === name) || null;
    renderCards();
    if (!selected) {
      detail.hidden = true;
      empty.hidden = false;
      return;
    }
    empty.hidden = true;
    detail.hidden = false;
    role("title").textContent = selected.title;
    role("desc").textContent = selected.description;
    renderInputs(selected);
    renderSteps(selected);
    outputBox.hidden = true;
    resultBox.innerHTML = "";
    status("");
  }

  function collectInputs() {
    const specs = new Map((selected?.inputs || []).map((item) => [item.id, item]));
    const values = {};
    for (const field of inputsForm.querySelectorAll("[data-input]")) {
      const id = field.dataset.input;
      const raw = field.type === "checkbox" ? field.checked : field.value;
      values[id] = inputValue(specs.get(id) || { type: "text" }, raw);
    }
    return values;
  }

  function setStep(id, state, note = "") {
    const node = stepNodes.get(id);
    if (!node) return;
    node.dataset.state = state;
    const label = node.querySelector('[data-role="state"]');
    if (label) label.textContent = state === "running" ? "running" : state;
    const noteNode = node.querySelector('[data-role="note"]');
    if (noteNode && note) noteNode.textContent = note;
  }

  function clearGate() {
    if (pendingGate) {
      pendingGate.remove();
      pendingGate = null;
    }
  }

  function askGate(event) {
    clearGate();
    const node = document.createElement("div");
    node.className = "wf-gate";
    node.innerHTML = `
      <p class="wf-gate-message">${esc(event.message)}</p>
      ${event.detail ? `<div class="wf-gate-detail">${md(event.detail)}</div>` : ""}
      <div class="wf-gate-actions">
        <button class="wf-gate-yes" data-act="gate-approve" type="button">Approve</button>
        <button class="wf-gate-no" data-act="gate-reject" type="button">Stop here</button>
      </div>`;
    node.dataset.requestId = event.request_id;
    const target = stepNodes.get(event.step_id);
    if (target) target.after(node);
    else stepsList.after(node);
    pendingGate = node;
    node.querySelector(".wf-gate-yes")?.focus({ preventScroll: true });
  }

  async function answerGate(approved) {
    if (!pendingGate) return;
    const requestId = pendingGate.dataset.requestId;
    clearGate();
    try {
      await fetch("/api/agents/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId, approved }),
      });
    } catch {
      status("Could not send that decision", "error");
    }
  }

  function applyEvent(event) {
    if (event.type === "workflow_started") {
      status("Running", "busy");
    } else if (event.type === "step_started") {
      setStep(event.id, "running");
    } else if (event.type === "step_finished") {
      setStep(event.id, event.state, event.error || "");
      if (event.artifact) {
        const node = stepNodes.get(event.id);
        const note = node?.querySelector('[data-role="note"]');
        if (note) note.textContent = `saved ${event.artifact.name}`;
      }
    } else if (event.type === "gate_requested") {
      status("Waiting for your decision", "gate");
      askGate(event);
    } else if (event.type === "gate_resolved") {
      clearGate();
      status(event.approved ? "Running" : "Stopped", event.approved ? "busy" : "error");
    } else if (event.type === "tool_call") {
      status(`Running ${event.name}`, "busy");
    } else if (event.type === "workflow_completed") {
      status(event.state === "succeeded" ? "Finished" : "Did not finish", event.state === "succeeded" ? "ok" : "error");
      if (event.summary) {
        outputBox.hidden = false;
        resultBox.innerHTML = md(event.summary);
      }
    } else if (event.type === "error") {
      status(event.text || "Something went wrong", "error");
    }
  }

  async function run() {
    if (!selected || aborter) return;
    const values = collectInputs();
    const missing = missingInputs(selected.inputs || [], values);
    if (missing.length) {
      status(`Fill in ${missing.join(", ")} first`, "error");
      inputsForm.querySelector("input, select")?.focus({ preventScroll: true });
      return;
    }
    clearGate();
    outputBox.hidden = true;
    resultBox.innerHTML = "";
    renderSteps(selected);
    runButton.disabled = true;
    stopButton.hidden = false;
    status("Starting", "busy");
    aborter = new AbortController();
    const seen = [];
    let stopped = false;
    try {
      const response = await fetch("/api/agents/workflows/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow: selected.name, inputs: values }),
        signal: aborter.signal,
      });
      if (!response.ok) {
        const payload = await response.json().catch(() => ({}));
        status(payload.hint || payload.error || "The workflow could not start", "error");
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const { events, rest } = parseEventStream(buffer);
        buffer = rest;
        for (const payload of events) {
          if (payload.type === "done") continue;
          seen.push(payload);
          applyEvent(payload);
        }
      }
    } catch (error) {
      stopped = true;
      if (error.name !== "AbortError") status("The run stopped unexpectedly", "error");
      else status("Stopped", "");
    } finally {
      // A stream that ends without workflow_completed did not finish, whatever
      // the last step said. Saying "finished" there would be a lie.
      if (!stopped) {
        const outcome = runOutcome(seen);
        if (!outcome.done && seen.length) {
          status(outcome.error || "The run did not finish", "error");
        }
      }
      aborter = null;
      runButton.disabled = false;
      stopButton.hidden = true;
      clearGate();
    }
  }

  async function load() {
    try {
      const response = await fetch("/api/agents/workflows");
      if (!response.ok) {
        cards.innerHTML = `<p class="wf-muted wf-pad">Workflows are unavailable.</p>`;
        return;
      }
      const payload = await response.json();
      workflows = payload.workflows || [];
      renderCards();
      if (selected) select(selected.name);
      else if (options.initial) select(options.initial);
    } catch {
      cards.innerHTML = `<p class="wf-muted wf-pad">Could not load workflows.</p>`;
    }
  }

  root.addEventListener("click", (event) => {
    const button = event.target.closest("[data-act]");
    if (!button || !root.contains(button)) return;
    const action = button.dataset.act;
    if (action === "pick") select(button.dataset.name);
    else if (action === "run") void run();
    else if (action === "stop") aborter?.abort();
    else if (action === "reload") void load();
    else if (action === "gate-approve") void answerGate(true);
    else if (action === "gate-reject") void answerGate(false);
    else if (action === "close") options.onClose?.();
  });

  void load();

  return {
    refresh: load,
    focus() {
      (root.querySelector(".wf-card") || runButton)?.focus({ preventScroll: true });
    },
    stop() {
      aborter?.abort();
    },
  };
}

export const mountPanel = mountWorkflows;
export default mountWorkflows;
