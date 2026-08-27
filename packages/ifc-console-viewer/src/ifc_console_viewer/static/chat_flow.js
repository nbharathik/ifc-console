/* The context-flow model: what the agent is doing, in the user's language.
 *
 * The panel used to show a flat list of tool chips, which says what ran but
 * not why, and put them all above the answer. This module maps every tool to a
 * stage of the agent's pipeline and reduces the SSE event stream into an
 * ordered list of blocks, so the view can render a tool exactly where it ran.
 * Pure functions only: unit tested without a DOM.
 */

export const STAGES = [
  {
    id: "scope",
    label: "Scope",
    hint: "Find the elements the question is about",
    tools: [
      "get_ifc_project_info",
      "search_elements",
      "query_elements",
      "get_element",
      "get_psets",
      "get_spatial_structure",
      "get_viewer_selection",
      "list_models",
      "get_georeferencing",
      "get_schema_docs",
    ],
  },
  {
    id: "evidence",
    label: "Evidence",
    hint: "Read the manuals, drawings, and images",
    tools: [
      "list_project_documents",
      "search_ifc_knowledge",
      "get_knowledge_record",
      "get_project_reference_image",
      "get_project_document_page",
      "find_files",
      "get_api_docs",
    ],
  },
  {
    id: "method",
    label: "Method",
    hint: "Pick a rule, then measure or compute",
    tools: [
      "get_measurement_recipe",
      "list_agent_skills",
      "get_agent_skill",
      "save_agent_skill",
      "measure_elements",
      "measure_distance",
      "compute_quantities",
      "get_element_geometry",
      "analyze_element_geometry",
      "detect_clashes",
      "execute_ifc_code",
    ],
  },
  {
    id: "verify",
    label: "Verify",
    hint: "Check the result against the model and the 3D view",
    tools: [
      "validate_model",
      "validate_ids",
      "highlight_elements",
      "apply_color_theme",
      "get_viewer_screenshot",
      "get_viewer_measurements",
      "control_viewer",
      "orient",
      "list_ai_authored_properties",
      "get_change_set",
      "export_csv",
      "export_measurement_report",
    ],
  },
  {
    id: "propose",
    label: "Propose",
    hint: "Prepare a reviewable, AI-marked change",
    tools: [
      "measure__propose_measured_value",
      "measure__propose_property_value",
      "preview_property_change",
      "preview_classification_assignment",
    ],
  },
];

const STAGE_OF = new Map();
for (const [index, stage] of STAGES.entries()) {
  for (const tool of stage.tools) STAGE_OF.set(tool, index);
}

export function stageOf(toolName) {
  const index = STAGE_OF.get(toolName);
  return index === undefined ? -1 : index;
}

export function stageLabel(index) {
  return STAGES[index]?.label ?? "";
}

/** Which stages an agent can reach at all, given the tools it holds. */
export function stagesForTools(toolNames = []) {
  const held = new Set(toolNames);
  return STAGES.map((stage, index) => ({
    id: stage.id,
    label: stage.label,
    hint: stage.hint,
    index,
    available: stage.tools.some((tool) => held.has(tool)),
  }));
}

export function emptyRun() {
  return {
    stage: -1,
    thinking: false,
    answering: false,
    // Everything the turn produced, in the order it happened. The view walks
    // this list, so a tool card lands between the sentence that led to it and
    // the sentence that follows.
    blocks: [],
    tools: [],
    proposals: [],
    approvals: [],
    usage: null,
    error: "",
    finishReason: "",
    threadId: "",
    stages: STAGES.map(() => ({ started: false, done: false, count: 0, failed: 0 })),
  };
}

function touch(run, index, { done = false } = {}) {
  if (index < 0 || index >= run.stages.length) return;
  const stage = run.stages[index];
  stage.started = true;
  if (done) stage.done = true;
  if (index > run.stage) run.stage = index;
}

function push(run, block) {
  // `v` is a revision counter, not decoration: the view repaints a block only
  // when it changed, which keeps a long streamed answer from being re-parsed
  // from scratch on every frame.
  const entry = { v: 1, ...block };
  run.blocks.push(entry);
  return entry;
}

