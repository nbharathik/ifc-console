/* The agent workspace: what this assistant is, and what it can reach.
 *
 * The chat used to be the only surface, so "how does this thing work" had
 * nowhere to live and leaked into the transcript. The workspace is a separate
 * panel with four tabs, fed by one /api/agents/workspace payload. The model
 * below is pure: it takes that payload and returns exactly what the view
 * draws, so it can be unit tested without a DOM.
 */

export const TABS = [
  { id: "overview", label: "Overview" },
  { id: "tools", label: "Tools" },
  { id: "files", label: "Files" },
  { id: "settings", label: "Settings" },
];

const plain = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** Group the agent's tools by pipeline stage, in stage order. */
export function toolsByStage(payload) {
  const stages = Array.isArray(payload?.stages) ? payload.stages : [];
  const tools = Array.isArray(payload?.tools) ? payload.tools : [];
  const groups = stages.map((stage) => ({
    id: stage.id,
    label: stage.label,
    hint: stage.hint,
    available: Boolean(stage.available),
    tools: tools.filter((tool) => tool.stage === stage.id),
  }));
  const loose = tools.filter((tool) => !tool.stage);
  if (loose.length) {
    groups.push({ id: "other", label: "Other", hint: "", available: true, tools: loose });
  }
  return groups.filter((group) => group.tools.length);
}

/** Split the reference ledger into images and documents, newest counts first. */
export function fileGroups(files) {
  const rows = Array.isArray(files) ? files : [];
  const images = rows.filter((file) => file.media === "image");
  const documents = rows.filter((file) => file.media !== "image");
  return {
    images,
    documents,
    total: rows.length,
    indexed: rows.filter((file) => file.indexed).length,
  };
}

export function formatBytes(size) {
  const bytes = Number(size) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * Everything the workspace view needs, derived once.
 *
 * Returns a stable shape even for a missing or malformed payload, so a failed
 * fetch renders an empty workspace rather than throwing inside the panel.
 */
export function workspaceModel(payload, { files = null } = {}) {
  const safe = plain(payload) ? payload : {};
  const agent = plain(safe.agent) ? safe.agent : {};
  const blocks = Array.isArray(safe.blocks) ? safe.blocks : [];
  const tools = Array.isArray(safe.tools) ? safe.tools : [];
  const writes = Array.isArray(safe.writes) ? safe.writes : [];
  const rows = files ?? (Array.isArray(safe.files) ? safe.files : []);
  const groups = fileGroups(rows);
  const stages = Array.isArray(safe.stages) ? safe.stages : [];

  return {
    name: String(agent.name ?? ""),
    title: String(agent.title || "Assistant"),
    description: String(agent.description || ""),
    summary: String(safe.summary || ""),
    kind: String(safe.kind || agent.kind || "built-in"),
    builtin: Boolean(safe.builtin),
    role: String(safe.role || ""),
    examples: Array.isArray(safe.examples) ? safe.examples : [],
    blocks,
    availableBlocks: blocks.filter((block) => block.available),
    stages,
    reachableStages: stages.filter((stage) => stage.available),
    tools,
    stageGroups: toolsByStage(safe),
    writes,
    artifactWrites: Array.isArray(safe.artifactWrites) ? safe.artifactWrites : [],
    canWriteModel: writes.length > 0,
    writePolicy: plain(safe.write_policy) ? safe.write_policy : null,
    unavailable: Array.isArray(safe.unavailable_tools) ? safe.unavailable_tools : [],
    viewer: Boolean(safe.viewer),
    mode: String(safe.mode || "ask"),
    limits: plain(safe.limits) ? safe.limits : {},
    files: rows,
    fileGroups: groups,
    // No count on the overview: "10" next to "How it works" reads like a
    // quantity of something the reader can act on, and it is not.
    counts: {
      overview: 0,
      tools: tools.length,
      files: groups.total,
      settings: 0,
    },
  };
}

/** One line summarising an agent's reach, for the panel header. */
export function reachSentence(model) {
  if (!model.name) return "";
  const bits = [`${model.counts.tools} tools`];
  if (model.counts.files) bits.push(`${model.counts.files} files`);
  bits.push(
    model.canWriteModel
      ? "preview changes only"
      : "read-only"
  );
  return bits.join(" · ");
}
