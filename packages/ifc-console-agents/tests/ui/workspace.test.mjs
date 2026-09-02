import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canonicalSelectionGuids,
  TABS,
  fileGroups,
  formatBytes,
  geometryRequestToken,
  geometrySelectionToken,
  measurementAlternativeRows,
  measurementDryRunCanPropose,
  measurementDryRunIsPartial,
  measurementEvidenceKind,
  reachSentence,
  representativeSectionStations,
  suggestedQuestions,
  tabsFor,
  toolsByStage,
  workspaceModel,
} from "../../src/ifc_console_agents/static/chat_workspace.js";

const PAYLOAD = {
  agent: {
    name: "measurement",
    title: "Measurement",
    description: "Measures things.",
    features: ["files"],
  },
  kind: "built-in",
  builtin: true,
  summary: "The general assistant with a sharper prompt.",
  role: "You are a measurement assistant.",
  examples: [{ title: "Recipe", prompt: "Measure the walls", note: "uses the recipe" }],
  blocks: [
    { name: "ifc-context", title: "IFC context", description: "d", available: true, tools: ["query_elements"], missing: [] },
    { name: "viewer", title: "Viewer vision", description: "d", available: false, tools: [], missing: ["get_viewer_screenshot"] },
  ],
  stages: [
    { id: "scope", label: "Scope", hint: "h", available: true, tools: ["query_elements"] },
    { id: "propose", label: "Propose", hint: "h", available: true, tools: ["measure__propose_measured_value"] },
    { id: "verify", label: "Verify", hint: "h", available: false, tools: [] },
  ],
  tools: [
    {
      name: "query_elements",
      summary: "s",
      stage: "scope",
      read_only: true,
      writes_model: false,
      input_schema: {
        type: "object",
        properties: {
          selector: { type: "string", description: "IFC selector to run." },
          limit: { type: "integer", default: 50 },
        },
        required: ["selector"],
        additionalProperties: false,
      },
    },
    {
      name: "measure__propose_measured_value",
      summary: "s",
      stage: "propose",
      read_only: false,
      writes_model: true,
    },
    { name: "orphan_tool", summary: "s", stage: "", read_only: true, writes_model: false },
  ],
  writes: [{ name: "measure__propose_measured_value" }],
  unavailable_tools: ["get_viewer_screenshot"],
  artifact_writes: ["export_csv"],
  viewer: false,
  mode: "ask",
  write_policy: { property_sets: ["IfcConsole_AI_Measurements"], note: "previews only" },
  limits: { max_tool_rounds: 8, timeout_s: 300 },
  files: [
    { name: "manual.pdf", media: "document", indexed: true, size_bytes: 4096, path: "a/manual.pdf" },
    { name: "shot.png", media: "image", indexed: false, size_bytes: 2048, path: "a/shot.png" },
  ],
  skills: [
    {
      name: "sheet-pile-profile",
      description: "Measure a sheet pile profile",
      applies_to: "IfcMember",
      path: ".ifc-console/agents/skills/sheet-pile-profile.md",
      size_bytes: 512,
      updated_at: "2026-08-26T10:00:00+00:00",
    },
  ],
};

test("the workspace navigation has the seven sections in vertical order", () => {
  // Pipeline is not among them: an agent's reachable stages follow from the
  // blocks it holds, so the workflow belongs inside the selected agent.
  assert.deepEqual(TABS.map((tab) => tab.id), [
    "agent",
    "capabilities",
    "tools",
    "content",
    "skills",
    "models",
    "app",
  ]);
  assert.ok(!TABS.some((tab) => tab.id === "pipeline"));
});

test("Content, Skills, Models, and App remain available for an empty assistant", () => {
  const empty = workspaceModel({ agent: { features: [] }, files: [] });
  assert.deepEqual(tabsFor(empty).map((tab) => tab.id), [
    "agent",
    "capabilities",
    "tools",
    "content",
    "skills",
    "models",
    "app",
  ]);
  assert.deepEqual(empty.skills, []);
  assert.equal(empty.counts.skills, 0);
});