/** The open text-ish block of this kind, or a new one. */
function stream(run, kind) {
  const last = run.blocks.at(-1);
  if (last && last.kind === kind) return last;
  return push(run, { kind, text: "" });
}

function addUsage(current, event) {
  const sum = (left, right) =>
    typeof right === "number" ? (typeof left === "number" ? left + right : right) : left ?? null;
  return {
    in: sum(current?.in, event.in),
    out: sum(current?.out, event.out),
  };
}

/**
 * Fold one SSE payload into the run state. Returns the same object so callers
 * can keep a single mutable run per assistant turn.
 *
 * `now` is passed in rather than read here: a reducer that calls the clock
 * cannot be tested, and the panel already has a monotonic one.
 */
export function applyEvent(run, event, { now = 0 } = {}) {
  switch (event?.type) {
    case "thread":
      run.threadId = String(event.id || "");
      break;
    case "reasoning": {
      run.thinking = true;
      const block = stream(run, "reasoning");
      block.text += event.text || "";
      block.v += 1;
      break;
    }
    case "content": {
      run.answering = true;
      const block = stream(run, "text");
      block.text += event.text || "";
      block.v += 1;
      break;
    }
    case "tool_call": {
      const index = stageOf(event.name);
      const block = push(run, {
        kind: "tool",
        id: event.id,
        name: event.name,
        args: event.arguments || "",
        stage: index,
        state: "running",
        summary: "",
        preview: "",
        detail: "",
        rows: null,
        startedAt: now,
        ms: null,
      });
      run.tools.push(block);
      touch(run, index);
      break;
    }
    case "tool_result": {
      const entry = run.tools.find((item) => item.id === event.id);
      const index = entry ? entry.stage : stageOf(event.name);
      if (entry) {
        entry.state = event.ok ? "ok" : "bad";
        entry.summary = event.summary || (event.ok ? "ok" : "failed");
        entry.preview = event.preview || "";
        entry.detail = event.detail || "";
        entry.rows = typeof event.rows === "number" ? event.rows : null;
        entry.ms = now && entry.startedAt ? Math.max(0, Math.round(now - entry.startedAt)) : null;
        entry.v += 1;
      }
      if (index >= 0) {
        run.stages[index].count += 1;
        if (!event.ok) run.stages[index].failed += 1;
      }
      touch(run, index, { done: true });
      break;
    }
    case "approval": {
      // The agent is blocked on this one. It becomes a block in the stream so
      // it lands where it happened, between the sentence that asked for it
      // and whatever follows the decision.
      const block = push(run, {
        kind: "approval",
        requestId: String(event.request_id || ""),
        id: event.id,
        name: event.name,
        args: event.arguments || "",
        capabilities: Array.isArray(event.capabilities) ? event.capabilities : [],
        state: "waiting",
        decidedBy: "",
        reason: "",
      });
      run.approvals.push(block);
      break;
    }
    case "approval_decided": {
      const entry = run.approvals.find((item) => item.id === event.id);
      if (entry) {
        entry.state = event.approved ? "approved" : "denied";
        entry.decidedBy = String(event.decided_by || "");
        entry.reason = String(event.reason || "");
        entry.v += 1;
      }
      break;
    }
    case "proposal": {
      const block = push(run, { kind: "proposal", proposal: event });
      run.proposals.push(block);
      touch(run, STAGES.length - 1, { done: true });
      break;
    }
    case "usage":
      run.usage = addUsage(run.usage, event);
      break;
    case "finish":
      run.finishReason = String(event.reason || "");
      break;
    case "error":
      run.error = String(event.text || "");
      break;
    default:
      break;
  }
  return run;
}

/**
 * Close out whatever the stream left open, once the run is over.
 *
 * A run ends whenever the user stops it, the connection drops, or the server
 * tears the turn down. A tool still "running" and an approval still "waiting"
 * would keep a live control on screen for work that can never finish: the
 * server resolves every pending approval to a denial as it unwinds, so the
 * buttons on that card answer nothing and the click comes back a conflict.
 */
export function settleRun(run, { stopped = false } = {}) {
  for (const tool of run.tools) {
    if (tool.state !== "running") continue;
    tool.state = "bad";
    tool.summary = stopped ? "stopped" : "no result";
    tool.v += 1;
  }
  for (const approval of run.approvals) {
    if (approval.state !== "waiting") continue;
    approval.state = "denied";
    approval.decidedBy = stopped ? "run stopped" : "run ended";
    approval.v += 1;
  }
  return run;
}

