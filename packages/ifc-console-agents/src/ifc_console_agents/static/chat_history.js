/* Local conversation archive for the agent panel.
 *
 * This module stores transcripts and opaque agent thread ids, never provider
 * credentials. Keeping it separate makes the same history/export component
 * reusable by future agent panels without coupling it to streaming or IFC UI.
 */

const STORAGE_VERSION = 1;
export const HISTORY_STORAGE_NAME = "ifc-console-chat-history-v3";
export const LEGACY_HISTORY_STORAGE_NAMES = Object.freeze([
  "ifc-console-chat-history-v1",
  "ifc-console-chat-history-v2",
]);
const DEFAULT_LIMIT = 40;
const TURN_LIMIT = 80;
const TEXT_LIMIT = 100_000;
const BLOCK_LIMIT = 6000;
const BLOCK_COUNT = 60;
const REQUEST_TURN_LIMIT = 60;
const REQUEST_TEXT_LIMIT = 180_000;
const REQUEST_TURN_TEXT_LIMIT = 100_000;

const plain = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

// A stored block is what the panel drew, not what the provider sent, so a
// reopened conversation still shows which tools ran and what they returned.
function cleanBlock(value) {
  if (!plain(value)) return null;
  if (value.kind === "tool") {
    return {
      kind: "tool",
      name: String(value.name || "tool").slice(0, 120),
      stage: Number.isInteger(value.stage) ? value.stage : -1,
      state: ["ok", "bad", "running"].includes(value.state) ? value.state : "ok",
      summary: String(value.summary || "").slice(0, 300),
      ms: Number.isFinite(value.ms) ? value.ms : null,
      args: String(value.args || "").slice(0, BLOCK_LIMIT),
      preview: String(value.preview || "").slice(0, BLOCK_LIMIT),
      detail: String(value.detail || "").slice(0, 400),
    };
  }
  if (value.kind === "proposal") {
    return plain(value.proposal) ? { kind: "proposal", proposal: value.proposal } : null;
  }
  // Who approved which write is the audit trail of an AI-authored change, so
  // it outlives the run that asked. The request id is deliberately not kept:
  // it answers a future the console has already resolved.
  if (value.kind === "approval") {
    return {
      kind: "approval",
      name: String(value.name || "tool").slice(0, 120),
      capabilities: Array.isArray(value.capabilities)
        ? value.capabilities.slice(0, 12).map((item) => String(item).slice(0, 80))
        : [],
      state: ["approved", "denied", "waiting"].includes(value.state) ? value.state : "denied",
      decidedBy: String(value.decidedBy || "").slice(0, 120),
      reason: String(value.reason || "").slice(0, 400),
      args: String(value.args || "").slice(0, BLOCK_LIMIT),
    };
  }
  if (value.kind !== "text" && value.kind !== "reasoning") return null;
  if (typeof value.text !== "string" || !value.text.trim()) return null;
  return { kind: value.kind, text: value.text.slice(0, BLOCK_LIMIT) };
}

function cleanTurn(value) {
  if (!plain(value) || !["user", "assistant"].includes(value.role)) return null;
  const text = typeof value.text === "string" ? value.text.slice(0, TEXT_LIMIT) : "";
  const attachments = Array.isArray(value.attachments)
    ? value.attachments.slice(0, 8).map((item) => ({
        name: String(item?.name || "attachment").slice(0, 300),
        path: String(item?.path || "").slice(0, 2000),
        media: item?.media === "image" ? "image" : "document",
      }))
    : [];
  const blocks = Array.isArray(value.blocks)
    ? value.blocks.slice(-BLOCK_COUNT).map(cleanBlock).filter(Boolean)
    : [];
  const status = ["complete", "stopped", "error"].includes(value.status)
    ? value.status
    : "complete";
  const error = typeof value.error === "string" ? value.error.slice(0, 1000) : "";
  if (!text.trim() && !blocks.length && !error) return null;
  const turn = { role: value.role, text, attachments, blocks, status, error };
  // A turn that started a workflow keeps the name it ran under, and the raw
  // typed prompt when it differs from the line shown for it.
  const workflow = cleanWorkflow(value.workflow);
  if (workflow) turn.workflow = workflow;
  if (typeof value.prompt === "string" && value.prompt !== text) {
    turn.prompt = value.prompt.slice(0, TEXT_LIMIT);
  }
  return turn;
}

