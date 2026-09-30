/* The context-flow model: what the agent is doing, in the user's language.
 *
 * The panel used to show a flat list of tool chips, which says what ran but
 * not why, and put them all above the answer. This module maps every tool to a
 * stage of the agent's pipeline and reduces the SSE event stream into an
 * ordered list of blocks, so the view can render a tool exactly where it ran.
 * It also builds the agent stream request. Pure functions only: unit tested
 * without a DOM.
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
      "audit_element_properties",
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
      "check_model_health",
      "assess_model_quality",
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
        output: null,
        detail: "",
        rows: null,
        progress: null,
        startedAt: now,
        ms: null,
      });
      run.tools.push(block);
      touch(run, index);
      break;
    }
    case "tool_progress": {
      const entry = run.tools.find((item) => item.id === event.id);
      if (entry) {
        entry.progress = {
          done: Number.isFinite(Number(event.done)) ? Number(event.done) : 0,
          total: Number.isFinite(Number(event.total)) ? Number(event.total) : null,
          note: String(event.note || ""),
          elapsed: Number.isFinite(Number(event.elapsed)) ? Number(event.elapsed) : 0,
        };
        entry.v += 1;
      }
      break;
    }
    case "tool_result": {
      const entry = run.tools.find((item) => item.id === event.id);
      const index = entry ? entry.stage : stageOf(event.name);
      if (entry) {
        entry.state = event.ok ? "ok" : "bad";
        entry.summary = event.summary || (event.ok ? "ok" : "failed");
        entry.preview = event.preview || "";
        entry.output = event.output ?? null;
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
        standingAllowed: event.standing_allowed === true,
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
 * The approval a tool call ran under, when the stream shows both.
 *
 * The console asks before the call and emits the call once it is allowed, so
 * the decision sits right before the card. Restored turns carry no ids, so
 * the pair is read by position and name; ids only rule a match out.
 */
