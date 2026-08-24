import assert from "node:assert/strict";
import { test } from "node:test";

import {
  TABS,
  fileGroups,
  formatBytes,
  reachSentence,
  toolsByStage,
  workspaceModel,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/chat_workspace.js";

const PAYLOAD = {
  agent: { name: "measurement", title: "Measurement", description: "Measures things." },
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
    { name: "query_elements", summary: "s", stage: "scope", read_only: true, writes_model: false },
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
  viewer: false,
  mode: "ask",
  write_policy: { property_sets: ["IfcConsole_AI_Measurements"], note: "previews only" },
  limits: { max_tool_rounds: 8, timeout_s: 300 },
  files: [
    { name: "manual.pdf", media: "document", indexed: true, size_bytes: 4096, path: "a/manual.pdf" },
    { name: "shot.png", media: "image", indexed: false, size_bytes: 2048, path: "a/shot.png" },
  ],
};

test("the workspace has exactly the four tabs the panel renders", () => {
  assert.deepEqual(TABS.map((tab) => tab.id), ["overview", "tools", "files", "settings"]);
});

test("tools are grouped by stage, and stages with no tools disappear", () => {
  const groups = toolsByStage(PAYLOAD);
  assert.deepEqual(groups.map((group) => group.id), ["scope", "propose", "other"]);
  assert.equal(groups[2].tools[0].name, "orphan_tool");
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
  assert.deepEqual(model.counts, { overview: 0, tools: 3, files: 2, settings: 0 });
  assert.deepEqual(model.unavailable, ["get_viewer_screenshot"]);
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

test("files can be supplied separately from the payload", () => {
  const model = workspaceModel(PAYLOAD, { files: [] });
  assert.equal(model.fileGroups.total, 0);
  assert.equal(model.counts.files, 0);
});

test("byte sizes stay short", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(4096), "4 KB");
  assert.equal(formatBytes(5 * 1024 * 1024), "5.0 MB");
  assert.equal(formatBytes(undefined), "0 B");
});
