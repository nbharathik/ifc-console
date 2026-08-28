/* Pure Agent setup draft and summary model. */

import { STAGES, stageOf } from "./chat_flow.js";

export const STRATEGIES = Object.freeze([
  "adaptive",
  "evidence-first",
  "fast-scan",
]);

export const STUDIO_STORAGE_NAME = "ifc-console-agent-studio-draft-v1";

const STORAGE_VERSION = 1;
const DEFAULT_STRATEGY = STRATEGIES[0];
const DEFAULT_MAX_TOOL_ROUNDS = 12;
const DEFAULT_MAX_TOOL_CALLS = 48;
const CUSTOM_NAME = /^custom-[a-z0-9][a-z0-9-]*$/;
const SOURCE_KINDS = new Set(["blank", "built-in", "custom"]);
const ARTIFACT_WRITE_TOOLS = new Set(["export_csv"]);

const STRATEGY_STAGE_ORDER = Object.freeze({
  adaptive: STAGES.map((stage) => stage.id),
  "evidence-first": ["evidence", "scope", "method", "verify", "propose"],
  "fast-scan": ["scope", "verify", "method", "evidence", "propose"],
});

const plain = (value) =>
  value !== null && typeof value === "object" && !Array.isArray(value);

const first = (...values) => values.find((value) => value !== undefined && value !== null);

function cleanText(value, limit) {
  return String(value ?? "").trim().slice(0, limit);
}

function cleanList(value, { limit = 100, itemLimit = 180, names = false } = {}) {
  const rows = Array.isArray(value)
    ? value
    : typeof value === "string"
      ? value.split("\n")
      : [];
  const seen = new Set();
  const result = [];
  for (const row of rows) {
    const raw = plain(row) ? row.name : row;
    const item = cleanText(raw, itemLimit);
    if (!item || seen.has(item)) continue;
    if (names && !/^[a-z0-9][a-z0-9-]*$/.test(item)) continue;
    seen.add(item);
    result.push(item);
    if (result.length >= limit) break;
  }
  return result;
}

function boundedInteger(value, fallback, minimum, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number)) return fallback;
  return Math.min(maximum, Math.max(minimum, Math.trunc(number)));
}

function workflowValues(source, info = source) {
  const workflow = plain(source.workflow)
    ? source.workflow
    : plain(info.workflow)
      ? info.workflow
      : {};
  const strategy = first(
    workflow.strategy,
    source.workflow_strategy,
    source.strategy,
    info.workflow_strategy,
    info.strategy,
  );
  const maxToolRounds = first(
    workflow.max_tool_rounds,
    source.max_tool_rounds,
    source.limits?.max_tool_rounds,
    info.max_tool_rounds,
    info.limits?.max_tool_rounds,
  );
  const maxToolCalls = first(
    workflow.max_tool_calls,
    source.max_tool_calls,
    source.limits?.max_tool_calls,
    info.max_tool_calls,
    info.limits?.max_tool_calls,
  );
  return { strategy, maxToolRounds, maxToolCalls };
}

/** Normalize untrusted or locally stored draft data into one stable shape. */
export function normalizeStudioDraft(value = {}) {
  const source = plain(value) ? value : {};
  const workflow = workflowValues(source);
  const rawName = cleanText(source.name, 64);
  const inferredKind = rawName.startsWith("custom-") ? "custom" : "blank";
  const sourceKind = SOURCE_KINDS.has(source.sourceKind)
    ? source.sourceKind
    : SOURCE_KINDS.has(source.source_kind)
      ? source.source_kind
      : inferredKind;
  const strategy = STRATEGIES.includes(workflow.strategy)
    ? workflow.strategy
    : DEFAULT_STRATEGY;
  const rawContentPaths = first(source.contentPaths, source.content_paths);

  return {
    name: sourceKind === "custom" && CUSTOM_NAME.test(rawName) ? rawName : "",
    sourceName: cleanText(first(source.sourceName, source.source_name), 64),
    sourceKind,
    title: cleanText(source.title, 80),
    description: cleanText(source.description, 300),
    instructions: cleanText(source.instructions, 12_000),
    blocks: cleanList(first(source.blocks, source.selectedBlocks), {
      limit: 100,
      itemLimit: 80,
      names: true,
    }),
    starters: cleanList(source.starters, { limit: 6, itemLimit: 180 }),
    contentPaths: Array.isArray(rawContentPaths)
      ? cleanList(rawContentPaths, { limit: 500, itemLimit: 4096 })
      : null,
    workflow: {
      strategy,
      max_tool_rounds: boundedInteger(
        workflow.maxToolRounds,
        DEFAULT_MAX_TOOL_ROUNDS,
        1,
        100,
      ),
      max_tool_calls: boundedInteger(
        workflow.maxToolCalls,
        DEFAULT_MAX_TOOL_CALLS,
        1,
        1000,
      ),
    },
  };
}

