/* Build-free compatibility with the AI SDK UI message and transport shapes. */

export const IFC_PROPOSAL_PART = "data-ifc-proposal";
export const IFC_THREAD_PART = "data-ifc-thread";
export const IFC_TOOL_PROGRESS_PART = "data-ifc-tool-progress";

const FINISH_REASONS = new Set([
  "stop",
  "length",
  "content-filter",
  "tool-calls",
  "error",
  "other",
]);

const CHAT_TRIGGERS = new Set(["submit-message", "regenerate-message"]);

const plain = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function text(value) {
  return typeof value === "string" ? value : value == null ? "" : String(value);
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function integer(value) {
  return Number.isInteger(value) && value >= 0 ? value : 0;
}

function compact(record) {
  return Object.fromEntries(Object.entries(record).filter(([, value]) => value !== undefined));
}

function jsonValue(value) {
  if (value === undefined) return null;
  try {
    return JSON.parse(JSON.stringify(value));
  } catch {
    return text(value);
  }
}

function parseInput(value) {
  if (plain(value) || Array.isArray(value)) return jsonValue(value);
  const source = text(value).trim();
  if (!source) return {};
  try {
    return JSON.parse(source);
  } catch {
    return source;
  }
}

function toolOutput(block) {
  return compact({
    ok: block.state === "ok",
    summary: text(block.summary),
    preview: text(block.preview),
    output: block.output == null ? undefined : jsonValue(block.output),
    detail: text(block.detail),
    rows: number(block.rows) ?? undefined,
    durationMs: number(block.ms) ?? undefined,
  });
}

function toolError(block) {
  return [text(block.summary), text(block.detail)].filter(Boolean).join(": ") || "Tool failed";
}

export function normalizeIfcProposal(value = {}) {
  const proposal = plain(value) ? value : {};
  return {
    changeSetId: text(proposal.changeSetId || proposal.change_set_id || proposal.id),
    changeCount: integer(proposal.changeCount ?? proposal.count),
    elementCount: integer(proposal.elementCount ?? proposal.elements),
    psetName: text(proposal.psetName || proposal.pset_name || proposal.pset),
    propertyName: text(
      proposal.propertyName || proposal.property_name || proposal.property,
    ),
    value: jsonValue(proposal.value),
    unit: text(proposal.unit),
    method: text(proposal.method),
    source: text(proposal.source),
    confidence: text(proposal.confidence),
    marked: Boolean(proposal.marked),
    provenanceChangeSet: text(
      proposal.provenanceChangeSet || proposal.provenance_change_set,
    ),
    warning: text(proposal.warning),
    aiGenerated: proposal.aiGenerated !== false && proposal.ai_generated !== false,
  };
}

function partFromBlock(block, index, blocks, live) {
  if (!plain(block)) return null;
  const streaming = live && index === blocks.length - 1;
  if ((block.kind === "text" || block.kind === "reasoning") && text(block.text)) {
    return {
      type: block.kind,
      text: text(block.text),
      state: streaming ? "streaming" : "done",
    };
  }
  if (block.kind === "tool") {
    const base = {
      type: "dynamic-tool",
      toolName: text(block.name) || "tool",
      toolCallId: text(block.id) || `tool-${index + 1}`,
      input: parseInput(block.args),
    };
    if (block.state === "ok") {
      return { ...base, state: "output-available", output: toolOutput(block) };
    }
    if (block.state === "bad") {
      return { ...base, state: "output-error", errorText: toolError(block) };
    }
    return { ...base, state: "input-available" };
  }
  if (block.kind === "proposal") {
    const data = normalizeIfcProposal(block.proposal);
    return {
      type: IFC_PROPOSAL_PART,
      id: data.changeSetId || `proposal-${index + 1}`,
      data,
    };
  }
  return null;
}

export function uiPartsFromRun(run = {}, { live = false } = {}) {
  const blocks = Array.isArray(run.blocks) ? run.blocks : [];
  return blocks
    .map((block, index) => partFromBlock(block, index, blocks, live))
    .filter(Boolean);
}

export function uiMessageFromRun(
  run = {},
  { id = "assistant-message", live = false, metadata = {} } = {},
) {
  const inputTokens = number(run.usage?.in);
  const outputTokens = number(run.usage?.out);
  return {
    id: text(id) || "assistant-message",
    role: "assistant",
    metadata: compact({
      ...jsonValue(plain(metadata) ? metadata : {}),
      threadId: text(run.threadId) || undefined,
      usage: inputTokens !== null || outputTokens !== null
        ? { inputTokens, outputTokens }
        : undefined,
      error: text(run.error) || undefined,
      finishReason: text(run.finishReason) || undefined,
    }),
    parts: uiPartsFromRun(run, { live }),
  };
}

export function messageText(message) {
  if (!plain(message)) return "";
  if (typeof message.text === "string") return message.text;
  if (!Array.isArray(message.parts)) return "";
  return message.parts
    .filter((part) => part?.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("\n\n");
}

export function uiMessagesToTurns(messages = []) {
  if (!Array.isArray(messages)) return [];
  // Python chat turns are text-only. Files use the separate upload API.
  return messages
    .filter((message) => plain(message) && ["user", "assistant"].includes(message.role))
    .map((message) => ({ role: message.role, text: messageText(message) }))
    .filter((turn) => turn.text);
}

function copySharedOptions(target, options) {
  for (const key of [
    "provider",
    "model",
    "base_url",
    "api_key",
    "tools_supported",
    "vision_supported",
    "temperature",
    "top_p",
    "max_tokens",
  ]) {
    if (options[key] !== undefined) target[key] = options[key];
  }
  return target;
}

export function plainChatRequest(messages = [], options = {}) {
  const body = copySharedOptions({ turns: uiMessagesToTurns(messages) }, options);
  if (typeof options.system === "string") body.system = options.system;
  if (typeof options.tools === "boolean") body.tools = options.tools;
  return body;
}

export function agentChatRequest(messages = [], options = {}) {
  const latestUser = [...(Array.isArray(messages) ? messages : [])]
    .reverse()
    .find((message) => message?.role === "user");
  // With a workflow attached an empty prompt is a real request: the console
  // answers it with the workflow's own task, so no text is invented here.
  const workflow = text(options.workflow);
  const body = copySharedOptions({
    agent: text(options.agent),
    prompt: text(options.prompt) || (workflow ? "" : messageText(latestUser)),
  }, options);
  if (workflow) {
    body.workflow = workflow;
    body.workflow_scope = options.workflow_scope === "selection" ? "selection" : "model";
  }
  if (typeof options.thread_id === "string" && options.thread_id) {
    body.thread_id = options.thread_id;
  }
  if (typeof options.persist_history === "boolean") {
    body.persist_history = options.persist_history;
  }
  if (typeof options.additional_instructions === "string") {
    body.additional_instructions = options.additional_instructions;
  }
  if (Array.isArray(options.attachments)) {
    // These are paths returned by the local upload API, not FileUIPart URLs.
    body.attachments = options.attachments.filter(
      (item) => typeof item === "string" && item.trim(),
    );
  }
  return body;
}

export function decodeIfcSSE(buffer = "") {
  const normalized = text(buffer).replaceAll("\r\n", "\n");
  const frames = normalized.split("\n\n");
  const rest = frames.pop() || "";
  const events = [];
  let done = false;
  for (const frame of frames) {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    if (data === "[DONE]") {
      done = true;
      continue;
    }
    try {
      events.push(JSON.parse(data));
    } catch {
      // A malformed frame must not break the remaining stream.
    }
  }
  return { events, rest, done };
}

function normalizedFinishReason(value, failed) {
  if (failed) return "error";
  if (["end_turn", "stop_sequence"].includes(value)) return "stop";
  if (value === "max_tokens") return "length";
  if (value === "tool_calls") return "tool-calls";
  return FINISH_REASONS.has(value) ? value : "other";
}

function streamToolOutput(event) {
  return compact({
    ok: Boolean(event.ok),
    summary: text(event.summary),
    preview: text(event.preview),
    output: event.output,
    detail: text(event.detail),
    rows: number(event.rows) ?? undefined,
  });
}

export function createIfcStreamAdapter({ messageId = "assistant-message" } = {}) {
  let begun = false;
  let finished = false;
  let active = null;
  let textCount = 0;
  let reasoningCount = 0;
  let finishReason = "stop";
  let failed = false;

  const begin = () => {
    if (begun) return [];
    begun = true;
    return [{ type: "start", messageId }, { type: "start-step" }];
  };

  const closeActive = () => {
    if (!active) return [];
    const chunk = { type: `${active.kind}-end`, id: active.id };
    active = null;
    return [chunk];
  };

  const delta = (kind, value) => {
    const chunks = begin();
    if (active?.kind !== kind) {
      chunks.push(...closeActive());
      const count = kind === "text" ? ++textCount : ++reasoningCount;
      active = { kind, id: `${kind}-${count}` };
      chunks.push({ type: `${kind}-start`, id: active.id });
    }
    chunks.push({ type: `${kind}-delta`, id: active.id, delta: text(value) });
    return chunks;
  };

  const finish = () => {
    if (finished) return [];
    finished = true;
    const chunks = begun ? closeActive() : begin();
    chunks.push(
      { type: "finish-step" },
      { type: "finish", finishReason: normalizedFinishReason(finishReason, failed) },
    );
    return chunks;
  };

  const push = (event = {}) => {
    if (finished || !plain(event)) return [];
    if (event.type === "content") return delta("text", event.text);
    if (event.type === "reasoning") return delta("reasoning", event.text);
    if (event.type === "done") return finish();
    if (event.type === "finish") {
      finishReason = text(event.reason) || finishReason;
      return [];
    }
    if (!["thread", "tool_call", "tool_progress", "tool_result", "proposal", "usage", "error"].includes(
      event.type,
    )) return [];

    const chunks = begin();
    if (event.type === "thread") {
      chunks.push({
        type: IFC_THREAD_PART,
        id: "thread",
        data: { threadId: text(event.id), agent: text(event.agent) },
        transient: true,
      });
    } else if (event.type === "tool_call") {
      chunks.push(...closeActive(), {
        type: "tool-input-available",
        toolCallId: text(event.id),
        toolName: text(event.name) || "tool",
        input: parseInput(event.arguments),
        dynamic: true,
      });
    } else if (event.type === "tool_progress") {
      chunks.push({
        type: IFC_TOOL_PROGRESS_PART,
        id: text(event.id) || "tool-progress",
        data: {
          toolCallId: text(event.id),
          toolName: text(event.name),
          done: number(event.done) ?? 0,
          total: number(event.total),
          note: text(event.note),
          elapsedSeconds: number(event.elapsed) ?? 0,
        },
        transient: true,
      });
    } else if (event.type === "tool_result") {
      chunks.push(...closeActive());
      if (event.ok) {
        chunks.push({
          type: "tool-output-available",
          toolCallId: text(event.id),
          output: streamToolOutput(event),
          dynamic: true,
        });
      } else {
        chunks.push({
          type: "tool-output-error",
          toolCallId: text(event.id),
          errorText: [text(event.summary), text(event.detail)].filter(Boolean).join(": ")
            || "Tool failed",
          dynamic: true,
        });
      }
    } else if (event.type === "proposal") {
      const data = normalizeIfcProposal(event);
      chunks.push(...closeActive(), {
        type: IFC_PROPOSAL_PART,
        id: data.changeSetId || "proposal",
        data,
      });
    } else if (event.type === "usage") {
      chunks.push({
        type: "message-metadata",
        messageMetadata: {
          usage: { inputTokens: number(event.in), outputTokens: number(event.out) },
        },
      });
    } else if (event.type === "error") {
      failed = true;
      chunks.push(...closeActive(), { type: "error", errorText: text(event.text) });
    } else {
      return [];
    }
    return chunks;
  };

  return { push, finish };
}

export function ifcSSEToUIMessageStream(source, options = {}) {
  if (!source || typeof source.getReader !== "function") {
    throw new TypeError("source must be a readable byte stream");
  }
  const reader = source.getReader();
  const decoder = new TextDecoder();
  const adapter = createIfcStreamAdapter(options);
  let buffer = "";
  let queue = [];
  let sourceDone = false;

  return new ReadableStream({
    async pull(controller) {
      try {
        while (!queue.length && !sourceDone) {
          const next = await reader.read();
          if (next.done) {
            buffer += decoder.decode();
            const final = decodeIfcSSE(`${buffer}\n\n`);
            for (const event of final.events) queue.push(...adapter.push(event));
            queue.push(...adapter.finish());
            sourceDone = true;
            break;
          }
          buffer += decoder.decode(next.value, { stream: true });
          const decoded = decodeIfcSSE(buffer);
          buffer = decoded.rest;
          for (const event of decoded.events) queue.push(...adapter.push(event));
          const terminal = decoded.done || decoded.events.some((event) => event.type === "done");
          if (terminal) {
            queue.push(...adapter.finish());
            sourceDone = true;
            await reader.cancel("stream complete");
          }
        }
        if (queue.length) controller.enqueue(queue.shift());
        if (sourceDone && !queue.length) controller.close();
      } catch (error) {
        controller.error(error);
      }
    },
    cancel(reason) {
      sourceDone = true;
      return reader.cancel(reason);
    },
  });
}

async function responseError(response) {
  try {
    const payload = await response.json();
    return text(payload.hint || payload.error) || `HTTP ${response.status}`;
  } catch {
    return `HTTP ${response.status}`;
  }
}

async function resolved(value) {
  return typeof value === "function" ? value() : value;
}

function jsonObject(value) {
  if (!plain(value)) return undefined;
  try {
    const copy = JSON.parse(JSON.stringify(value));
    return plain(copy) ? copy : undefined;
  } catch {
    return undefined;
  }
}

function transportRequestFields({ chatId, trigger, messageId, metadata }) {
  return compact({
    chatId: typeof chatId === "string" && chatId ? chatId : undefined,
    trigger: CHAT_TRIGGERS.has(trigger) ? trigger : undefined,
    messageId: typeof messageId === "string" && messageId ? messageId : undefined,
    metadata: jsonObject(metadata),
  });
}

export function createIfcChatTransport({
  agent = "",
  endpoint = "",
  token = "",
  context = {},
  fetcher = globalThis.fetch?.bind(globalThis),
} = {}) {
  if (typeof fetcher !== "function") throw new TypeError("fetch is unavailable");
  return {
    async sendMessages({
      chatId,
      trigger,
      messageId,
      messages = [],
      abortSignal,
      headers,
      body = {},
      metadata,
    } = {}) {
      const configured = await resolved(context);
      const requestOptions = {
        ...(plain(configured) ? configured : {}),
        ...(plain(body) ? body : {}),
      };
      if (agent && !requestOptions.agent) requestOptions.agent = agent;
      const requestBody = requestOptions.agent
        ? agentChatRequest(messages, requestOptions)
        : plainChatRequest(messages, requestOptions);
      Object.assign(requestBody, transportRequestFields({
        chatId,
        trigger,
        messageId,
        metadata,
      }));
      const targetEndpoint = endpoint || (
        requestOptions.agent ? "/api/agents/stream" : "/api/chat/stream"
      );
      const requestHeaders = new Headers(headers || {});
      requestHeaders.set("Content-Type", "application/json");
      const currentToken = text(await resolved(token));
      if (currentToken && !requestHeaders.has("Authorization")) {
        requestHeaders.set("Authorization", `Bearer ${currentToken}`);
      }
      const response = await fetcher(targetEndpoint, {
        method: "POST",
        headers: requestHeaders,
        body: JSON.stringify(requestBody),
        signal: abortSignal,
      });
      if (!response.ok) throw new Error(await responseError(response));
      if (!response.body) throw new Error("Chat response has no stream");
      return ifcSSEToUIMessageStream(response.body);
    },
    async reconnectToStream() {
      return null;
    },
  };
}
