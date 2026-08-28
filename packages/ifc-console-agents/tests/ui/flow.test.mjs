import assert from "node:assert/strict";
import { test } from "node:test";

import {
  STAGES,
  applyEvent,
  composerIntent,
  emptyRun,
  duration,
  globalIdsIn,
  pretty,
  settleRun,
  stageOf,
  stagesForTools,
  timeline,
  toolHeadline,
  transcriptBlocks,
} from "../../src/ifc_console_agents/static/chat_flow.js";

const GUID_A = "0aBcDeFgHiJkLmNoPqRsT1";
const GUID_B = "3zYxWvUtSrQpOnMlKjIhG2";

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

test("an error event is captured rather than thrown", () => {
  const run = emptyRun();
  applyEvent(run, { type: "error", text: "provider refused" });
  assert.equal(run.error, "provider refused");
});

test("blocks keep the order the turn happened in", () => {
  const run = emptyRun();
  for (const event of [
    { type: "content", text: "Looking " },
    { type: "content", text: "at the walls." },
    { type: "tool_call", id: "a", name: "query_elements", arguments: '{"ifc_class":"IfcWall"}' },
    {
      type: "tool_result",
      id: "a",
      name: "query_elements",
      ok: true,
      summary: "3 row(s)",
      rows: 3,
      preview: '{"rows":[]}',
    },
    { type: "content", text: "Three walls." },
  ]) {
    applyEvent(run, event);
  }
  assert.deepEqual(run.blocks.map((block) => block.kind), ["text", "tool", "text"]);
  assert.equal(run.blocks[0].text, "Looking at the walls.");
  assert.equal(run.blocks[1].args, '{"ifc_class":"IfcWall"}');
  assert.equal(run.blocks[1].preview, '{"rows":[]}');
  assert.equal(run.blocks[1].rows, 3);
  assert.equal(run.blocks[2].text, "Three walls.");
  // the tool entry and the block are the same object, so both views agree
  assert.equal(run.tools[0], run.blocks[1]);
});

test("a repainted block is only repainted when it changed", () => {
  const run = emptyRun();
  applyEvent(run, { type: "content", text: "a" });
  const first = run.blocks[0].v;
  applyEvent(run, { type: "content", text: "b" });
  assert.ok(run.blocks[0].v > first);
});

test("tool progress updates the matching call and its compact headline", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "analyze_element_geometry" });
  const before = run.tools[0].v;
  applyEvent(run, {
    type: "tool_progress",
    id: "a",
    done: 18,
    total: 40,
    note: "Tessellating selected elements",
    elapsed: 0.8,
  });
  assert.deepEqual(run.tools[0].progress, {
    done: 18,
    total: 40,
    note: "Tessellating selected elements",
    elapsed: 0.8,
  });
  assert.equal(toolHeadline(run.tools[0]), "Tessellating selected elements");
  assert.ok(run.tools[0].v > before);
});

test("a completed tool keeps its structured output for lazy inspection", () => {
  const run = emptyRun();
  const output = { ok: true, data: { rows: [{ name: "Wall" }] }, meta: { returned: 1 } };
  applyEvent(run, { type: "tool_call", id: "a", name: "query_elements" });
  applyEvent(run, { type: "tool_result", id: "a", ok: true, output });
  assert.equal(run.tools[0].output, output);
});

test("usage adds up across tool rounds instead of being overwritten", () => {
  const run = emptyRun();
  applyEvent(run, { type: "usage", in: 100, out: 20 });
  applyEvent(run, { type: "usage", in: 140, out: 35 });
  assert.deepEqual(run.usage, { in: 240, out: 55 });
});

test("a provider that reports no token counts does not invent any", () => {
  const run = emptyRun();
  applyEvent(run, { type: "usage" });
  assert.deepEqual(run.usage, { in: null, out: null });
});

test("a failed tool reports the console's message, not just 'failed'", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "measure_elements" });
  applyEvent(run, {
    type: "tool_result",
    id: "a",
    name: "measure_elements",
    ok: false,
    summary: "bad_selector",
    detail: "IfcWal is not an IFC class",
  });
  assert.equal(toolHeadline(run.tools[0]), "bad_selector: IfcWal is not an IFC class");
});

test("a parser error does not become a paragraph in the collapsed line", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "query_elements" });
  applyEvent(run, {
    type: "tool_result",
    id: "a",
    name: "query_elements",
    ok: false,
    summary: "INVALID_QUERY",
    detail: "parse failed:\n\t* DOT\n\t* BANG\n\t* EQUAL\n\t* MORETHAN\n",
  });
  const line = toolHeadline(run.tools[0]);
  assert.ok(!line.includes("\n"), "the summary line must stay one line");
  assert.ok(line.length <= 80, line.length);
  assert.match(line, /^INVALID_QUERY: parse failed/);
});

