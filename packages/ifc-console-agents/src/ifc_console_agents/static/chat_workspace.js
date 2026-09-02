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

export const SKILL_DRY_RUN_SELECTION_LIMIT = 25;

const plain = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);

/** Canonical target order for review requests and their cache keys. */
export function canonicalSelectionGuids(selection) {
  const guids = Array.isArray(selection?.guids) ? selection.guids : [];
  return [...new Set(guids.filter((guid) => typeof guid === "string" && guid))].sort();
}

/**
 * Stable identity for a model-scoped viewer selection.
 *
 * Model rows expose an etag even when the underlying fingerprint and revision
 * are not sent separately, so prefer every available revision signal.
 */
export function geometrySelectionToken(selection, status = {}) {
  const modelId = typeof selection?.model_id === "string" ? selection.model_id : "";
  if (!modelId) return "";
  const models = Array.isArray(status?.models) ? status.models : [];
  const model = models.find((row) => row?.id === modelId) || {};
  const revision = [
    selection?.fingerprint,
    selection?.revision,
    model.fingerprint,
    model.revision,
    model.etag,
    model.active ? status?.fingerprint : null,
  ].filter((value) => value !== undefined && value !== null && value !== "");
  return JSON.stringify({
    model_id: modelId,
    revision: revision.length ? revision.map(String) : null,
    global_ids: canonicalSelectionGuids(selection),
  });
}

/** Add a request generation so two races for one selection cannot share state. */
export function geometryRequestToken(selectionToken, requestGeneration) {
  return `${selectionToken}#request:${requestGeneration}`;
}

/** Server paging or response-envelope truncation makes a review incomplete. */
export function measurementDryRunIsPartial(payload) {
  const data = plain(payload?.data) ? payload.data : {};
  const targets = plain(data.targets) ? data.targets : {};
  const envelopeMeta = plain(payload?.meta) ? payload.meta : {};
  const dataMeta = plain(data.meta) ? data.meta : {};
  return Boolean(
    targets.has_more
      || targets.truncated_by_max_matches
      || envelopeMeta.truncated
      || dataMeta.truncated
  );
}

/** A proposal can follow only a complete, current preview with extracted values. */
export function measurementDryRunCanPropose(
  state,
  { selectionToken = "", requestGeneration = 0 } = {},
) {
  if (!state || state.loading || state.error || state.complete !== true) return false;
  if (state.selectionToken !== selectionToken) return false;
  if (state.requestGeneration !== requestGeneration) return false;
  if (state.requestToken !== geometryRequestToken(selectionToken, requestGeneration)) return false;
  if (measurementDryRunIsPartial(state.payload)) return false;
  const data = plain(state.payload?.data) ? state.payload.data : {};
  const results = Array.isArray(data.results) ? data.results : [];
  return results.some((result) => (
    ["extracted", "partial"].includes(String(result?.status || "").toLowerCase())
      && Array.isArray(result?.extracted)
      && result.extracted.length > 0
  ));
}

const EXACT_IFC_SOURCES = new Set([
  "extrusion_depth",
  "ifc_relationship",
  "material_layer_parameter",
  "profile_curve",
  "profile_parameter",
]);

/** Distinguish exact IFC parameters from sampled or derived observations. */
export function measurementEvidenceKind(measurement) {
  const flags = Array.isArray(measurement?.flags)
    ? measurement.flags.map((flag) => String(flag).toLowerCase())
    : [];
  const disqualified = flags.some((flag) => (
    flag.includes("approximate") || flag.includes("pre_boolean")
  ));
  const exact = measurement?.uncertainty_si === 0
    && EXACT_IFC_SOURCES.has(String(measurement?.source || ""))
    && !disqualified;
  return exact
    ? {
      id: "exact",
      label: "Exact IFC",
      description: "Read from an IFC parameter or relationship with zero stated uncertainty.",
    }
    : {
      id: "measured",
      label: "Measured",
      description: "Sampled, tessellated, approximated, or otherwise derived geometry evidence.",
    };
}

