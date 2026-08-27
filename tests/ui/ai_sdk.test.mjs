import assert from "node:assert/strict";
import { test } from "node:test";

import {
  IFC_PROPOSAL_PART,
  IFC_TOOL_PROGRESS_PART,
  agentChatRequest,
  createIfcChatTransport,
  createIfcStreamAdapter,
  decodeIfcSSE,
  ifcSSEToUIMessageStream,
  normalizeIfcProposal,
  plainChatRequest,
  uiMessageFromRun,
  uiMessagesToTurns,
  uiPartsFromRun,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/chat_ai_sdk.js";

const collect = async (stream) => {
  const chunks = [];
  for await (const chunk of stream) chunks.push(chunk);
  return chunks;
};

const byteStream = (source) => new ReadableStream({
  start(controller) {
    const bytes = new TextEncoder().encode(source);
    const middle = Math.floor(bytes.length / 2);
    controller.enqueue(bytes.slice(0, middle));
    controller.enqueue(bytes.slice(middle));
    controller.close();
  },
});

test("run blocks become ordered AI SDK UI message parts", () => {
  const parts = uiPartsFromRun({
    blocks: [
      { kind: "reasoning", text: "Find walls" },
      {
        kind: "tool",
        id: "call-1",
        name: "query_elements",
        state: "running",
        args: "{\"ifc_class\":\"IfcWall\"}",
      },
      { kind: "text", text: "There are three walls." },
    ],
  });
  assert.deepEqual(parts.map((part) => part.type), ["reasoning", "dynamic-tool", "text"]);
  assert.equal(parts[0].state, "done");
  assert.equal(parts[1].state, "input-available");
  assert.deepEqual(parts[1].input, { ifc_class: "IfcWall" });
  assert.equal(Object.hasOwn(parts[1], "providerExecuted"), false);
  assert.equal(Object.hasOwn(parts[1], "toolMetadata"), false);
});

test("completed tool parts preserve structured output", () => {
  const output = { ok: true, data: { count: 3 }, meta: {} };
  const [part] = uiPartsFromRun({
    blocks: [{
      kind: "tool",
      id: "call-1",
      name: "query_elements",
      state: "ok",
      output,
    }],
  });
  assert.equal(part.state, "output-available");
  assert.deepEqual(part.output.output, output);
});

test("finished and failed tool blocks use AI SDK dynamic tool states", () => {
  const parts = uiPartsFromRun({
    blocks: [
      {
        kind: "tool",
        id: "ok",
        name: "query_elements",
        state: "ok",
        args: "{}",
        summary: "3 rows",
        rows: 3,
        ms: 20,
      },
      {
        kind: "tool",
        id: "bad",
        name: "measure_elements",
        state: "bad",
        args: "not-json",
        summary: "bad selector",
        detail: "IfcWal is invalid",
      },
    ],
  });
  assert.equal(parts[0].state, "output-available");
  assert.deepEqual(parts[0].output, {
    ok: true,
    summary: "3 rows",
    preview: "",
    detail: "",
    rows: 3,
    durationMs: 20,
  });
  assert.equal(parts[1].state, "output-error");
  assert.equal(parts[1].input, "not-json");
  assert.equal(parts[1].errorText, "bad selector: IfcWal is invalid");
});

test("IFC proposals have a stable typed data shape", () => {
  const proposal = normalizeIfcProposal({
    id: "change-1",
    count: 2,
    elements: 5,
    pset: "IfcConsole_AI_Measurements",
    property: "Area",
    value: 12.5,
    marked: true,
    provenance_change_set: "provenance-1",
  });
  assert.deepEqual(proposal, {
    changeSetId: "change-1",
    changeCount: 2,
    elementCount: 5,
    psetName: "IfcConsole_AI_Measurements",
    propertyName: "Area",
    value: 12.5,
    unit: "",
    method: "",
    source: "",
    confidence: "",
    marked: true,
    provenanceChangeSet: "provenance-1",
    warning: "",
    aiGenerated: true,
  });
  const [part] = uiPartsFromRun({ blocks: [{ kind: "proposal", proposal }] });
  assert.equal(part.type, IFC_PROPOSAL_PART);
  assert.equal(part.id, "change-1");
  assert.deepEqual(part.data, proposal);
});

test("a run becomes one UIMessage with IFC metadata", () => {
  const message = uiMessageFromRun({
    threadId: "panel-1",
    usage: { in: 20, out: 5 },
    finishReason: "stop",
    blocks: [{ kind: "text", text: "Done" }],
  }, { id: "message-1", metadata: { model: "local" } });
  assert.equal(message.id, "message-1");
  assert.equal(message.role, "assistant");
  assert.deepEqual(message.metadata, {
    model: "local",
    threadId: "panel-1",
    usage: { inputTokens: 20, outputTokens: 5 },
    finishReason: "stop",
  });
  assert.equal(message.parts[0].text, "Done");
});

test("live state applies only to the open final text part", () => {
  const parts = uiPartsFromRun({
    blocks: [{ kind: "text", text: "First" }, { kind: "text", text: "Second" }],
  }, { live: true });
  assert.equal(parts[0].state, "done");
  assert.equal(parts[1].state, "streaming");
});

test("plain chat turns keep text and ignore UI-only parts", () => {
  const messages = [
    {
      id: "u",
      role: "user",
      parts: [
        { type: "text", text: "Inspect" },
        {
          type: "file",
          mediaType: "application/pdf",
          filename: "manual.pdf",
          url: "data:application/pdf;base64,cGRm",
        },
        { type: "data-ifc-context", data: { selected: 3 } },
      ],
    },
    {
      id: "a",
      role: "assistant",
      parts: [{ type: "reasoning", text: "hidden" }, { type: "text", text: "Ready" }],
    },
  ];
  assert.deepEqual(uiMessagesToTurns(messages), [
    { role: "user", text: "Inspect" },
    { role: "assistant", text: "Ready" },
  ]);
  assert.deepEqual(plainChatRequest(messages, {
    provider: "openrouter",
    model: "model-a",
    temperature: 0,
    tools: true,
  }), {
    turns: [{ role: "user", text: "Inspect" }, { role: "assistant", text: "Ready" }],
    provider: "openrouter",
    model: "model-a",
    temperature: 0,
    tools: true,
  });
});

test("agent requests keep only local upload path strings", () => {
  const messages = [
    { role: "user", parts: [{ type: "text", text: "First" }] },
    { role: "assistant", parts: [{ type: "text", text: "Answer" }] },
    { role: "user", parts: [{ type: "text", text: "Measure walls" }] },
  ];
  assert.deepEqual(agentChatRequest(messages, {
    agent: "measurement",
    model: "local-model",
    thread_id: "panel-1",
    persist_history: true,
    additional_instructions: "Use millimetres.",
    attachments: [
      ".ifc-console/agents/references/manual.pdf",
      "",
      { url: "file:///tmp/not-an-upload.pdf" },
      4,
    ],
  }), {
    agent: "measurement",
    prompt: "Measure walls",
    model: "local-model",
    thread_id: "panel-1",
    persist_history: true,
    additional_instructions: "Use millimetres.",
    attachments: [".ifc-console/agents/references/manual.pdf"],
  });
});

test("SSE decoding handles CRLF, malformed frames, and the done marker", () => {
  const decoded = decodeIfcSSE(
    "data: {\"type\":\"content\",\"text\":\"a\"}\r\n\r\n" +
    "data: nope\r\n\r\ndata: [DONE]\r\n\r\ndata: {\"type\":\"cont",
  );
  assert.deepEqual(decoded.events, [{ type: "content", text: "a" }]);
  assert.equal(decoded.done, true);
  assert.equal(decoded.rest, 'data: {"type":"cont');
});

test("a frame without the space after data: still decodes", () => {
  // The panel used to carry its own decoder that required "data: " exactly.
  const decoded = decodeIfcSSE('data:{"type":"content","text":"a"}\n\n');
  assert.deepEqual(decoded.events, [{ type: "content", text: "a" }]);
});

test("current events become valid AI SDK chunk lifecycles", () => {
  const adapter = createIfcStreamAdapter({ messageId: "message-1" });
  const chunks = [
    ...adapter.push({ type: "thread", id: "panel-1", agent: "general" }),
    ...adapter.push({ type: "reasoning", text: "Inspect" }),
    ...adapter.push({ type: "tool_call", id: "call-1", name: "query_elements", arguments: "{}" }),
    ...adapter.push({
      type: "tool_progress", id: "call-1", name: "query_elements", done: 2, total: 3,
    }),
    ...adapter.push({
      type: "tool_result",
      id: "call-1",
      ok: true,
      summary: "3 rows",
      rows: 3,
      output: { ok: true, data: { count: 3 }, meta: {} },
    }),
    ...adapter.push({ type: "content", text: "Three" }),
    ...adapter.push({ type: "content", text: " walls" }),
    ...adapter.push({ type: "done" }),
  ];
  assert.deepEqual(chunks.map((chunk) => chunk.type), [
    "start",
    "start-step",
    "data-ifc-thread",
    "reasoning-start",
    "reasoning-delta",
    "reasoning-end",
    "tool-input-available",
    IFC_TOOL_PROGRESS_PART,
    "tool-output-available",
    "text-start",
    "text-delta",
    "text-delta",
    "text-end",
    "finish-step",
    "finish",
  ]);
  const starts = chunks.filter((chunk) => chunk.type === "text-start");
  const deltas = chunks.filter((chunk) => chunk.type === "text-delta");
  assert.equal(deltas.every((chunk) => chunk.id === starts[0].id), true);
  assert.equal(chunks.at(-1).finishReason, "stop");
  const progress = chunks.find((chunk) => chunk.type === IFC_TOOL_PROGRESS_PART);
  assert.deepEqual(progress.data, {
    toolCallId: "call-1",
    toolName: "query_elements",
    done: 2,
    total: 3,
    note: "",
    elapsedSeconds: 0,
  });
  const output = chunks.find((chunk) => chunk.type === "tool-output-available");
  assert.equal(output.output.output.data.count, 3);
  for (const chunk of chunks.filter((item) => item.type.startsWith("tool-"))) {
    assert.equal(Object.hasOwn(chunk, "providerExecuted"), false);
  }
});

test("unknown events cannot consume the stream start lifecycle", () => {
  const adapter = createIfcStreamAdapter();
  assert.deepEqual(adapter.push({ type: "future-event" }), []);
  const chunks = adapter.push({ type: "content", text: "Hello" });
  assert.deepEqual(chunks.slice(0, 3).map((chunk) => chunk.type), [
    "start",
    "start-step",
    "text-start",
  ]);
});

test("errors end a stream with the error finish reason", () => {
  const adapter = createIfcStreamAdapter();
  const chunks = [
    ...adapter.push({ type: "content", text: "Partial" }),
    ...adapter.push({ type: "error", text: "provider refused" }),
    ...adapter.push({ type: "done" }),
  ];
  assert.equal(chunks.find((chunk) => chunk.type === "error").errorText, "provider refused");
  assert.equal(chunks.at(-1).finishReason, "error");
});

test("provider finish reasons normalize to the UI SDK vocabulary", () => {
  const adapter = createIfcStreamAdapter();
  adapter.push({ type: "finish", reason: "max_tokens" });
  assert.equal(adapter.push({ type: "done" }).at(-1).finishReason, "length");
});

test("a byte SSE stream converts without depending on SDK packages", async () => {
  const chunks = await collect(ifcSSEToUIMessageStream(byteStream(
    'data: {"type":"content","text":"Hello"}\n\n' +
    'data: {"type":"done"}\n\n',
  ), { messageId: "m-1" }));
  assert.equal(chunks[0].messageId, "m-1");
  assert.equal(chunks.find((chunk) => chunk.type === "text-delta").delta, "Hello");
  assert.equal(chunks.at(-1).type, "finish");
});

test("the logical done event closes a response that stays connected", async () => {
  let cancelled = false;
  const source = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode('data: {"type":"done"}\n\n'));
    },
    cancel() {
      cancelled = true;
    },
  });
  const chunks = await collect(ifcSSEToUIMessageStream(source));
  assert.equal(chunks.at(-1).type, "finish");
  assert.equal(cancelled, true);
});

