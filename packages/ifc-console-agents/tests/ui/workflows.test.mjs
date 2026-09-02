import assert from "node:assert/strict";
import { test } from "node:test";

import {
  authenticatedOptions,
  clipText,
  initialScope,
  inputValue,
  missingInputs,
  normalizeViewerContext,
  parseEventStream,
  runOutcome,
  selectionCount,
} from "../../src/ifc_console_agents/static/workflows_model.js";

const frame = (payload) => `data: ${JSON.stringify(payload)}\n\n`;

test("run detail is clipped with a visible marker and never grows past its limit", () => {
  assert.equal(clipText("short", 100), "short");
  const clipped = clipText("x".repeat(500), 100);
  assert.equal(clipped.length, 100);
  assert.match(clipped, /truncated$/);
  assert.equal(clipText(null, 10), "");
  assert.equal(clipText("keep me", 0), "keep me");
});

test("a whole buffer decodes to its events and leaves no tail", () => {
  const buffer = frame({ type: "step_started", id: "a" }) + frame({ type: "done" });
  const { events, rest } = parseEventStream(buffer);
  assert.deepEqual(
    events.map((event) => event.type),
    ["step_started", "done"],
  );
  assert.equal(rest, "");
});

test("a frame split across two reads survives", () => {
  const whole = frame({ type: "step_finished", id: "a", state: "succeeded" });
  const cut = Math.floor(whole.length / 2);

  const first = parseEventStream(whole.slice(0, cut));
  assert.deepEqual(first.events, []);

  const second = parseEventStream(first.rest + whole.slice(cut));
  assert.equal(second.events.length, 1);
  assert.equal(second.events[0].state, "succeeded");
  assert.equal(second.rest, "");
});

test("a malformed frame is dropped without losing the others", () => {
  const buffer = "data: {not json}\n\n" + frame({ type: "step_started", id: "b" });
  const { events } = parseEventStream(buffer);
  assert.equal(events.length, 1);
  assert.equal(events[0].id, "b");
});

test("non-data lines are ignored", () => {
  const { events } = parseEventStream(": keep-alive\n\n" + frame({ type: "usage" }));
  assert.deepEqual(
    events.map((event) => event.type),
    ["usage"],
  );
});

test("input values are coerced by their declared type", () => {
  assert.equal(inputValue({ type: "number" }, "12"), 12);
  assert.equal(inputValue({ type: "number" }, ""), "");
  // A number the user typed badly goes to the server as typed, which answers
  // with a precise error instead of the browser silently sending NaN.
  assert.equal(inputValue({ type: "number" }, "twelve"), "twelve");
  assert.equal(inputValue({ type: "boolean" }, true), true);
  assert.equal(inputValue({ type: "text" }, undefined), "");
  assert.equal(inputValue({ type: "text" }, 5), "5");
});

test("workflow API requests carry the session token and preserve headers", () => {
  const options = authenticatedOptions("secret", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  });
  assert.equal(options.method, "POST");
  assert.deepEqual(options.headers, {
    Authorization: "Bearer secret",
    "Content-Type": "application/json",
  });
});

test("missing required inputs are reported by label", () => {
  const inputs = [
    { id: "a", label: "Newer revision", required: true },
    { id: "b", label: "Optional", required: false },
  ];
  assert.deepEqual(missingInputs(inputs, { a: "", b: "" }), ["Newer revision"]);
  assert.deepEqual(missingInputs(inputs, { a: "   ", b: "" }), ["Newer revision"]);
  assert.deepEqual(missingInputs(inputs, { a: "model-2", b: "" }), []);
});

test("a completed run reports its state and summary", () => {
  const outcome = runOutcome([
    { type: "step_finished", id: "one", state: "succeeded" },
    { type: "workflow_completed", state: "succeeded", summary: "# Report" },
  ]);
  assert.deepEqual(outcome, { state: "succeeded", summary: "# Report", done: true });
});

test("a stream that stops early is not reported as finished", () => {
  const outcome = runOutcome([{ type: "step_started", id: "one" }]);
  assert.equal(outcome.done, false);
  assert.equal(outcome.state, "interrupted");
});

test("an error frame makes the run failed, not interrupted", () => {
  const outcome = runOutcome([{ type: "error", text: "internal workflow error" }]);
  assert.equal(outcome.state, "failed");
  assert.equal(outcome.error, "internal workflow error");
});

test("viewer facade and API contexts normalize to model-scoped selections", () => {
  const context = normalizeViewerContext({
    open: true,
    model: { id: "main", name: "Office.ifc" },
    models: [{ id: "main", name: "Office.ifc" }, { model_id: "rev", name: "Rev B" }],
    selections: [
      { model_id: "main", model: "Office.ifc", guids: ["a", "b"] },
      { model_id: "rev", model: "Rev B", guids: ["c"] },
    ],
  });
  assert.equal(context.connected, true);
  assert.equal(context.models[0].active, true);
  assert.equal(selectionCount(context), 3);
  assert.deepEqual(context.selections[1], {
    model_id: "rev",
    model: "Rev B",
    guids: ["c"],
  });
});

test("selection-aware workflows start focused only when a selection exists", () => {
  const selected = { selections: [{ model_id: "main", guids: ["a"] }] };
  assert.equal(initialScope({ scope: "either" }, selected), "selection");
  assert.equal(initialScope({ scope: "either" }, { selections: [] }), "model");
  assert.equal(initialScope({ scope: "model" }, selected), "model");
  assert.equal(initialScope({ scope: "selection" }, { selections: [] }), "selection");
});