/** Create a blank draft or clone agent data returned by either panel route. */
export function createStudioDraft(value = null) {
  if (!plain(value) || !Object.keys(value).length) return normalizeStudioDraft();
  const info = plain(value.agent) ? value.agent : value;
  const name = cleanText(info.name, 64);
  const kind = cleanText(first(value.kind, info.kind), 20).toLowerCase();
  const sourceKind = kind === "custom" || name.startsWith("custom-")
    ? "custom"
    : "built-in";
  const workflow = workflowValues(value, info);
  const instructions = first(
    value.instructions,
    info.instructions,
    sourceKind === "custom" ? value.role : undefined,
    "",
  );
  const access = plain(value.content?.access) ? value.content.access : null;

  return normalizeStudioDraft({
    name: sourceKind === "custom" ? name : "",
    sourceName: name,
    sourceKind,
    title: info.title,
    description: info.description,
    instructions,
    blocks: first(info.blocks, value.blocks),
    starters: first(info.starters, value.starters),
    contentPaths: access?.mode === "selected" ? access.paths : null,
    workflow: {
      strategy: workflow.strategy,
      max_tool_rounds: workflow.maxToolRounds,
      max_tool_calls: workflow.maxToolCalls,
    },
  });
}

function normalizeBlock(value) {
  if (!plain(value)) return null;
  const name = cleanText(value.name, 80);
  if (!name) return null;
  return {
    name,
    title: cleanText(value.title || name, 80),
    description: cleanText(value.description, 300),
    features: cleanList(value.features, { limit: 30, itemLimit: 80 }),
    tools: cleanList(value.tools, { limit: 300, itemLimit: 160 }),
    advanced: Boolean(value.advanced),
    viewerOnly: Boolean(value.viewer_only ?? value.viewerOnly),
  };
}

function blockCatalog(value) {
  const rows = Array.isArray(value) ? value : Array.isArray(value?.blocks) ? value.blocks : [];
  const seen = new Set();
  const result = [];
  for (const value of rows) {
    const block = normalizeBlock(value);
    if (!block || seen.has(block.name)) continue;
    seen.add(block.name);
    result.push(block);
  }
  return result;
}

function orderedStages(strategy, tools, selectedBlocks, proposalReach) {
  const rank = new Map(
    STRATEGY_STAGE_ORDER[strategy].map((identifier, index) => [identifier, index]),
  );
  return STAGES.map((stage, canonicalIndex) => {
    const stageTools = tools.filter((tool) => stageOf(tool) === canonicalIndex);
    const stageBlocks = selectedBlocks
      .filter((block) =>
        block.tools.some((tool) => stageOf(tool) === canonicalIndex)
        || (stage.id === "propose" && block.features.includes("proposals")))
      .map((block) => block.name);
    const dynamic = stage.id === "propose" && proposalReach && !stageTools.length;
    return {
      id: stage.id,
      label: stage.label,
      hint: stage.hint,
      canonicalIndex,
      tools: stageTools,
      blocks: stageBlocks,
      reachable: stageTools.length > 0 || dynamic,
      dynamic,
    };
  }).sort((left, right) => rank.get(left.id) - rank.get(right.id));
}

