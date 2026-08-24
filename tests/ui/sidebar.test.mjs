import assert from "node:assert/strict";
import { test } from "node:test";

import {
  agentGroups,
  conversationGroups,
  initials,
  relativeDay,
  sidebarModel,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/chat_sidebar.js";

const AGENTS = [
  { name: "general", title: "General assistant", kind: "built-in" },
  { name: "measurement", title: "Measurement", kind: "built-in" },
  { name: "custom-envelope", title: "Envelope compliance", kind: "custom" },
];

const DAY = 24 * 60 * 60 * 1000;
const NOON = new Date(2026, 7, 23, 12, 0, 0).getTime();

test("built-in assistants and the user's own are separate groups", () => {
  const groups = agentGroups(AGENTS, "measurement");
  assert.deepEqual(groups.map((group) => group.id), ["builtin", "custom"]);
  assert.equal(groups[0].agents.length, 2);
  assert.equal(groups[1].agents[0].name, "custom-envelope");
});

test("only the user's own assistants can be deleted", () => {
  const [builtin, custom] = agentGroups(AGENTS);
  assert.equal(builtin.agents.every((agent) => agent.deletable === false), true);
  assert.equal(custom.agents[0].deletable, true);
});

test("the selected assistant is the only active one", () => {
  const groups = agentGroups(AGENTS, "measurement");
  const active = groups.flatMap((group) => group.agents).filter((agent) => agent.active);
  assert.deepEqual(active.map((agent) => agent.name), ["measurement"]);
});

test("initials survive one-word and empty titles", () => {
  assert.equal(initials("General assistant"), "GA");
  assert.equal(initials("Measurement"), "M");
  assert.equal(initials(""), "?");
  assert.equal(initials(undefined), "?");
});

test("conversations are labelled the way people talk about them", () => {
  assert.equal(relativeDay(NOON, NOON), "Today");
  assert.equal(relativeDay(NOON - DAY, NOON), "Yesterday");
  assert.equal(relativeDay(NOON - 3 * DAY, NOON), "This week");
  assert.equal(relativeDay(NOON - 20 * DAY, NOON), "This month");
  assert.equal(relativeDay(NOON - 200 * DAY, NOON), "Earlier");
  assert.equal(relativeDay("nonsense", NOON), "Earlier");
});

test("conversation buckets run newest first, and so do the chats inside them", () => {
  const records = [
    { id: "old", updated_at: NOON - 200 * DAY, turns: [] },
    { id: "now", updated_at: NOON, turns: [] },
    { id: "earlier-today", updated_at: NOON - 60_000, turns: [] },
    { id: "yesterday", updated_at: NOON - DAY, turns: [] },
  ];
  const groups = conversationGroups(records, { now: NOON });
  assert.deepEqual(groups.map((group) => group.label), ["Today", "Yesterday", "Earlier"]);
  assert.deepEqual(groups[0].records.map((row) => row.id), ["now", "earlier-today"]);
});

test("the archive listing is capped", () => {
  const records = Array.from({ length: 60 }, (_, index) => ({
    id: `c-${index}`,
    updated_at: NOON - index * 1000,
    turns: [],
  }));
  const total = conversationGroups(records, { now: NOON }).reduce(
    (sum, group) => sum + group.records.length,
    0
  );
  assert.equal(total, 40);
});

test("the whole sidebar comes back in one shape", () => {
  const model = sidebarModel({
    agents: AGENTS,
    records: [{ id: "c1", updated_at: NOON, turns: [] }],
    currentAgent: "general",
    currentConversationId: "c1",
    now: NOON,
  });
  assert.equal(model.agentGroups.length, 2);
  assert.equal(model.conversationGroups[0].records[0].active, true);
  assert.equal(model.hasCustom, true);
  assert.equal(model.conversationCount, 1);
});

test("an empty panel still returns a usable model", () => {
  const model = sidebarModel();
  assert.deepEqual(model.agentGroups, []);
  assert.deepEqual(model.conversationGroups, []);
  assert.equal(model.hasCustom, false);
});
