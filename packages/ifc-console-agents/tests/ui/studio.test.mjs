import assert from "node:assert/strict";
import { test } from "node:test";

import {
  STRATEGIES,
  StudioDraftStore,
  createStudioDraft,
  normalizeStudioDraft,
  reorderSelectedBlocks,
  studioModel,
  studioPayload,
} from "../../src/ifc_console_agents/static/chat_studio.js";

const BLOCKS = [
  {
    name: "ifc-context",
    title: "IFC context",
    description: "Resolve model facts.",
    features: [],
    tools: ["query_elements", "get_element"],
  },
  {
    name: "documents",
    title: "Project documents",
    description: "Read project evidence.",
    features: ["files", "vision"],
    tools: ["list_project_documents", "query_elements"],
  },
  {
    name: "measurements",
    title: "Measurements",
    description: "Measure geometry.",
    features: [],
    tools: ["measure_elements"],
  },
  {
    name: "quantities",
    title: "Quantity takeoff",
    description: "Export computed quantities.",
    features: [],
    tools: ["compute_quantities", "export_csv"],
  },
  {
    name: "property-proposals",
    title: "Marked property proposals",
    description: "Prepare preview-only changes.",
    features: ["proposals"],
    tools: [],
  },
];

const COMPLETE = {
  title: "Envelope reviewer",
  description: "Checks walls against project evidence.",
  instructions: "Resolve the walls, read the manual, then report evidence.",
  blocks: ["documents", "ifc-context", "property-proposals"],
  starters: ["Review the selected walls"],
  workflow: {
    strategy: "evidence-first",
    max_tool_rounds: 9,
    max_tool_calls: 30,
  },
};

class MemoryStorage {
  constructor() {
    this.values = new Map();
  }

  getItem(name) {
    return this.values.get(name) ?? null;
  }

  setItem(name, value) {
    this.values.set(name, value);
  }

  removeItem(name) {
    this.values.delete(name);
  }
}

test("Studio exposes the three supported workflow strategies", () => {
  assert.deepEqual(STRATEGIES, ["adaptive", "evidence-first", "fast-scan"]);
});

test("a blank draft has bounded workflow defaults", () => {
  const draft = createStudioDraft();
  assert.equal(draft.sourceKind, "blank");
  assert.deepEqual(draft.blocks, []);
  assert.deepEqual(draft.workflow, {
    strategy: "adaptive",
    max_tool_rounds: 12,
    max_tool_calls: 48,
  });
});

test("a built-in agent becomes a clone without its protected name or role prompt", () => {
  const draft = createStudioDraft({
    name: "general",
    title: "General assistant",
    description: "Works across the model.",
    role: "Internal built-in role prompt",
    kind: "built-in",
    blocks: ["ifc-context", "documents"],
    starters: ["What is in this model?"],
  });
  assert.equal(draft.name, "");
  assert.equal(draft.sourceName, "general");
  assert.equal(draft.sourceKind, "built-in");
  assert.equal(draft.instructions, "");
  assert.deepEqual(draft.blocks, ["ifc-context", "documents"]);
});

test("custom workspace data restores its identity, instructions, and legacy limits", () => {
  const draft = createStudioDraft({
    kind: "custom",
    agent: {
      name: "custom-envelope",
      title: "Envelope reviewer",
      description: "Checks the envelope.",
      blocks: ["documents", "ifc-context"],
      starters: ["Review the walls"],
    },
    role: "Use the project envelope procedure.",
    content: {
      access: {
        mode: "selected",
        paths: [".ifc-console/agents/references/envelope.pdf"],
      },
    },
    workflow_strategy: "fast-scan",
    limits: { max_tool_rounds: 7, max_tool_calls: 22 },
  });
  assert.equal(draft.name, "custom-envelope");
  assert.equal(draft.sourceKind, "custom");
  assert.equal(draft.instructions, "Use the project envelope procedure.");
  assert.deepEqual(draft.contentPaths, [".ifc-console/agents/references/envelope.pdf"]);
  assert.deepEqual(draft.workflow, {
    strategy: "fast-scan",
    max_tool_rounds: 7,
    max_tool_calls: 22,
  });
});

test("normalization deduplicates lists and clamps malformed workflow values", () => {
  const draft = normalizeStudioDraft({
    title: "  Review  ",
    blocks: ["documents", "documents", { name: "ifc-context" }, "bad name"],
    starters: "One\nOne\nTwo",
    strategy: "unknown",
    max_tool_rounds: 500,
    max_tool_calls: -20,
  });
  assert.equal(draft.title, "Review");
  assert.deepEqual(draft.blocks, ["documents", "ifc-context"]);
  assert.deepEqual(draft.starters, ["One", "Two"]);
  assert.deepEqual(draft.workflow, {
    strategy: "adaptive",
    max_tool_rounds: 100,
    max_tool_calls: 1,
  });
});

