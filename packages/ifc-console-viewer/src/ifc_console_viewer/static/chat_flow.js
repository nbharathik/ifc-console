/* The context-flow model: what the agent is doing, in the user's language.
 *
 * The panel used to show a flat list of tool chips, which says what ran but
 * not why. This module maps every tool to a stage of the agent's pipeline and
 * reduces the SSE event stream into a small state object the view renders, so
 * a reader can follow scope -> evidence -> method -> verify -> propose without
 * knowing any tool names. Pure functions only: unit tested without a DOM.
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
      "measure_elements",
      "measure_distance",
      "compute_quantities",
      "get_element_geometry",
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
      "orient",
      "list_ai_authored_properties",
      "get_change_set",
      "export_csv",
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
    tools: [],
    proposals: [],
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

/**
 * Fold one SSE payload into the run state. Returns the same object so callers
 * can keep a single mutable run per assistant turn.
 */
export function applyEvent(run, event) {
  switch (event?.type) {
    case "thread":
      run.threadId = String(event.id || "");
      break;
    case "reasoning":
      run.thinking = true;
      break;
    case "content":
      run.answering = true;
      break;
    case "tool_call": {
      const index = stageOf(event.name);
      run.tools.push({
        id: event.id,
        name: event.name,
        arguments: event.arguments || "",
        stage: index,
        state: "running",
        summary: "",
      });
      touch(run, index);
      break;
    }
    case "tool_result": {
      const entry = run.tools.find((item) => item.id === event.id);
      const index = entry ? entry.stage : stageOf(event.name);
      if (entry) {
        entry.state = event.ok ? "ok" : "bad";
        entry.summary = event.summary || "";
      }
      if (index >= 0) {
        run.stages[index].count += 1;
        if (!event.ok) run.stages[index].failed += 1;
      }
      touch(run, index, { done: true });
      break;
    }
    case "proposal":
      run.proposals.push(event);
      touch(run, STAGES.length - 1, { done: true });
      break;
    case "usage":
      run.usage = { in: event.in ?? null, out: event.out ?? null };
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

/** Group the tools that ran under their stage, for the timeline view. */
export function timeline(run) {
  return STAGES.map((stage, index) => ({
    id: stage.id,
    label: stage.label,
    hint: stage.hint,
    state: run.stages[index],
    tools: run.tools.filter((tool) => tool.stage === index),
  })).filter((row) => row.tools.length);
}

/** Split an SSE text buffer into parsed payloads plus the unconsumed tail. */
export function decodeSSE(buffer) {
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  const events = [];
  for (const part of parts) {
    if (!part.startsWith("data: ")) continue;
    try {
      events.push(JSON.parse(part.slice(6)));
    } catch {
      /* a truncated frame is dropped, never fatal */
    }
  }
  return { events, rest };
}