function cleanWorkflow(value) {
  if (!plain(value) || typeof value.name !== "string" || !value.name) return null;
  return {
    name: value.name.slice(0, 64),
    title: String(value.title || value.name).slice(0, 100),
    scope: value.scope === "selection" ? "selection" : "model",
  };
}

function cleanRecord(value) {
  if (!plain(value) || typeof value.id !== "string" || !value.id) return null;
  const turns = Array.isArray(value.turns)
    ? value.turns.slice(-TURN_LIMIT).map(cleanTurn).filter(Boolean)
    : [];
  const record = {
    id: value.id.slice(0, 100),
    scope: typeof value.scope === "string" ? value.scope.slice(0, 160) : "",
    agent: typeof value.agent === "string" ? value.agent.slice(0, 100) : "",
    agent_title: typeof value.agent_title === "string" ? value.agent_title.slice(0, 100) : "Chat",
    title: typeof value.title === "string" ? value.title.slice(0, 100) : "New conversation",
    updated_at: Number.isFinite(value.updated_at) ? value.updated_at : Date.now(),
    thread_id: typeof value.thread_id === "string" ? value.thread_id.slice(0, 100) : "",
    turns,
  };
  // The workflow a conversation stands on is part of its configuration: a
  // reopened conversation must send it again or the console forks the thread.
  const workflow = cleanWorkflow(value.workflow);
  if (workflow) record.workflow = workflow;
  return record;
}