test("the transport posts current Python bodies and returns UI chunks", async () => {
  let request;
  const fetcher = async (url, options) => {
    request = { url, options };
    return new Response(byteStream(
      'data: {"type":"content","text":"Ready"}\n\n' +
      'data: {"type":"done"}\n\n',
    ), { status: 200 });
  };
  const transport = createIfcChatTransport({
    agent: "general",
    token: () => "secret",
    context: { model: "local-model", thread_id: "panel-1" },
    fetcher,
  });
  const stream = await transport.sendMessages({
    chatId: "chat-1",
    trigger: "regenerate-message",
    messageId: "message-previous",
    metadata: { source: "ifc-viewer", selectedElements: 3 },
    messages: [{ role: "user", parts: [{ type: "text", text: "Inspect" }] }],
    ignored: "not-forwarded",
  });
  const chunks = await collect(stream);
  assert.equal(request.url, "/api/agents/stream");
  assert.equal(request.options.headers.get("Authorization"), "Bearer secret");
  assert.deepEqual(JSON.parse(request.options.body), {
    agent: "general",
    prompt: "Inspect",
    model: "local-model",
    thread_id: "panel-1",
    chatId: "chat-1",
    trigger: "regenerate-message",
    messageId: "message-previous",
    metadata: { source: "ifc-viewer", selectedElements: 3 },
  });
  assert.equal(chunks.find((chunk) => chunk.type === "text-delta").delta, "Ready");
  assert.equal(await transport.reconnectToStream(), null);
});

test("transport drops invalid request context fields", async () => {
  let requestBody;
  const transport = createIfcChatTransport({
    fetcher: async (_url, options) => {
      requestBody = JSON.parse(options.body);
      return new Response(byteStream('data: {"type":"done"}\n\n'), { status: 200 });
    },
  });
  await collect(await transport.sendMessages({
    chatId: 12,
    trigger: "future-trigger",
    messageId: {},
    metadata: ["not", "an", "object"],
    messages: [{ role: "user", text: "Hi" }],
    ignored: "not-forwarded",
  }));
  assert.deepEqual(requestBody, { turns: [{ role: "user", text: "Hi" }] });
});

test("transport errors use the Python route hint", async () => {
  const transport = createIfcChatTransport({
    fetcher: async () => new Response(JSON.stringify({
      error: "chat disabled",
      hint: "Turn chat on first.",
    }), { status: 404, headers: { "Content-Type": "application/json" } }),
  });
  await assert.rejects(
    transport.sendMessages({ messages: [{ role: "user", text: "Hi" }] }),
    /Turn chat on first/,
  );
});
