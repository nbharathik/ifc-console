const state = {
  token: "",
  threadId: sessionStorage.getItem("ifc-sdk-thread") || crypto.randomUUID(),
  running: false,
  callCount: 0,
  assistant: null,
  controller: null,
  ledger: new Map(),
};
sessionStorage.setItem("ifc-sdk-thread", state.threadId);

const hash = new URLSearchParams(location.hash.slice(1));
state.token = hash.get("t") || sessionStorage.getItem("ifc-sdk-token") || "";
if (state.token) sessionStorage.setItem("ifc-sdk-token", state.token);
if (hash.has("t")) history.replaceState(null, "", location.pathname + location.search);

const $ = (selector) => document.querySelector(selector);
const messages = $('[data-role="messages"]');
const prompt = $('[data-role="prompt"]');
const composer = $('[data-role="composer"]');
const runStatus = $('[data-role="run-status"]');
const ledger = $('[data-role="ledger"]');
const send = $(".send");

async function api(path, options = {}) {
  return fetch(path, {
    ...options,
    headers: {
      Authorization: `Bearer ${state.token}`,
      ...(options.headers || {}),
    },
  });
}

function setStatus(name, value) {
  const target = document.querySelector(`[data-status="${name}"]`);
  if (target) target.textContent = value;
}

function addMessage(role, text = "") {
  $('[data-role="welcome"]')?.remove();
  const row = document.createElement("article");
  row.className = `message ${role}`;
  const label = document.createElement("div");
  label.className = "message-label";
  label.textContent = role === "user" ? "You" : role === "error" ? "Run stopped" : "Reviewer";
  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = text;
  row.append(label, body);
  messages.append(row);
  messages.scrollTop = messages.scrollHeight;
  return body;
}

function setRunning(value) {
  state.running = value;
  prompt.disabled = value;
  send.textContent = value ? "Stop" : "Run";
  send.classList.toggle("stop", value);
  runStatus.textContent = value ? "Building an evidence-backed answer…" : "Ready";
}

function resetLedger() {
  state.callCount = 0;
  state.ledger.clear();
  ledger.innerHTML = '<div class="ledger-empty">The run is active. Operations will appear here as the model requests them.</div>';
  $('[data-role="ledger-count"]').textContent = "0 calls";
  $('[data-role="usage"]').textContent = "No token usage yet";
}

function ledgerItem(event) {
  if (!state.callCount) ledger.innerHTML = "";
  state.callCount += 1;
  const item = document.createElement("article");
  item.className = "ledger-item running";
  item.dataset.callId = event.tool_call_id;
  const seq = document.createElement("span");
  seq.className = "ledger-seq";
  seq.textContent = String(state.callCount).padStart(2, "0");
  const name = document.createElement("span");
  name.className = "ledger-name";
  name.textContent = event.tool_name;
  const status = document.createElement("span");
  status.className = "ledger-state";
  status.textContent = "running";
  const args = document.createElement("code");
  args.className = "ledger-args";
  const encoded = JSON.stringify(event.arguments || {});
  args.textContent = encoded.length > 280 ? `${encoded.slice(0, 277)}…` : encoded;
  item.append(seq, name, status, args);
  ledger.append(item);
  state.ledger.set(event.tool_call_id, item);
  $('[data-role="ledger-count"]').textContent = `${state.callCount} call${state.callCount === 1 ? "" : "s"}`;
  ledger.scrollTop = ledger.scrollHeight;
}