export function conversationId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `conversation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export class ChatHistoryStore {
  constructor(storage, name = HISTORY_STORAGE_NAME, limit = DEFAULT_LIMIT, scope = "") {
    this.storage = storage;
    this.name = name;
    this.limit = limit;
    this.scope = String(scope || "").slice(0, 160);
  }

  setScope(scope) {
    this.scope = String(scope || "").slice(0, 160);
    return this;
  }

  list() {
    return this.all().filter((record) => record.scope === this.scope);
  }

  all() {
    return this._all();
  }

  get(id) {
    return this.list().find((record) => record.id === id) || null;
  }

  latest(agent) {
    return this.list().find((record) => record.agent === agent) || null;
  }

  save(record) {
    const safe = cleanRecord(record);
    if (!safe || !safe.turns.length) return null;
    safe.scope = this.scope;
    const payload = this._all();
    const items = [safe, ...payload.filter((item) => item.id !== safe.id)]
      .slice(0, this.limit);
    try {
      this.storage.setItem(this.name, JSON.stringify({ version: STORAGE_VERSION, items }));
    } catch {
      return null;
    }
    return safe;
  }

  remove(id) {
    const current = this._all();
    const removed = current.find((item) => item.id === id) || null;
    const items = current.filter((item) => item.id !== id);
    try {
      this.storage.setItem(this.name, JSON.stringify({ version: STORAGE_VERSION, items }));
    } catch {
      return null;
    }
    return removed;
  }

  _all() {
    try {
      const payload = JSON.parse(this.storage.getItem(this.name) || "{}");
      if (!plain(payload) || payload.version !== STORAGE_VERSION || !Array.isArray(payload.items)) {
        return [];
      }
      return payload.items.map(cleanRecord).filter(Boolean)
        .sort((left, right) => right.updated_at - left.updated_at);
    } catch {
      return [];
    }
  }

  clear({ includeLegacy = false } = {}) {
    try {
      this.storage.removeItem(this.name);
      if (includeLegacy) this.discardLegacy();
      return true;
    } catch {
      return false;
    }
  }

  discardLegacy() {
    let complete = true;
    for (const name of LEGACY_HISTORY_STORAGE_NAMES) {
      if (name === this.name) continue;
      try {
        this.storage.removeItem(name);
      } catch {
        complete = false;
      }
    }
    return complete;
  }
}

// Plain chat sends its visible transcript on every request. Bound that payload
// independently of the local archive so a long-lived tab cannot eventually
// exceed the server's transcript limit. The newest turns win; a single very
// large message is clipped with an explicit marker instead of disappearing.
export function boundedChatTurns(
  source,
  {
    maxTurns = REQUEST_TURN_LIMIT,
    maxChars = REQUEST_TEXT_LIMIT,
    maxTurnChars = REQUEST_TURN_TEXT_LIMIT,
  } = {},
) {
  if (!Array.isArray(source) || maxTurns < 1 || maxChars < 1 || maxTurnChars < 1) return [];
  const clip = (value, limit) => {
    if (value.length <= limit) return value;
    const marker = "\n\n[message truncated by the chat panel]";
    const room = Math.max(0, limit - marker.length);
    return value.slice(0, room) + marker.slice(0, limit - room);
  };
  const selected = [];
  let remaining = maxChars;
  for (let index = source.length - 1; index >= 0 && selected.length < maxTurns; index -= 1) {
    const turn = source[index];
    if (!plain(turn) || !["user", "assistant"].includes(turn.role)) continue;
    const text = clip(typeof turn.text === "string" ? turn.text : "", maxTurnChars);
    if (!text && !selected.length) continue;
    if (text.length <= remaining) {
      selected.push({ role: turn.role, text });
      remaining -= text.length;
      continue;
    }
    if (!selected.length) {
      selected.push({ role: turn.role, text: clip(text, remaining) });
    }
    break;
  }
  selected.reverse();
  while (selected[0]?.role === "assistant" && selected.some((turn) => turn.role === "user")) {
    selected.shift();
  }
  return selected;
}

/* What a person is actually being asked to allow.
 *
 * The reviewer used to approve an IFC write by reading the tool's raw JSON.
 * These are the fields that decide whether a change is right, in the order a
 * reader wants them; anything unrecognised stays in the arguments fold.
 * Pure, so the live card and the exported transcript say the same thing.
 */
const APPROVAL_FIELDS = Object.freeze([
  ["Intent", ["description", "intent", "purpose"]],
  ["Property", ["property", "property_name", "name", "quantity"]],
  ["Value", ["value", "measured_value", "new_value"]],
  ["Unit", ["unit", "units"]],
  ["Property set", ["pset", "pset_name", "property_set"]],
  ["Classification", ["classification", "system", "reference"]],
  ["Method", ["method"]],
  ["Confidence", ["confidence"]],
]);

const APPROVAL_TARGET_KEYS = Object.freeze([
  "global_ids",
  "globalids",
  "guids",
  "elements",
  "element_ids",
]);

function approvalTarget(args) {
  for (const key of APPROVAL_TARGET_KEYS) {
    const value = args[key];
    if (Array.isArray(value)) return `${value.length} element${value.length === 1 ? "" : "s"}`;
    if (typeof value === "string" && value.trim()) return value.trim().slice(0, 120);
  }
  if (Number.isFinite(args.element_count)) {
    return `${args.element_count} element${args.element_count === 1 ? "" : "s"}`;
  }
  const selector = args.selector ?? args.query;
  return typeof selector === "string" && selector.trim() ? selector.trim().slice(0, 120) : "";
}

const scalar = (value) =>
  value !== null && value !== undefined && value !== "" && !plain(value) && !Array.isArray(value);

function approvalPayload(block) {
  const source = block?.args;
  if (plain(source)) return source;
  try {
    const parsed = JSON.parse(String(source || "{}"));
    return plain(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** The compact argument view used inside an approval card.
 *
 * Executable code is the part a reviewer needs to inspect, so show it as code
 * instead of burying it in an escaped JSON string. Other tools retain their
 * complete, pretty-printed argument object.
 */
export function approvalArgumentPreview(block) {
  const raw = block?.args;
  const args = approvalPayload(block);
  if (typeof args.code === "string" && args.code.trim()) {
    return { label: "Code", text: args.code, code: true };
  }
  let text = "";
  try {
    text = plain(raw) ? JSON.stringify(raw, null, 1) : String(raw || "").trim();
    if (text) text = JSON.stringify(JSON.parse(text), null, 1);
  } catch {
    // An unreadable payload is still useful evidence; show it unchanged.
  }
  return { label: "Arguments", text, code: false };
}

export function approvalDigest(block) {
  const name = String(block?.name || "tool");
  const args = approvalPayload(block);
  const facts = [];
  for (const [label, keys] of APPROVAL_FIELDS) {
    const key = keys.find((candidate) => scalar(args[candidate]));
    if (!key) continue;
    facts.push({ label, value: String(args[key]).slice(0, 240) });
  }
  const target = approvalTarget(args);
  if (target) facts.push({ label: "Targets", value: target });
  const property = [args.property, args.property_name, args.quantity].find(scalar);
  const value = [args.value, args.measured_value, args.new_value].find(scalar);
  const unit = scalar(args.unit) ? ` ${args.unit}` : "";
  const headline = property !== undefined && value !== undefined
    ? `Set ${property} to ${value}${unit}${target ? ` on ${target}` : ""}`
    : `Run ${name}${target ? ` on ${target}` : ""}`;
  return { name, headline, facts };
}

/* The last few turns of the outgoing conversation, as plain text.
 *
 * Switching assistant used to throw the transcript away, so escalating a
 * question to a specialist meant retyping its whole setup. The slice goes into
 * the composer where it can be read and edited, never silently into a prompt.
 */
export function carrySlice(source, { count = 3, chars = 400, label = "the previous assistant" } = {}) {
  const rows = (Array.isArray(source) ? source : [])
    .filter((turn) =>
      plain(turn)
      && ["user", "assistant"].includes(turn.role)
      && typeof turn.text === "string"
      && turn.text.trim())
    .slice(-Math.max(1, count));
  if (!rows.length) return "";
  const lines = [`Context carried from ${label}:`];
  for (const turn of rows) {
    const text = turn.text.trim().replace(/\s+/g, " ");
    const clipped = text.length > chars ? `${text.slice(0, chars - 3)}...` : text;
    lines.push(`${turn.role === "user" ? "Asked" : "Answered"}: ${clipped}`);
  }
  return lines.join("\n");
}

export function transcriptMarkdown(record, model = "") {
  const safe = cleanRecord(record);
  if (!safe) return "";
  const lines = [
    `# ${safe.title}`,
    "",
    `- Assistant: ${safe.agent_title || "Chat"}`,
    `- Exported: ${new Date().toISOString()}`,
  ];
  if (model) lines.push(`- Model: ${model}`);
  lines.push("");
  for (const turn of safe.turns) {
    lines.push(`## ${turn.role === "user" ? "You" : safe.agent_title || "Assistant"}`, "");
    if (turn.attachments?.length) {
      lines.push(`Attachments: ${turn.attachments.map((item) => `\`${item.name}\``).join(", ")}`, "");
    }
    if (turn.text) lines.push(turn.text, "");
    else if (turn.blocks.length) lines.push("_[Tool and evidence output; see the panel transcript.]_", "");
    // An exported transcript is the audit record, so it names every gated call
    // and how it was answered, not just what the assistant wrote afterwards.
    for (const block of turn.blocks) {
      if (block.kind !== "approval") continue;
      const digest = approvalDigest(block);
      const who = block.decidedBy ? ` (${block.decidedBy})` : "";
      lines.push(`_Approval: \`${block.name}\` ${block.state}${who}. ${digest.headline}._`, "");
    }
    if (turn.error) lines.push(`_Error: ${turn.error}_`, "");
  }
  return lines.join("\n");
}