/** Derive the complete view model for the setup summary. */
export function studioModel(value, availableBlocks = []) {
  const draft = normalizeStudioDraft(value);
  const catalog = blockCatalog(availableBlocks);
  const byName = new Map(catalog.map((block) => [block.name, block]));
  const selectedBlocks = draft.blocks
    .map((name, order) => {
      const block = byName.get(name);
      return block ? { ...block, order } : null;
    })
    .filter(Boolean);
  const unknownBlocks = draft.blocks.filter((name) => !byName.has(name));

  const tools = [];
  const features = [];
  const seenTools = new Set();
  const seenFeatures = new Set();
  for (const block of selectedBlocks) {
    for (const tool of block.tools) {
      if (seenTools.has(tool)) continue;
      seenTools.add(tool);
      tools.push(tool);
    }
    for (const feature of block.features) {
      if (seenFeatures.has(feature)) continue;
      seenFeatures.add(feature);
      features.push(feature);
    }
  }

  const proposalReach = features.includes("proposals")
    || tools.some((tool) => STAGES[stageOf(tool)]?.id === "propose");
  const stages = orderedStages(
    draft.workflow.strategy,
    tools,
    selectedBlocks,
    proposalReach,
  );
  const errors = [];
  if (!draft.title) errors.push("Name the assistant.");
  if (!draft.description) errors.push("Describe what the assistant should do.");
  if (!draft.instructions) errors.push("Add working instructions.");
  if (!draft.blocks.length) errors.push("Choose at least one capability block.");
  if (draft.blocks.length && !catalog.length) {
    errors.push("Capability blocks are unavailable.");
  } else if (unknownBlocks.length) {
    errors.push(`Unavailable capability blocks: ${unknownBlocks.join(", ")}.`);
  }

  const ready = errors.length === 0;
  return {
    draft,
    availableBlocks: catalog.map((block) => ({
      ...block,
      selected: draft.blocks.includes(block.name),
      order: draft.blocks.indexOf(block.name),
    })),
    selectedBlocks,
    selectedBlockNames: selectedBlocks.map((block) => block.name),
    unknownBlocks,
    tools,
    toolCount: tools.length,
    uniqueToolCount: tools.length,
    features,
    stages,
    reachableStages: stages.filter((stage) => stage.reachable),
    proposalReach,
    canPropose: proposalReach,
    canWriteModel: proposalReach,
    writeReach: {
      model: proposalReach ? "preview" : "none",
      proposals: proposalReach,
      artifacts: tools.some((tool) => ARTIFACT_WRITE_TOOLS.has(tool)),
    },
    ready,
    errors,
    readiness: { ready, errors },
  };
}

export const deriveStudioModel = studioModel;

/** Return a new draft with one selected block moved to another position. */
export function reorderSelectedBlocks(value, fromIndex, toIndex) {
  const draft = normalizeStudioDraft(value);
  if (!Number.isInteger(fromIndex) || !Number.isInteger(toIndex)) return draft;
  if (
    fromIndex < 0
    || fromIndex >= draft.blocks.length
    || toIndex < 0
    || toIndex >= draft.blocks.length
    || fromIndex === toIndex
  ) {
    return draft;
  }
  const blocks = [...draft.blocks];
  const [moved] = blocks.splice(fromIndex, 1);
  blocks.splice(toIndex, 0, moved);
  return { ...draft, blocks };
}

/** Build the request body accepted by the custom-agent endpoint. */
export function studioPayload(value) {
  const draft = normalizeStudioDraft(value);
  const payload = {
    title: draft.title,
    description: draft.description,
    instructions: draft.instructions,
    blocks: [...draft.blocks],
    starters: [...draft.starters],
    workflow: { ...draft.workflow },
    content_paths: draft.contentPaths === null ? null : [...draft.contentPaths],
  };
  if (draft.name) payload.name = draft.name;
  return payload;
}

export const customAgentPayload = studioPayload;

/** One failure-safe browser draft. Credentials and transcripts never enter it. */
export class StudioDraftStore {
  constructor(storage, name = STUDIO_STORAGE_NAME) {
    this.storage = storage;
    this.name = name;
  }

  load() {
    try {
      const source = this.storage.getItem(this.name);
      if (!source) return null;
      const payload = JSON.parse(source);
      const draft = payload?.version === STORAGE_VERSION ? payload.draft : payload;
      if (!plain(draft)) return null;
      const looksLikeDraft = ["title", "description", "instructions", "blocks", "workflow"]
        .some((key) => Object.hasOwn(draft, key));
      return looksLikeDraft ? normalizeStudioDraft(draft) : null;
    } catch {
      return null;
    }
  }

  save(value) {
    const draft = normalizeStudioDraft(value);
    try {
      this.storage.setItem(
        this.name,
        JSON.stringify({ version: STORAGE_VERSION, draft }),
      );
      return draft;
    } catch {
      return null;
    }
  }

  clear() {
    try {
      this.storage.removeItem(this.name);
      return true;
    } catch {
      return false;
    }
  }
}