test("geometry review tokens are model, revision, selection, and generation scoped", () => {
  const status = {
    fingerprint: "active-fingerprint",
    models: [{ id: "model-a", active: true, etag: "model-a-fingerprint-7" }],
  };
  const selection = { model_id: "model-a", guids: ["guid-b", "guid-a", "guid-b"] };
  const token = geometrySelectionToken(selection, status);

  assert.deepEqual(canonicalSelectionGuids(selection), ["guid-a", "guid-b"]);
  assert.equal(
    token,
    geometrySelectionToken({ model_id: "model-a", guids: ["guid-a", "guid-b"] }, status),
  );
  assert.notEqual(token, geometrySelectionToken(selection, {
    ...status,
    models: [{ id: "model-a", active: true, etag: "model-a-fingerprint-8" }],
  }));
  assert.notEqual(token, geometrySelectionToken({ ...selection, model_id: "model-b" }, status));
  assert.notEqual(geometryRequestToken(token, 1), geometryRequestToken(token, 2));
});

test("skill proposals require a current complete preview with extracted values", () => {
  const selectionToken = "model-and-selection";
  const requestGeneration = 4;
  const complete = {
    selectionToken,
    requestGeneration,
    requestToken: geometryRequestToken(selectionToken, requestGeneration),
    loading: false,
    error: "",
    complete: true,
    payload: {
      data: {
        targets: { has_more: false, truncated_by_max_matches: false },
        results: [{ status: "extracted", extracted: [{ output: "profile.width" }] }],
      },
      meta: { truncated: false },
    },
  };
  const current = { selectionToken, requestGeneration };

  assert.equal(measurementDryRunCanPropose(complete, current), true);
  assert.equal(measurementDryRunCanPropose({
    ...complete,
    payload: { data: { results: [{ status: "skipped", extracted: [] }] } },
  }, current), false);
  assert.equal(measurementDryRunCanPropose({
    ...complete,
    payload: { data: { results: [{ status: "partial", extracted: [] }] } },
  }, current), false);
  assert.equal(measurementDryRunCanPropose(complete, {
    selectionToken: "new-selection",
    requestGeneration,
  }), false);
  assert.equal(measurementDryRunCanPropose(complete, {
    selectionToken,
    requestGeneration: requestGeneration + 1,
  }), false);

  for (const payload of [
    { data: { targets: { has_more: true } } },
    { data: { targets: { truncated_by_max_matches: true } } },
    { data: { meta: { truncated: true } } },
    { meta: { truncated: true } },
  ]) {
    assert.equal(measurementDryRunIsPartial(payload), true);
    assert.equal(measurementDryRunCanPropose({ ...complete, payload }, current), false);
  }
});

test("measurement evidence separates exact IFC parameters from measured geometry", () => {
  const exact = {
    value_si: 0.2,
    source: "profile_parameter",
    uncertainty_si: 0,
    flags: [],
  };
  assert.equal(measurementEvidenceKind(exact).id, "exact");
  assert.equal(measurementEvidenceKind({
    ...exact,
    flags: ["pre_boolean_parameter"],
  }).id, "measured");
  assert.equal(measurementEvidenceKind({
    ...exact,
    source: "analysis_mesh",
  }).id, "measured");

  const rows = measurementAlternativeRows({
    ...exact,
    alternatives: [{
      value_si: 0.204,
      source: "mesh_section",
      uncertainty_si: 0.001,
    }],
  }, 0.002);
  assert.equal(rows[0].evidence_kind.id, "exact");
  assert.equal(rows[1].evidence_kind.id, "measured");
  assert.ok(Math.abs(rows[1].delta_si - 0.004) < 1e-12);
  assert.equal(rows[1].within_tolerance, false);
  assert.equal(rows[1].assessment, "outside tolerance");
});

test("representative section stations merge roles and remain ordered", () => {
  const stations = representativeSectionStations({
    stations: [{ at: 0.5, descriptor: { area_si: 0.12 } }],
    representative_sections: {
      minimum: { at: 0.1, descriptor: { width_si: 0.3 } },
      dominant: { at: 0.5, descriptor: { width_si: 0.5 }, closed: true },
      transition_stations: [0.8],
    },
    profile_regions: [{
      start: 0,
      end: 0.4,
      representative_station: 0.2,
      descriptor: { height_si: 0.6 },
    }],
  });

  assert.deepEqual(stations.map((station) => station.at), [0.1, 0.2, 0.5, 0.8]);
  assert.deepEqual(stations[2].roles, ["evaluated station", "dominant"]);
  assert.equal(stations[2].descriptor.area_si, 0.12);
  assert.equal(stations[2].descriptor.width_si, 0.5);
  assert.equal(stations[2].closed, true);
});

test("tools are grouped by stage, and stages with no tools disappear", () => {
  const groups = toolsByStage(PAYLOAD);
  assert.deepEqual(groups.map((group) => group.id), ["scope", "propose", "other"]);
  assert.equal(groups[2].tools[0].name, "orphan_tool");
});

