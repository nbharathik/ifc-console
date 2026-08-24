/* Local conversation archive for the agent panel.
 *
 * This module stores transcripts and opaque agent thread ids, never provider
 * credentials. Keeping it separate makes the same history/export component
 * reusable by future agent panels without coupling it to streaming or IFC UI.
 */

const STORAGE_VERSION = 1;
const DEFAULT_LIMIT = 40;
const TURN_LIMIT = 80;
const TEXT_LIMIT = 100_000;

const plain = (value) => value !== null && typeof value === "object" && !Array.isArray(value);

function cleanTurn(value) {
  if (!plain(value) || !["user", "assistant"].includes(value.role)) return null;
  if (typeof value.text !== "string" || !value.text.trim()) return null;
  const attachments = Array.isArray(value.attachments)
    ? value.attachments.slice(0, 8).map((item) => ({
        name: String(item?.name || "attachment").slice(0, 300),
        path: String(item?.path || "").slice(0, 2000),
        media: item?.media === "image" ? "image" : "document",
      }))
    : [];
  return { role: value.role, text: value.text.slice(0, TEXT_LIMIT), attachments };
}

function cleanRecord(value) {
  if (!plain(value) || typeof value.id !== "string" || !value.id) return null;
  const turns = Array.isArray(value.turns)
    ? value.turns.slice(-TURN_LIMIT).map(cleanTurn).filter(Boolean)
    : [];
  return {
    id: value.id.slice(0, 100),
    agent: typeof value.agent === "string" ? value.agent.slice(0, 100) : "",
    agent_title: typeof value.agent_title === "string" ? value.agent_title.slice(0, 100) : "Chat",
    title: typeof value.title === "string" ? value.title.slice(0, 100) : "New conversation",
    updated_at: Number.isFinite(value.updated_at) ? value.updated_at : Date.now(),
    thread_id: typeof value.thread_id === "string" ? value.thread_id.slice(0, 100) : "",
    turns,
  };
}

export function conversationId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `conversation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export class ChatHistoryStore {
  constructor(storage, name = "ifc-console-chat-history-v2", limit = DEFAULT_LIMIT) {
    this.storage = storage;
    this.name = name;
    this.limit = limit;
  }

  list() {
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

  get(id) {
    return this.list().find((record) => record.id === id) || null;
  }

  latest(agent) {
    return this.list().find((record) => record.agent === agent) || null;
  }

  save(record) {
    const safe = cleanRecord(record);
    if (!safe || !safe.turns.length) return null;
    const items = [safe, ...this.list().filter((item) => item.id !== safe.id)]
      .slice(0, this.limit);
    try {
      this.storage.setItem(this.name, JSON.stringify({ version: STORAGE_VERSION, items }));
    } catch {
      return null;
    }
    return safe;
  }

  remove(id) {
    const current = this.list();
    const removed = current.find((item) => item.id === id) || null;
    const items = current.filter((item) => item.id !== id);
    try {
      this.storage.setItem(this.name, JSON.stringify({ version: STORAGE_VERSION, items }));
    } catch {
      return null;
    }
    return removed;
  }

  clear() {
    try {
      this.storage.removeItem(this.name);
      return true;
    } catch {
      return false;
    }
  }
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
    lines.push(turn.text, "");
  }
  return lines.join("\n");
}
