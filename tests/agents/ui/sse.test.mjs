import assert from "node:assert/strict";
import { test } from "node:test";

import { decodeSSE } from "../../../src/ifc_console/agents/static/sse.js";

const frame = (payload) => `data: ${JSON.stringify(payload)}\n\n`;

test("decoding handles CRLF, malformed frames, and the done marker", () => {
  const decoded = decodeSSE(
    "data: {\"type\":\"content\",\"text\":\"a\"}\r\n\r\n" +
    "data: nope\r\n\r\ndata: [DONE]\r\n\r\ndata: {\"type\":\"cont",
  );
  assert.deepEqual(decoded.events, [{ type: "content", text: "a" }]);
  assert.equal(decoded.done, true);
  assert.equal(decoded.rest, 'data: {"type":"cont');
});

test("a frame without the space after data: still decodes", () => {
  const decoded = decodeSSE('data:{"type":"content","text":"a"}\n\n');
  assert.deepEqual(decoded.events, [{ type: "content", text: "a" }]);
});

test("a whole buffer decodes to its events and leaves no tail", () => {
  const { events, rest, done } = decodeSSE(
    frame({ type: "step_started", id: "a" }) + frame({ type: "done" }),
  );
  assert.deepEqual(events.map((event) => event.type), ["step_started", "done"]);
  assert.equal(rest, "");
  assert.equal(done, false);
});

test("a frame split across two reads survives", () => {
  const whole = frame({ type: "step_finished", id: "a", state: "succeeded" });
  const cut = Math.floor(whole.length / 2);

  const first = decodeSSE(whole.slice(0, cut));
  assert.deepEqual(first.events, []);

  const second = decodeSSE(first.rest + whole.slice(cut));
  assert.equal(second.events.length, 1);
  assert.equal(second.events[0].state, "succeeded");
  assert.equal(second.rest, "");
});

test("a malformed frame is dropped without losing the others", () => {
  const { events } = decodeSSE("data: {not json}\n\n" + frame({ type: "step_started", id: "b" }));
  assert.equal(events.length, 1);
  assert.equal(events[0].id, "b");
});

test("comment and non-data lines are ignored", () => {
  const { events } = decodeSSE(": keep-alive\n\n" + frame({ type: "usage" }));
  assert.deepEqual(events.map((event) => event.type), ["usage"]);
});

test("an empty or missing buffer decodes to nothing", () => {
  for (const source of [undefined, null, ""]) {
    assert.deepEqual(decodeSSE(source), { events: [], rest: "", done: false });
  }
});
