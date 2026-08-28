/* The agent workspace: what this assistant is, and what it can reach.
 *
 * The chat used to be the only surface, so "how does this thing work" had
 * nowhere to live and leaked into the transcript. The workspace is one panel
 * behind one control, fed by one /api/agents/workspace payload. The model
 * below is pure: it takes that payload and returns exactly what the view
 * draws, so it can be unit tested without a DOM.
 */

// Pipeline is not a tab: an agent's stages depend on the blocks it holds, so
// the workflow is a property of the selected agent and lives in its overview.
const ALL_TABS = [
  { id: "agent", label: "Agents" },
  { id: "capabilities", label: "Capabilities" },
  { id: "tools", label: "Tools" },
  { id: "content", label: "Content" },
  { id: "skills", label: "Skills" },
  { id: "models", label: "Models" },
  { id: "app", label: "App" },
];

// The workspace keeps one stable navigation. Content remains available when
// empty because it is also where people upload and configure references.
export const TABS = ALL_TABS;

const plain = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** Group the agent's tools by pipeline stage, in stage order. */
export function toolsByStage(payload) {
  const stages = Array.isArray(payload?.stages) ? payload.stages : [];
  const tools = Array.isArray(payload?.tools) ? payload.tools : [];
  const knownStages = new Set(stages.map((stage) => stage.id));
  const groups = stages.map((stage) => ({
    id: stage.id,
    label: stage.label,
    hint: stage.hint,
    available: Boolean(stage.available),
    tools: tools.filter((tool) => tool.stage === stage.id),
  }));
  const loose = tools.filter((tool) => !tool.stage || !knownStages.has(tool.stage));
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

/** Every workspace section, in its vertical navigation order. */
export function tabsFor() {
  return [...ALL_TABS];
}

/** Questions shown in the agent overview, with starters as the fallback. */
export function suggestedQuestions(examples, starters) {
  const rows = Array.isArray(examples) ? examples.filter(plain) : [];
  if (rows.length) return rows;
  const prompts = Array.isArray(starters) ? starters : [];
  return prompts
    .map((prompt) => String(prompt ?? "").trim())
    .filter(Boolean)
    .map((prompt, index) => ({
      title: `Suggested question ${index + 1}`,
      prompt,
      note: "",
    }));
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
  const content = plain(safe.content) ? safe.content : {};
  const contentAccess = plain(content.access)
    ? content.access
    : { mode: "all", paths: [] };
  const groups = fileGroups(rows);
  const stages = Array.isArray(safe.stages) ? safe.stages : [];
  const features = Array.isArray(agent.features) ? agent.features : [];
  const examples = Array.isArray(safe.examples) ? safe.examples : [];
  const starters = Array.isArray(agent.starters) ? agent.starters : [];
  const skills = Array.isArray(safe.skills) ? safe.skills : [];

  return {
    name: String(agent.name ?? ""),
    title: String(agent.title || "Assistant"),
    description: String(agent.description || ""),
    summary: String(safe.summary || ""),
    kind: String(safe.kind || agent.kind || "built-in"),
    builtin: Boolean(safe.builtin),
    plain: Boolean(safe.plain),
    role: String(safe.role || ""),
    examples,
    starters,
    suggestedQuestions: suggestedQuestions(examples, starters),
    blocks,
    availableBlocks: blocks.filter((block) => block.available),
    stages,
    reachableStages: stages.filter((stage) => stage.available),
    tools,
    stageGroups: toolsByStage(safe),
    writes,
    artifactWrites: Array.isArray(safe.artifact_writes) ? safe.artifact_writes : [],
    canWriteModel: writes.length > 0,
    writePolicy: plain(safe.write_policy) ? safe.write_policy : null,
    unavailable: Array.isArray(safe.unavailable_tools) ? safe.unavailable_tools : [],
    viewer: Boolean(safe.viewer),
    mode: String(safe.mode || "ask"),
    limits: plain(safe.limits) ? safe.limits : {},
    workflow: plain(safe.workflow) ? safe.workflow : {},
    usesFiles: features.includes("files"),
    files: rows,
    fileGroups: groups,
    content: {
      enabled: Boolean(content.enabled),
      usable: Boolean(content.usable),
      access: {
        mode: contentAccess.mode === "selected" ? "selected" : "all",
        paths: Array.isArray(contentAccess.paths) ? contentAccess.paths.map(String) : [],
      },
      files: Array.isArray(content.files) ? content.files : [],
    },
    skills,
    counts: {
      agent: stages.length,
      capabilities: blocks.length,
      tools: tools.length,
      content: groups.total,
      skills: skills.length,
      models: 0,
      app: 0,
    },
  };
}

/** One line summarising an agent's reach, for the panel header. */
export function reachSentence(model) {
  // Nothing to say about the reach of an assistant that holds no tools, and a
  // failed fetch must not produce a confident-sounding line about one.
  if (!model.tools.length) return "";
  const bits = [`${model.counts.tools} tools`];
  if (model.usesFiles) bits.push(`${model.counts.content} files`);
  bits.push(model.canWriteModel ? "preview changes only" : "read-only");
  return bits.join(" · ");
}
