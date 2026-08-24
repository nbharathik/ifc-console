import assert from "node:assert/strict";
import { test } from "node:test";

import {
  STAGES,
  applyEvent,
  decodeSSE,
  emptyRun,
  stageOf,
  stagesForTools,
  timeline,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/chat_flow.js";

test("every tool in the stage map is unique to one stage", () => {
  const seen = new Map();
  for (const stage of STAGES) {
    for (const tool of stage.tools) {
      assert.equal(seen.has(tool), false, `${tool} appears in two stages`);
      seen.set(tool, stage.id);
    }
  }
});

test("tools resolve to the stage a reader would expect", () => {
  assert.equal(STAGES[stageOf("query_elements")].id, "scope");
  assert.equal(STAGES[stageOf("get_project_document_page")].id, "evidence");
  assert.equal(STAGES[stageOf("measure_elements")].id, "method");
  assert.equal(STAGES[stageOf("get_viewer_screenshot")].id, "verify");
  assert.equal(STAGES[stageOf("measure__propose_measured_value")].id, "propose");
  assert.equal(stageOf("not_a_tool"), -1);
});

test("an agent without document tools cannot reach the evidence stage", () => {
  const rows = stagesForTools(["query_elements", "get_element"]);
  const byId = Object.fromEntries(rows.map((row) => [row.id, row.available]));
  assert.equal(byId.scope, true);
  assert.equal(byId.evidence, false);
  assert.equal(byId.propose, false);
});

test("a run folds the event stream into stage progress", () => {
  const run = emptyRun();
  for (const event of [
    { type: "thread", id: "panel-1" },
    { type: "reasoning", text: "thinking" },
    { type: "tool_call", id: "a", name: "query_elements" },
    { type: "tool_result", id: "a", name: "query_elements", ok: true, summary: "3 rows" },
    { type: "tool_call", id: "b", name: "get_project_document_page" },
    { type: "tool_result", id: "b", name: "get_project_document_page", ok: true, summary: "ok" },
    { type: "tool_call", id: "c", name: "measure_elements" },
    { type: "tool_result", id: "c", name: "measure_elements", ok: false, summary: "bad selector" },
    { type: "content", text: "answer" },
    { type: "usage", in: 10, out: 20 },
  ]) {
    applyEvent(run, event);
  }
  assert.equal(run.threadId, "panel-1");
  assert.equal(run.thinking, true);
  assert.equal(run.answering, true);
  assert.equal(run.stage, 2);
  assert.equal(run.stages[0].done, true);
  assert.equal(run.stages[2].failed, 1);
  assert.deepEqual(run.usage, { in: 10, out: 20 });
  assert.equal(run.tools.find((tool) => tool.id === "c").state, "bad");
});

test("a proposal event jumps the run to the propose stage", () => {
  const run = emptyRun();
  applyEvent(run, { type: "proposal", id: "cs-1", pset: "IfcConsole_AI_Measurements" });
  assert.equal(run.stage, STAGES.length - 1);
  assert.equal(run.proposals.length, 1);
});

test("the timeline groups tools under their stage and drops empty stages", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "query_elements" });
  applyEvent(run, { type: "tool_result", id: "a", name: "query_elements", ok: true });
  const rows = timeline(run);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].id, "scope");
  assert.equal(rows[0].tools.length, 1);
});

test("decodeSSE parses whole frames and keeps the partial tail", () => {
  const { events, rest } = decodeSSE('data: {"type":"content","text":"a"}\n\ndata: {"type":"cont');
  assert.equal(events.length, 1);
  assert.equal(events[0].text, "a");
  assert.equal(rest, 'data: {"type":"cont');
});

test("decodeSSE survives a malformed frame without throwing", () => {
  const { events } = decodeSSE('data: not json\n\ndata: {"type":"done"}\n\n');
  assert.deepEqual(events, [{ type: "done" }]);
});

test("an error event is captured rather than thrown", () => {
  const run = emptyRun();
  applyEvent(run, { type: "error", text: "provider refused" });
  assert.equal(run.error, "provider refused");
});