/**
 * What pressing send means with this composer and this run.
 *
 * Enter used to abort mid-stream: it destroyed the answer being written and
 * did not send the typed message either. A follow-up queues instead, and only
 * an empty composer still reads as stop.
 */
export function composerIntent({ busy = false, text = "" } = {}) {
  const typed = String(text ?? "").trim();
  if (!busy) return typed ? "send" : "ignore";
  return typed ? "queue" : "stop";
}

/* An IFC GlobalId is 22 chars of a base64 variant, and its first character
 * encodes the UUID's top bits, so it is always 0-3. The neighbouring classes
 * keep a longer hash from matching a 22-char slice of itself. The prose
 * renderer holds the same shape for markdown; this one reads tool output. */
const GLOBAL_ID = "(^|[^0-9A-Za-z_$])([0-3][0-9A-Za-z_$]{21})(?![0-9A-Za-z_$])";

/** A fresh matcher per call: one shared /g regex carries lastIndex onward. */
export function globalIdPattern() {
  return new RegExp(GLOBAL_ID, "g");
}

/** Every distinct GlobalId a result names, in the order it names them. */
export function globalIdsIn(text) {
  const found = new Set();
  for (const match of String(text ?? "").matchAll(globalIdPattern())) found.add(match[2]);
  return [...found];
}

/** Group the tools that ran under their stage, for the workspace view. */
export function timeline(run) {
  return STAGES.map((stage, index) => ({
    id: stage.id,
    label: stage.label,
    hint: stage.hint,
    state: run.stages[index],
    tools: run.tools.filter((tool) => tool.stage === index),
  })).filter((row) => row.tools.length);
}

/** Pretty-print a JSON payload; anything else comes back unchanged. */
export function pretty(text) {
  const source = String(text ?? "").trim();
  if (!source) return "";
  try {
    return JSON.stringify(JSON.parse(source), null, 1);
  } catch {
    return source;
  }
}

/** How long the console took, in the shortest form that is still honest. */
export function duration(ms) {
  if (typeof ms !== "number" || !Number.isFinite(ms) || ms < 0) return "";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

/**
 * The one-line "what did this call actually do" a reader wants first.
 *
 * A parser error arrives as a paragraph. The collapsed line gets the code and
 * the start of the message, on one line; the card carries the whole thing and
 * opens itself when the call failed, so nothing is lost by shortening here.
 */
export function toolHeadline(tool, { limit = 80 } = {}) {
  if (!tool) return "";
  if (tool.state === "running") return "running";
  if (tool.state !== "bad") return tool.summary || "ok";
  const code = String(tool.summary || "").replace(/\s+/g, " ").trim();
  const message = String(tool.detail || "").replace(/\s+/g, " ").trim();
  const line = [code, message].filter(Boolean).join(": ") || "failed";
  return line.length > limit ? `${line.slice(0, limit - 1)}...` : line;
}

/** The turn as history should keep it: ordered, bounded, JSON-safe. */
export function transcriptBlocks(run, { limit = 60, chars = 4000 } = {}) {
  const clip = (value) => String(value ?? "").slice(0, chars);
  return run.blocks
    .filter((block) => block.kind !== "text" || block.text.trim())
    .slice(-limit)
    .map((block) => {
      if (block.kind === "tool") {
        return {
          kind: "tool",
          name: block.name,
          stage: block.stage,
          state: block.state,
          summary: block.summary,
          ms: block.ms ?? null,
          args: clip(block.args),
          preview: clip(block.preview),
          detail: clip(block.detail),
        };
      }
      if (block.kind === "proposal") return { kind: "proposal", proposal: block.proposal };
      return { kind: block.kind, text: clip(block.text) };
    });
}

/* SSE framing is transport, not run state: it lives with the rest of the wire
   boundary in chat_ai_sdk.js, and the panel imports `decodeIfcSSE` from there.
   A second decoder here drifted from it and dropped every event whenever a
   frame arrived with CRLF line endings or without the space after `data:`. */
