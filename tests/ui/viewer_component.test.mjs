import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createViewerComponent,
  VIEWER_COMMAND_EVENT,
  VIEWER_CONTEXT_EVENT,
  VIEWER_RESULT_EVENT,
} from "../../src/ifc_console/viewer/static/viewer_component.js";

test("the component facade and compatibility events share one command function", async () => {
  const target = new EventTarget();
  const calls = [];
  const host = createViewerComponent({
    target,
    readContext: (reason) => ({ reason, selection: [42] }),
    execute: (command) => {
      calls.push(command);
      return { action: command.action, count: calls.length };
    },
  });

  assert.deepEqual(host.api.getContext("test"), { reason: "test", selection: [42] });
  assert.deepEqual(await host.api.execute({ action: "section" }), {
    action: "section",
    count: 1,
  });

  const contexts = [];
  const eventContexts = [];
  host.api.subscribe((context) => contexts.push(context));
  target.addEventListener(VIEWER_CONTEXT_EVENT, (event) => eventContexts.push(event.detail));
  host.publish({ reason: "selection", selection: [7] });
  assert.deepEqual(contexts, eventContexts);

  const directResults = [];
  host.api.subscribeResults((result) => directResults.push(result));
  const legacyResult = new Promise((resolve) => {
    target.addEventListener(VIEWER_RESULT_EVENT, (event) => resolve(event.detail), { once: true });
  });
  target.dispatchEvent(new CustomEvent(VIEWER_COMMAND_EVENT, {
    detail: { action: "measure_points", commandId: "legacy-1" },
  }));

  const result = await legacyResult;
  assert.equal(result.ok, true);
  assert.equal(result.commandId, "legacy-1");
  assert.deepEqual(result.result, { action: "measure_points", count: 2 });
  assert.deepEqual(directResults, [result]);
  assert.deepEqual(calls.map((call) => call.action), ["section", "measure_points"]);

  await assert.rejects(host.api.execute([]), /must be an object/);
  host.dispose();
});

test("asynchronous command failures carry useful compatibility results", async () => {
  const target = new EventTarget();
  const host = createViewerComponent({
    target,
    readContext: () => ({}),
    execute: async () => { throw new Error("camera unavailable"); },
  });
  const result = new Promise((resolve) => {
    host.api.subscribeResults(resolve);
  });
  target.dispatchEvent(new CustomEvent(VIEWER_COMMAND_EVENT, {
    detail: { action: "set-camera", commandId: "failure-1" },
  }));
  const detail = await result;
  assert.equal(detail.ok, false);
  assert.equal(detail.action, "set-camera");
  assert.match(detail.error, /camera unavailable/);
  host.dispose();
});
