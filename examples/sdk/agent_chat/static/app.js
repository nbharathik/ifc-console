const state = {
  token: "",
  threadId: sessionStorage.getItem("ifc-sdk-thread") || crypto.randomUUID(),
  activeRun: null,
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
  prompt.disabled = value;
  send.textContent = value ? "Stop" : "Run";
  send.classList.toggle("stop", value);
  runStatus.textContent = value ? "Building an evidence-backed answer…" : "Ready";
}

function ownsRun(runState) {
  return state.activeRun === runState;
}

function finishRun(runState, status = "Ready") {
  if (!ownsRun(runState)) return false;
  state.activeRun = null;
  setRunning(false);
  runStatus.textContent = status;
  return true;
}

function stopActiveRun() {
  const runState = state.activeRun;
  if (!runState) return false;
  state.activeRun = null;
  runState.controller.abort();
  setRunning(false);
  runStatus.textContent = "Run stopped";
  return true;
}

function resetLedger(runState = null) {
  if (runState) {
    runState.callCount = 0;
    runState.ledger.clear();
  }
  const empty = runState
    ? "The run is active. Operations will appear here as the model requests them."
    : "Tool calls, approvals, and results will be recorded here as the answer is built.";
  ledger.innerHTML = `<div class="ledger-empty">${empty}</div>`;
  $('[data-role="ledger-count"]').textContent = "0 calls";
  $('[data-role="usage"]').textContent = "No token usage yet";
}

function ledgerItem(event, runState) {
  if (!runState.callCount) ledger.innerHTML = "";
  runState.callCount += 1;
  const item = document.createElement("article");
  item.className = "ledger-item running";
  item.dataset.callId = event.tool_call_id;
  const seq = document.createElement("span");
  seq.className = "ledger-seq";
  seq.textContent = String(runState.callCount).padStart(2, "0");
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
  runState.ledger.set(event.tool_call_id, item);
  $('[data-role="ledger-count"]').textContent = `${runState.callCount} call${runState.callCount === 1 ? "" : "s"}`;
  ledger.scrollTop = ledger.scrollHeight;
}

async function resolveApproval(runState, requestId, approved, buttons) {
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const reason = approved
      ? "Approved in the reference chat" : "Denied in the reference chat";
    const response = await api(`/api/sdk/approvals/${encodeURIComponent(requestId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approved, reason }),
      signal: runState.controller.signal,
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(detail.error || `server returned HTTP ${response.status}`);
    }
    if (ownsRun(runState)) {
      runStatus.textContent = "Decision sent. Waiting for the run to continue…";
    }
  } catch (error) {
    if (!ownsRun(runState)) return;
    buttons.forEach((button) => { button.disabled = false; });
    const detail = error instanceof Error && error.message
      ? error.message : "the network request failed";
    runStatus.textContent = `Could not send the approval: ${detail}. Retry the decision or stop the run.`;
  }
}

function showApproval(event, runState) {
  const item = runState.ledger.get(event.tool_call_id);
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
  approve.addEventListener("click", () => {
    resolveApproval(runState, event.approval.request_id, true, [approve, deny]);
  });
  deny.addEventListener("click", () => {
    resolveApproval(runState, event.approval.request_id, false, [approve, deny]);
  });
}

function handleEvent(event, runState) {
  if (!ownsRun(runState)) return;
  switch (event.type) {
    case "run_started":
      resetLedger(runState);
      runState.assistant = addMessage("assistant", "");
      break;
    case "text_delta":
      if (!runState.assistant) runState.assistant = addMessage("assistant", "");
      runState.assistant.textContent += event.text || "";
      messages.scrollTop = messages.scrollHeight;
      break;
    case "tool_call_started":
      ledgerItem(event, runState);
      break;
    case "approval_requested":
      showApproval(event, runState);
      break;
    case "approval_resolved": {
      const item = runState.ledger.get(event.tool_call_id);
      if (item) {
        item.querySelector(".ledger-state").textContent = event.decision?.approved
          ? "approved" : "denied";
      }
      break;
    }
    case "tool_call_finished": {
      const item = runState.ledger.get(event.tool_call_id);
      if (item) {
        const ok = Boolean(event.result?.ok);
        item.classList.remove("running", "approval");
        item.classList.add(ok ? "ok" : "failed");
        item.querySelector(".ledger-state").textContent = ok
          ? "ok" : (event.result?.error?.code || "failed").toLowerCase();
        item.querySelector(".approval-actions")?.remove();
      }
      break;
    }
    case "usage":
      $('[data-role="usage"]').textContent = `${event.usage?.input_tokens ?? "—"} in / ${event.usage?.output_tokens ?? "—"} out`;
      break;
    case "run_failed":
      if (runState.assistant && !runState.assistant.textContent) runState.assistant.closest(".message")?.remove();
      addMessage("error", event.text || "The run failed.");
      finishRun(runState, "Run failed. Review the message and try again.");
      break;
    case "run_completed":
      finishRun(runState, "Run complete");
      break;
    default:
      break;
  }
}

async function readSse(response, runState) {
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
      if (data) handleEvent(JSON.parse(data), runState);
    }
    if (done) break;
  }
}

async function run(message) {
  const clean = message.trim();
  if (!clean || state.activeRun) return;
  const runState = {
    assistant: null,
    callCount: 0,
    controller: new AbortController(),
    ledger: new Map(),
    threadId: state.threadId,
  };
  state.activeRun = runState;
  addMessage("user", clean);
  prompt.value = "";
  setRunning(true);
  try {
    const response = await api("/api/sdk/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: clean, thread_id: runState.threadId }),
      signal: runState.controller.signal,
    });
    await readSse(response, runState);
  } catch (error) {
    if (!ownsRun(runState)) return;
    if (error.name === "AbortError") {
      if (runState.assistant && !runState.assistant.textContent) {
        runState.assistant.closest(".message")?.remove();
      }
      finishRun(runState, "Run stopped");
    } else {
      addMessage("error", error.message || "The run could not start.");
      finishRun(runState, "Run failed. Check the connection and try again.");
    }
  } finally {
    finishRun(runState, "The run ended before completion. Try again.");
  }
}

composer.addEventListener("submit", (event) => {
  event.preventDefault();
  if (state.activeRun) {
    stopActiveRun();
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
document.querySelectorAll("[data-prompt]").forEach((button) => {
  button.addEventListener("click", () => run(button.dataset.prompt));
});
$('[data-action="new"]').addEventListener("click", () => {
  stopActiveRun();
  state.threadId = crypto.randomUUID();
  sessionStorage.setItem("ifc-sdk-thread", state.threadId);
  messages.innerHTML = '<div class="welcome" data-role="welcome"><p class="coordinate">NEW THREAD / ACTIVE MODEL</p><h3>Ready for another review.</h3><p>The model stays open; only the conversation context was cleared.</p></div>';
  resetLedger();
  runStatus.textContent = "Ready";
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