export function approvalBefore(blocks, index) {
  const tool = blocks[index];
  const prior = blocks[index - 1];
  if (tool?.kind !== "tool" || prior?.kind !== "approval") return null;
  if (prior.state !== "approved" || prior.name !== tool.name) return null;
  if (prior.id && tool.id && prior.id !== tool.id) return null;
  return prior;
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

/** Auto runs protected tools unattended, so it is only reached by confirming. */
export function autonomyRequest(current) {
  return current === "auto" ? "approval" : "confirm";
}

/**
 * The rows of the composer's + menu, as data. The panel draws them and owns
 * what each one does; which rows exist, in what order, and in what state is
 * decided here so it can be tested without a DOM.
 */
export function plusMenuModel({
  files = false,
  viewer = false,
  vision = true,
  workflows = 0,
  selected = 0,
  measured = 0,
  skills = [],
  pinned = [],
  autonomy = "approval",
  confirmingAuto = false,
  memory = null,
  models = [],
  activeModel = "",
} = {}) {
  const message = [];
  if (files) message.push({ id: "attach", label: "Attach a file", note: "This message only" });
  if (files && viewer && vision) {
    message.push({ id: "capture", label: "Attach the current 3D view", note: "Sends what you can see" });
  }
  if (files || viewer) {
    message.push({
      id: "mention",
      label: "Mention project content",
      note: "Also saved views and the 3D selection",
    });
  }
  if (workflows > 0) {
    message.push({
      id: "workflow",
      label: "Run a workflow",
      note: `${workflows} saved procedure${workflows === 1 ? "" : "s"}`,
    });
  }
  if (viewer) {
    message.push(
      {
        id: "frame",
        label: selected ? "Frame the 3D selection" : "Select elements in the 3D view",
        note: selected ? `${selected} in context` : "Nothing selected yet",
      },
      {
        id: "analyze",
        label: "Analyze the selected element",
        note: selected ? "Prefills a geometry analysis request" : "Nothing selected yet",
      },
      {
        id: "save-skill",
        label: "Save measurements as a skill",
        note: measured
          ? `${measured} on screen become one repeatable pattern`
          : "Measure in the 3D view first",
      },
    );
  }

  const followed = new Set(pinned);
  const skillRows = skills.length
    ? skills.map((skill) => ({
      id: `skill:${skill.name}`,
      kind: "check",
      label: `#${skill.name}`,
      note: skill.hint || "",
      checked: followed.has(skill.name),
    }))
    : [{ id: "skills-empty", kind: "note", label: "No skills yet", note: "Add them under Agent settings, Skills" }];

  const auto = autonomy === "auto";
  const session = [
    confirmingAuto && !auto
      ? {
        id: "autonomy",
        kind: "confirm",
        label: "Run protected tools without asking?",
        note: "Code runs and model edits will no longer wait for you.",
      }
      : {
        id: "autonomy",
        kind: "check",
        label: "Run tools without asking",
        note: auto ? "Auto: protected calls run unattended" : "Approval: asks before every protected call",
        checked: auto,
        level: auto ? "warn" : "",
      },
  ];
  if (memory) {
    session.push({
      id: "memory",
      label: "Free memory",
      note: memory.label,
      title: memory.title,
      level: memory.level,
    });
  }

  const sections = [
    { id: "message", label: "Add to this message", items: message },
    { id: "skills", label: "Skills", items: skillRows },
    { id: "session", label: "Session", items: session },
    {
      id: "models",
      label: "IFC model",
      items: models.length > 1
        ? models.map((model) => ({
          id: `model:${model.id}`,
          kind: "radio",
          label: model.name || model.id,
          checked: model.id === activeModel,
        }))
        : [],
    },
    {
      id: "panel",
      label: "Panel",
      items: [
        { id: "settings", label: "Settings", note: "Model, key, appearance, history" },
        { id: "shortcuts", label: "Keyboard shortcuts", note: "Enter, @, #, /" },
      ],
    },
  ];
  return { sections: sections.filter((section) => section.items.length) };
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

/** Pretty-print a JSON payload; anything else comes back unchanged. */
export function pretty(text) {
  if (text !== null && typeof text === "object") {
    try {
      return JSON.stringify(text, null, 1);
    } catch {
      return String(text);
    }
  }
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
  if (tool.state === "running") {
    const note = String(tool.progress?.note || "").replace(/\s+/g, " ").trim();
    if (note) return note.length > limit ? `${note.slice(0, limit - 1)}...` : note;
    if (Number.isFinite(tool.progress?.total) && tool.progress.total > 0) {
      return `${Math.min(tool.progress.done, tool.progress.total)} / ${tool.progress.total}`;
    }
    return "running";
  }
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

const REQUEST_OPTIONS = [
  "provider",
  "model",
  "base_url",
  "api_key",
  "tools_supported",
  "vision_supported",
  "temperature",
  "top_p",
  "max_tokens",
];

const SKILL_NAME = /^[a-z0-9][a-z0-9-]{1,63}$/;

const asText = (value) => (typeof value === "string" ? value : value == null ? "" : String(value));

function asJson(value) {
  if (value === undefined) return null;
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return asText(value);
  }
}

const asCount = (value) => (Number.isInteger(value) && value >= 0 ? value : 0);

/** The JSON body of one /api/agents/stream request. */
export function agentChatRequest(options = {}) {
  const workflow = asText(options.workflow);
  const body = { agent: asText(options.agent), prompt: asText(options.prompt) };
  for (const key of REQUEST_OPTIONS) {
    if (options[key] !== undefined) body[key] = options[key];
  }
  // An empty prompt with a workflow is a real request: the console answers it
  // with the workflow's own task, so none is invented here.
  if (workflow) {
    body.workflow = workflow;
    body.workflow_scope = options.workflow_scope === "selection" ? "selection" : "model";
  }
  if (typeof options.thread_id === "string" && options.thread_id) {
    body.thread_id = options.thread_id;
  }
  if (typeof options.persist_history === "boolean") {
    body.persist_history = options.persist_history;
  }
  if (typeof options.additional_instructions === "string") {
    body.additional_instructions = options.additional_instructions;
  }
  if (Array.isArray(options.attachments)) {
    // Paths returned by the local upload API, never URLs.
    body.attachments = options.attachments.filter(
      (item) => typeof item === "string" && item.trim(),
    );
  }
  if (Array.isArray(options.skills)) {
    const skills = [...new Set(options.skills.filter(
      (item) => typeof item === "string" && SKILL_NAME.test(item),
    ))];
    if (skills.length) body.skills = skills.slice(0, 8);
  }
  return body;
}

/** A proposal event or stored block, in one stable shape. */
export function normalizeIfcProposal(value = {}) {
  const proposal = value !== null && typeof value === "object" && !Array.isArray(value)
    ? value
    : {};
  return {
    changeSetId: asText(proposal.changeSetId || proposal.change_set_id || proposal.id),
    changeCount: asCount(proposal.changeCount ?? proposal.count),
    elementCount: asCount(proposal.elementCount ?? proposal.elements),
    psetName: asText(proposal.psetName || proposal.pset_name || proposal.pset),
    propertyName: asText(
      proposal.propertyName || proposal.property_name || proposal.property,
    ),
    value: asJson(proposal.value),
    unit: asText(proposal.unit),
    method: asText(proposal.method),
    source: asText(proposal.source),
    confidence: asText(proposal.confidence),
    marked: Boolean(proposal.marked),
    provenanceChangeSet: asText(
      proposal.provenanceChangeSet || proposal.provenance_change_set,
    ),
    warning: asText(proposal.warning),
    aiGenerated: proposal.aiGenerated !== false && proposal.ai_generated !== false,
  };
}
