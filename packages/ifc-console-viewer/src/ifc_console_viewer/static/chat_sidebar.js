/* The sidebar model: which assistants exist, and what has been asked of them.
 *
 * Creating an agent is the first thing in the list because building one is the
 * point of the panel, not a setting buried behind a gear. Conversations are
 * grouped by recency the way a person thinks about them ("today", "yesterday")
 * rather than by raw timestamp. Pure functions only, so both are testable.
 */

const DAY = 24 * 60 * 60 * 1000;

/* The no-agent surface, listed like any other so it can be returned to. It is
 * not a server pack, so the panel supplies it rather than /api/agents. */
export const PLAIN_CHAT = Object.freeze({
  name: "",
  title: "Plain chat",
  description: "Direct chat over the open model, with no agent around it.",
  kind: "built-in",
});

/** Built-in assistants first, then the user's own, each in a labelled group. */
export function agentGroups(agents, currentAgent = "") {
  const rows = Array.isArray(agents) ? agents : [];
  const builtin = rows.filter((agent) => agent.kind !== "custom");
  const custom = rows.filter((agent) => agent.kind === "custom");
  const decorate = (agent) => ({
    ...agent,
    active: agent.name === currentAgent,
    initials: initials(agent.title),
    deletable: agent.kind === "custom",
  });
  const groups = [];
  if (builtin.length) {
    groups.push({ id: "builtin", label: "Assistants", agents: builtin.map(decorate) });
  }
  if (custom.length) {
    groups.push({ id: "custom", label: "Yours", agents: custom.map(decorate) });
  }
  return groups;
}

export function initials(title) {
  const words = String(title || "?").trim().split(/\s+/).filter(Boolean);
  if (!words.length) return "?";
  const first = words[0][0] || "?";
  const second = words.length > 1 ? words[1][0] : "";
  return (first + second).toUpperCase();
}

export function relativeDay(timestamp, now = Date.now()) {
  const value = Number(timestamp);
  if (!Number.isFinite(value)) return "Earlier";
  const startOfToday = new Date(now);
  startOfToday.setHours(0, 0, 0, 0);
  const start = startOfToday.getTime();
  if (value >= start) return "Today";
  if (value >= start - DAY) return "Yesterday";
  if (value >= start - 7 * DAY) return "This week";
  if (value >= start - 30 * DAY) return "This month";
  return "Earlier";
}

const ORDER = ["Today", "Yesterday", "This week", "This month", "Earlier"];

/** Conversations bucketed by recency, newest bucket first, newest chat first. */
export function conversationGroups(records, { now = Date.now(), limit = 40 } = {}) {
  const rows = (Array.isArray(records) ? records : [])
    .slice()
    .sort((left, right) => (right.updated_at || 0) - (left.updated_at || 0))
    .slice(0, limit);
  const buckets = new Map();
  for (const record of rows) {
    const label = relativeDay(record.updated_at, now);
    if (!buckets.has(label)) buckets.set(label, []);
    buckets.get(label).push(record);
  }
  return ORDER.filter((label) => buckets.has(label)).map((label) => ({
    label,
    records: buckets.get(label),
  }));
}

/** The whole sidebar, derived once. */
export function sidebarModel({
  agents = [],
  records = [],
  currentAgent = "",
  currentConversationId = "",
  now = Date.now(),
} = {}) {
  const groups = conversationGroups(records, { now });
  return {
    agentGroups: agentGroups(agents, currentAgent),
    conversationGroups: groups.map((group) => ({
      ...group,
      records: group.records.map((record) => ({
        ...record,
        active: record.id === currentConversationId,
      })),
    })),
    conversationCount: records.length,
    hasCustom: agents.some((agent) => agent.kind === "custom"),
  };
}