test("a message typed while the agent answers is queued, never an abort", () => {
  // pressing Enter mid-stream used to kill the run and keep the text in the
  // box, losing both the answer and the question
  assert.equal(composerIntent({ busy: true, text: "and the columns?" }), "queue");
  // an empty composer is the one keyboard way left to stop
  assert.equal(composerIntent({ busy: true, text: "   " }), "stop");
  assert.equal(composerIntent({ busy: false, text: "how many walls?" }), "send");
  assert.equal(composerIntent({ busy: false, text: "" }), "ignore");
  assert.equal(composerIntent(), "ignore");
});

test("a stopped run leaves no tool claiming to still be running", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "validate_model" });
  settleRun(run, { stopped: true });
  assert.equal(run.tools[0].state, "bad");
  assert.equal(run.tools[0].summary, "stopped");
});

test("a run that ends resolves the approval nobody can answer any more", () => {
  const run = emptyRun();
  applyEvent(run, {
    type: "approval",
    id: "t1",
    request_id: "r1",
    name: "execute_ifc_code",
    capabilities: ["ifc.write"],
  });
  const before = run.approvals[0].v;
  settleRun(run, { stopped: true });
  assert.equal(run.approvals[0].state, "denied");
  assert.equal(run.approvals[0].decidedBy, "run stopped");
  assert.ok(run.approvals[0].v > before, "the card has to repaint");
  // a run that simply ended says so differently, and neither one lies about
  // who decided: the server denied it while tearing the turn down
  const dropped = emptyRun();
  applyEvent(dropped, { type: "approval", id: "t2", request_id: "r2", name: "save_ifc_file" });
  settleRun(dropped, { stopped: false });
  assert.equal(dropped.approvals[0].decidedBy, "run ended");
});

test("an answered approval is left exactly as the user answered it", () => {
  const run = emptyRun();
  applyEvent(run, { type: "approval", id: "t1", request_id: "r1", name: "execute_ifc_code" });
  applyEvent(run, { type: "approval_decided", id: "t1", approved: true, decided_by: "user" });
  settleRun(run, { stopped: true });
  assert.equal(run.approvals[0].state, "approved");
  assert.equal(run.approvals[0].decidedBy, "user");
});

test("GlobalIds are found in tool output and lookalikes are not", () => {
  const preview = `{"rows":[{"guid":"${GUID_A}"},{"guid":"${GUID_B}"},{"guid":"${GUID_A}"}]}`;
  assert.deepEqual(globalIdsIn(preview), [GUID_A, GUID_B]);
  assert.deepEqual(globalIdsIn(`${GUID_A} and ${GUID_B}.`), [GUID_A, GUID_B]);
  // 4 is out of range for the first character, 21 chars is one short, and a
  // longer hash must not surrender a 22-char slice of itself
  assert.deepEqual(globalIdsIn("4aBcDeFgHiJkLmNoPqRsT1"), []);
  assert.deepEqual(globalIdsIn("0aBcDeFgHiJkLmNoPqRsT"), []);
  assert.deepEqual(globalIdsIn(`x${GUID_A}x`), []);
  assert.deepEqual(globalIdsIn(""), []);
  assert.deepEqual(globalIdsIn(undefined), []);
});

test("pretty formats JSON and leaves anything else alone", () => {
  assert.equal(pretty('{"a":1}'), '{\n "a": 1\n}');
  assert.equal(pretty({ a: 1 }), '{\n "a": 1\n}');
  assert.equal(pretty("not json"), "not json");
  assert.equal(pretty(""), "");
  assert.equal(pretty(undefined), "");
});

test("the transcript keeps the tool trail, bounded", () => {
  const run = emptyRun();
  applyEvent(run, { type: "reasoning", text: "hmm" });
  applyEvent(run, { type: "tool_call", id: "a", name: "query_elements", arguments: "{}" });
  applyEvent(run, { type: "tool_result", id: "a", name: "query_elements", ok: true, summary: "ok" });
  applyEvent(run, { type: "content", text: "done" });
  applyEvent(run, { type: "content", text: "" });
  const blocks = transcriptBlocks(run, { chars: 2 });
  assert.deepEqual(blocks.map((block) => block.kind), ["reasoning", "tool", "text"]);
  assert.equal(blocks[1].name, "query_elements");
  assert.equal(blocks[2].text, "do");
});

test("a tool call records how long the console took", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "query_elements" }, { now: 1000 });
  applyEvent(run, { type: "tool_result", id: "a", ok: true, summary: "ok" }, { now: 1430 });
  assert.equal(run.tools[0].ms, 430);
  assert.equal(duration(run.tools[0].ms), "430ms");
});

test("a run with no clock reports no durations rather than wrong ones", () => {
  const run = emptyRun();
  applyEvent(run, { type: "tool_call", id: "a", name: "query_elements" });
  applyEvent(run, { type: "tool_result", id: "a", ok: true, summary: "ok" });
  assert.equal(run.tools[0].ms, null);
  assert.equal(duration(run.tools[0].ms), "");
});

test("durations stay short at every scale", () => {
  assert.equal(duration(0), "0ms");
  assert.equal(duration(999), "999ms");
  assert.equal(duration(1500), "1.5s");
  assert.equal(duration(64_000), "64s");
  assert.equal(duration(undefined), "");
  assert.equal(duration(-5), "");
});