/** Normalize preferred and alternative evidence into comparable delta rows. */
export function measurementAlternativeRows(measurement, absoluteToleranceSi = null) {
  if (!plain(measurement)) return [];
  const preferred = Number(measurement.value_si);
  const tolerance = absoluteToleranceSi === null || absoluteToleranceSi === undefined
    ? Number.NaN
    : Number(absoluteToleranceSi);
  const toleranceSi = Number.isFinite(tolerance) && tolerance >= 0 ? tolerance : null;
  const alternatives = Array.isArray(measurement.alternatives) ? measurement.alternatives : [];
  const rows = [{
    role: "preferred",
    record: measurement,
    delta_si: 0,
    relative_delta: 0,
    tolerance_si: toleranceSi,
    assessment: "preferred source",
    evidence_kind: measurementEvidenceKind(measurement),
  }];
  for (const alternative of alternatives) {
    if (!plain(alternative)) continue;
    const value = Number(alternative.value_si);
    const evidenceDelta = alternative?.evidence?.delta_si === null
      || alternative?.evidence?.delta_si === undefined
      ? Number.NaN
      : Number(alternative.evidence.delta_si);
    const explicitDelta = alternative.absolute_delta_si === null
      || alternative.absolute_delta_si === undefined
      ? Number.NaN
      : Number(alternative.absolute_delta_si);
    const delta = Number.isFinite(evidenceDelta)
      ? evidenceDelta
      : Number.isFinite(value) && Number.isFinite(preferred)
          ? value - preferred
          : Number.isFinite(explicitDelta)
            ? explicitDelta
            : null;
    const explicitRelative = alternative.relative_delta === null
      || alternative.relative_delta === undefined
      ? Number.NaN
      : Number(alternative.relative_delta);
    const relative = Number.isFinite(explicitRelative)
      ? explicitRelative
      : delta !== null && Number.isFinite(preferred) && preferred !== 0
        ? delta / preferred
        : null;
    const withinTolerance = delta !== null && toleranceSi !== null
      ? Math.abs(delta) <= toleranceSi
      : null;
    const explicitStatus = String(alternative.status || "").replaceAll("_", " ");
    const assessment = explicitStatus
      || (withinTolerance === null
        ? "not tolerance-assessed"
        : withinTolerance ? "within tolerance" : "outside tolerance");
    rows.push({
      role: "alternative",
      record: alternative,
      delta_si: delta,
      relative_delta: relative,
      tolerance_si: toleranceSi,
      within_tolerance: withinTolerance,
      assessment,
      evidence_kind: measurementEvidenceKind(alternative),
    });
  }
  return rows;
}

/** Representative and adaptive section positions, deduplicated and ordered. */
export function representativeSectionStations(sectionAnalysis) {
  if (!plain(sectionAnalysis)) return [];
  const byStation = new Map();
  const add = (at, role, section = {}) => {
    const station = Number(at);
    if (!Number.isFinite(station) || station < 0 || station > 1) return;
    const key = station.toFixed(9);
    const current = byStation.get(key) || {
      at: station,
      roles: [],
      descriptor: {},
      closed: null,
    };
    if (role && !current.roles.includes(role)) current.roles.push(role);
    const descriptor = plain(section?.descriptor) ? section.descriptor : {};
    current.descriptor = {
      ...current.descriptor,
      ...descriptor,
    };
    for (const [target, source] of [
      ["width_si", "width"],
      ["height_si", "height"],
      ["area_si", "area"],
      ["perimeter_si", "perimeter"],
      ["loop_count", "loop_count"],
      ["hole_count", "hole_count"],
    ]) {
      if (current.descriptor[target] === undefined && section?.[source] !== undefined) {
        current.descriptor[target] = section[source];
      }
    }
    if (Array.isArray(section?.thickness?.modes)) {
      current.descriptor.thickness_modes_si = section.thickness.modes
        .map((mode) => Number(mode?.value_si))
        .filter(Number.isFinite);
    }
    if (section?.closed !== undefined) current.closed = Boolean(section.closed);
    byStation.set(key, current);
  };

  for (const station of Array.isArray(sectionAnalysis.stations) ? sectionAnalysis.stations : []) {
    add(station?.at, "evaluated station", station);
  }
  const representatives = plain(sectionAnalysis.representative_sections)
    ? sectionAnalysis.representative_sections
    : {};
  for (const [name, label] of [
    ["dominant", "dominant"],
    ["minimum", "minimum area"],
    ["maximum", "maximum area"],
  ]) {
    const section = representatives[name];
    if (plain(section)) add(section.at, label, section);
  }
  for (const transition of Array.isArray(representatives.transitions)
    ? representatives.transitions
    : []) {
    add(transition?.at, "adaptive transition", transition);
  }
  for (const at of Array.isArray(representatives.transition_stations)
    ? representatives.transition_stations
    : []) {
    add(at, "adaptive transition");
  }
  const regions = Array.isArray(sectionAnalysis.profile_regions)
    ? sectionAnalysis.profile_regions
    : [];
  regions.forEach((region, index) => {
    add(region?.representative_station, `region ${index + 1}`, {
      descriptor: region?.descriptor,
    });
  });
  return [...byStation.values()].sort((left, right) => left.at - right.at);
}

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