test("tools from a future stage remain visible under Other", () => {
  const groups = toolsByStage({
    ...PAYLOAD,
    tools: [
      ...PAYLOAD.tools,
      { name: "future_tool", summary: "s", stage: "future", read_only: true },
    ],
  });
  assert.deepEqual(
    groups.at(-1).tools.map((tool) => tool.name),
    ["orphan_tool", "future_tool"],
  );
});

test("files split into images and documents with an indexed count", () => {
  const groups = fileGroups(PAYLOAD.files);
  assert.equal(groups.images.length, 1);
  assert.equal(groups.documents.length, 1);
  assert.equal(groups.total, 2);
  assert.equal(groups.indexed, 1);
});

test("the model derives everything the view needs", () => {
  const model = workspaceModel(PAYLOAD);
  assert.equal(model.title, "Measurement");
  assert.equal(model.canWriteModel, true);
  assert.equal(model.availableBlocks.length, 1);
  assert.deepEqual(model.reachableStages.map((stage) => stage.id), ["scope", "propose"]);
  // the agent page now carries the stage count, because it draws the pipeline
  assert.deepEqual(model.counts, {
    agent: 3,
    capabilities: 2,
    tools: 3,
    content: 2,
    skills: 1,
    models: 0,
    app: 0,
  });
  assert.equal(model.skills[0].name, "sheet-pile-profile");
  assert.deepEqual(model.unavailable, ["get_viewer_screenshot"]);
  // the server sends snake_case; reading the camelCase name found nothing
  assert.deepEqual(model.artifactWrites, ["export_csv"]);
  assert.deepEqual(model.tools[0].input_schema, PAYLOAD.tools[0].input_schema);
  assert.deepEqual(model.stageGroups[0].tools[0].input_schema, PAYLOAD.tools[0].input_schema);
});

test("suggested questions prefer examples and fall back to agent starters", () => {
  const withExamples = workspaceModel({
    ...PAYLOAD,
    agent: { ...PAYLOAD.agent, starters: ["Starter should not win"] },
  });
  assert.deepEqual(withExamples.suggestedQuestions, PAYLOAD.examples);

  const fromStarters = workspaceModel({
    ...PAYLOAD,
    examples: [],
    agent: {
      ...PAYLOAD.agent,
      starters: ["Inspect the selected walls", "", " Summarise the open model "],
    },
  });
  assert.deepEqual(fromStarters.suggestedQuestions, [
    {
      title: "Suggested question 1",
      prompt: "Inspect the selected walls",
      note: "",
    },
    {
      title: "Suggested question 2",
      prompt: "Summarise the open model",
      note: "",
    },
  ]);
  assert.deepEqual(suggestedQuestions([], ["What is selected?"]), [
    { title: "Suggested question 1", prompt: "What is selected?", note: "" },
  ]);
});

test("a read-only agent is reported as read-only", () => {
  const model = workspaceModel({ ...PAYLOAD, writes: [] });
  assert.equal(model.canWriteModel, false);
  assert.match(reachSentence(model), /read-only/);
});

test("the reach sentence stays short while naming tools, references, and write policy", () => {
  const sentence = reachSentence(workspaceModel(PAYLOAD));
  assert.match(sentence, /3 tools/);
  assert.match(sentence, /2 files/);
  assert.match(sentence, /preview changes only/);
});

test("a missing or malformed payload renders an empty workspace, not a crash", () => {
  for (const bad of [undefined, null, "nope", 42, []]) {
    const model = workspaceModel(bad);
    assert.equal(model.name, "");
    assert.equal(model.tools.length, 0);
    assert.equal(model.canWriteModel, false);
    assert.equal(reachSentence(model), "");
  }
});

test("plain chat is described like any other assistant", () => {
  const model = workspaceModel({ ...PAYLOAD, plain: true, agent: { name: "", title: "Plain chat" } });
  assert.equal(model.plain, true);
  assert.equal(model.name, "");
  assert.equal(model.title, "Plain chat");
  assert.equal(model.usesFiles, false);
  assert.match(reachSentence(model), /3 tools/);
});

test("files can be supplied separately from the payload", () => {
  const model = workspaceModel(PAYLOAD, { files: [] });
  assert.equal(model.fileGroups.total, 0);
  assert.equal(model.counts.content, 0);
});

test("byte sizes stay short", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(4096), "4 KB");
  assert.equal(formatBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatBytes(undefined), "0 B");
});