test("the live model keeps block order and counts every tool once", () => {
  const model = studioModel(COMPLETE, BLOCKS);
  assert.deepEqual(model.selectedBlockNames, [
    "documents",
    "ifc-context",
    "property-proposals",
  ]);
  assert.deepEqual(model.tools, ["list_project_documents", "query_elements", "get_element"]);
  assert.equal(model.uniqueToolCount, 3);
  assert.deepEqual(model.features, ["files", "vision", "proposals"]);
  assert.deepEqual(
    model.reachableStages.map((stage) => stage.id),
    ["evidence", "scope", "propose"],
  );
  assert.equal(model.reachableStages.at(-1).dynamic, true);
  assert.deepEqual(model.reachableStages.at(-1).blocks, ["property-proposals"]);
  assert.equal(model.ready, true);
});

test("proposal and artifact write reach stay explicit", () => {
  const proposal = studioModel(COMPLETE, BLOCKS);
  assert.equal(proposal.proposalReach, true);
  assert.deepEqual(proposal.writeReach, {
    model: "preview",
    proposals: true,
    artifacts: false,
  });

  const artifact = studioModel({ ...COMPLETE, blocks: ["quantities"] }, BLOCKS);
  assert.equal(artifact.proposalReach, false);
  assert.deepEqual(artifact.writeReach, {
    model: "none",
    proposals: false,
    artifacts: true,
  });
});

test("readiness explains missing fields and unavailable blocks", () => {
  const empty = studioModel({}, BLOCKS);
  assert.equal(empty.ready, false);
  assert.deepEqual(empty.errors, [
    "Name the assistant.",
    "Describe what the assistant should do.",
    "Add working instructions.",
    "Choose at least one capability block.",
  ]);

  const unknown = studioModel({ ...COMPLETE, blocks: ["documents", "missing"] }, BLOCKS);
  assert.equal(unknown.ready, false);
  assert.match(unknown.errors.at(-1), /missing/);
  assert.deepEqual(unknown.unknownBlocks, ["missing"]);
});

test("selected blocks can be reordered without mutating the input", () => {
  const input = normalizeStudioDraft(COMPLETE);
  const moved = reorderSelectedBlocks(input, 2, 0);
  assert.deepEqual(moved.blocks, ["property-proposals", "documents", "ifc-context"]);
  assert.deepEqual(input.blocks, ["documents", "ifc-context", "property-proposals"]);
  assert.deepEqual(reorderSelectedBlocks(input, -1, 2).blocks, input.blocks);
});

test("the custom-agent payload includes the workflow contract", () => {
  const payload = studioPayload({
    ...COMPLETE,
    name: "custom-envelope",
    sourceKind: "custom",
  });
  assert.deepEqual(payload, {
    name: "custom-envelope",
    title: COMPLETE.title,
    description: COMPLETE.description,
    instructions: COMPLETE.instructions,
    blocks: COMPLETE.blocks,
    starters: COMPLETE.starters,
    workflow: COMPLETE.workflow,
    content_paths: null,
  });
});

test("the draft store round-trips both current and legacy raw drafts", () => {
  const storage = new MemoryStorage();
  const store = new StudioDraftStore(storage, "studio-test");
  assert.equal(store.load(), null);
  const saved = store.save(COMPLETE);
  assert.equal(saved.title, COMPLETE.title);
  assert.deepEqual(store.load(), saved);

  storage.setItem("studio-test", JSON.stringify(COMPLETE));
  assert.equal(store.load().workflow.strategy, "evidence-first");
  assert.equal(store.clear(), true);
  assert.equal(store.load(), null);
});

test("the draft store survives corrupt and unavailable browser storage", () => {
  const storage = new MemoryStorage();
  storage.setItem("studio-test", "not json");
  assert.equal(new StudioDraftStore(storage, "studio-test").load(), null);

  const broken = {
    getItem() { throw new Error("blocked"); },
    setItem() { throw new Error("full"); },
    removeItem() { throw new Error("blocked"); },
  };
  const store = new StudioDraftStore(broken);
  assert.equal(store.load(), null);
  assert.equal(store.save(COMPLETE), null);
  assert.equal(store.clear(), false);
});