async function resolveApproval(requestId, approved, buttons) {
  buttons.forEach((button) => { button.disabled = true; });
  const response = await api(`/api/sdk/approvals/${encodeURIComponent(requestId)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved, reason: approved ? "Approved in the reference chat" : "Denied in the reference chat" }),
  });
  if (!response.ok) {
    runStatus.textContent = "The approval expired before the decision arrived.";
  }
}

function showApproval(event) {
  const item = state.ledger.get(event.tool_call_id);
  if (!item || !event.approval) return;
  item.classList.add("approval");
  item.querySelector(".ledger-state").textContent = "approval";
  const actions = document.createElement("div");
  actions.className = "approval-actions";
  const approve = document.createElement("button");
  approve.className = "approve";
  approve.textContent = "Approve once";
  const deny = document.createElement("button");
  deny.textContent = "Deny";
  actions.append(approve, deny);
  item.append(actions);
  approve.addEventListener("click", () => resolveApproval(event.approval.request_id, true, [approve, deny]));
  deny.addEventListener("click", () => resolveApproval(event.approval.request_id, false, [approve, deny]));
}

function handleEvent(event) {
  switch (event.type) {
    case "run_started":
      resetLedger();
      state.assistant = addMessage("assistant", "");
      break;
    case "text_delta":
      if (!state.assistant) state.assistant = addMessage("assistant", "");
      state.assistant.textContent += event.text || "";
      messages.scrollTop = messages.scrollHeight;
      break;
    case "tool_call_started":
      ledgerItem(event);
      break;
    case "approval_requested":
      showApproval(event);
      break;
    case "approval_resolved": { const item = state.ledger.get(event.tool_call_id); if (item) item.querySelector(".ledger-state").textContent = event.decision?.approved ? "approved" : "denied"; break; }
    case "tool_call_finished": { const item = state.ledger.get(event.tool_call_id); if (item) { const ok = Boolean(event.result?.ok); item.classList.remove("running", "approval"); item.classList.add(ok ? "ok" : "failed"); item.querySelector(".ledger-state").textContent = ok ? "ok" : (event.result?.error?.code || "failed").toLowerCase(); item.querySelector(".approval-actions")?.remove(); } break; }
    case "usage":
      $('[data-role="usage"]').textContent = `${event.usage?.input_tokens ?? "—"} in / ${event.usage?.output_tokens ?? "—"} out`;
      break;
    case "run_failed":
      if (state.assistant && !state.assistant.textContent) state.assistant.closest(".message")?.remove();
      addMessage("error", event.text || "The run failed.");
      setRunning(false);
      break;
    case "run_completed":
      setRunning(false);
      runStatus.textContent = "Run complete";
      break;
    default:
      break;
  }
}

async function readSse(response) {
  if (!response.ok || !response.body) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.error || `Request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const blocks = buffer.split("\n\n");
    buffer = blocks.pop() || "";
    for (const block of blocks) {
      const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5)).join("\n");
      if (data) handleEvent(JSON.parse(data));
    }
    if (done) break;
  }
}

async function run(message) {
  if (!message.trim()) return;
  addMessage("user", message.trim());
  prompt.value = "";
  setRunning(true);
  state.assistant = null;
  state.controller = new AbortController();
  try {
    const response = await api("/api/sdk/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: message.trim(), thread_id: state.threadId }),
      signal: state.controller.signal,
    });
    await readSse(response);
  } catch (error) {
    if (error.name === "AbortError") {
      if (state.assistant && !state.assistant.textContent) state.assistant.closest(".message")?.remove();
      runStatus.textContent = "Run stopped";
    } else {
      addMessage("error", error.message || "The run could not start.");
    }
    setRunning(false);
  } finally {
    state.controller = null;
    if (state.running) setRunning(false);
  }
}

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.running) {
    state.controller?.abort();
    return;
  }
  run(prompt.value);
});
prompt.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    composer.requestSubmit();
  }
});
document.querySelectorAll("[data-prompt]").forEach((button) => button.addEventListener("click", () => run(button.dataset.prompt)));
$('[data-action="new"]').addEventListener("click", () => {
  state.controller?.abort();
  state.threadId = crypto.randomUUID();
  sessionStorage.setItem("ifc-sdk-thread", state.threadId);
  messages.innerHTML = '<div class="welcome" data-role="welcome"><p class="coordinate">NEW THREAD / ACTIVE MODEL</p><h3>Ready for another review.</h3><p>The model stays open; only the conversation context was cleared.</p></div>';
  resetLedger();
  prompt.focus();
});

async function loadStatus() {
  try {
    const response = await api("/api/sdk/status");
    if (!response.ok) throw new Error("unauthorized");
    const status = await response.json();
    setStatus("file", status.model?.name || "No IFC model");
    setStatus("mode", status.mode);
    setStatus("tools", String(status.tool_count));
    setStatus("agent", status.agent);
    setStatus("model", `${status.provider} / ${status.ai_model}`);
    if (status.viewer?.enabled) {
      const viewer = $('[data-action="viewer"]');
      viewer.hidden = false;
      viewer.href = `${status.viewer.path}#t=${encodeURIComponent(state.token)}`;
    }
  } catch (_error) {
    runStatus.textContent = state.token ? "Cannot reach the SDK server." : "Open the tokenized URL printed by the server.";
    setStatus("file", "Connection unavailable");
  }
}

loadStatus();
prompt.focus();
