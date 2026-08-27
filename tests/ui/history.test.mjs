import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ChatHistoryStore,
  HISTORY_STORAGE_NAME,
  approvalDigest,
  boundedChatTurns,
  carrySlice,
  conversationId,
  transcriptMarkdown,
} from "../../packages/ifc-console-viewer/src/ifc_console_viewer/static/chat_history.js";

/** The smallest thing that behaves like localStorage. */
function storage(initial = {}) {
  const map = new Map(Object.entries(initial));
  return {
    map,
    getItem: (key) => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    removeItem: (key) => map.delete(key),
  };
}

const record = (overrides = {}) => ({
  id: "c-1",
  agent: "measurement",
  agent_title: "Measurement",
  title: "Measure the walls",
  updated_at: 1000,
  thread_id: "panel-abc",
  turns: [
    { role: "user", text: "measure the walls", attachments: [] },
    { role: "assistant", text: "240 mm", attachments: [] },
  ],
  ...overrides,
});

test("a saved conversation round-trips", () => {
  const store = new ChatHistoryStore(storage());
  store.save(record());
  const found = store.get("c-1");
  assert.equal(found.title, "Measure the walls");
  assert.equal(found.turns.length, 2);
  assert.equal(store.latest("measurement").id, "c-1");
});

test("conversations come back newest first", () => {
  const store = new ChatHistoryStore(storage());
  store.save(record({ id: "old", updated_at: 1 }));
  store.save(record({ id: "new", updated_at: 2 }));
  assert.deepEqual(store.list().map((row) => row.id), ["new", "old"]);
});

test("a conversation with no turns is not stored", () => {
  const store = new ChatHistoryStore(storage());
  assert.equal(store.save(record({ turns: [] })), null);
  assert.deepEqual(store.list(), []);
});

test("the archive is capped so it cannot grow without bound", () => {
  const store = new ChatHistoryStore(storage(), "test-history", 3);
  for (let index = 0; index < 6; index++) {
    store.save(record({ id: `c-${index}`, updated_at: index }));
  }
  assert.equal(store.list().length, 3);
});

test("corrupt storage degrades to an empty archive instead of throwing", () => {
  const store = new ChatHistoryStore(storage({ [HISTORY_STORAGE_NAME]: "{not json" }));
  assert.deepEqual(store.list(), []);
});

test("a storage write failure is survivable", () => {
  const broken = storage();
  broken.setItem = () => {
    throw new Error("quota");
  };
  const store = new ChatHistoryStore(broken);
  assert.equal(store.save(record()), null);
});

test("unknown roles and non-string text are dropped, not stored", () => {
  const store = new ChatHistoryStore(storage());
  store.save(
    record({
      turns: [
        { role: "system", text: "ignore me" },
        { role: "user", text: "" },
        { role: "user", text: "kept" },
      ],
    })
  );
  assert.deepEqual(store.get("c-1").turns.map((turn) => turn.text), ["kept"]);
});

test("removing and clearing work", () => {
  const store = new ChatHistoryStore(storage());
  store.save(record());
  assert.equal(store.remove("c-1").id, "c-1");
  assert.deepEqual(store.list(), []);
  store.save(record());
  store.clear();
  assert.deepEqual(store.list(), []);
});

test("the markdown export carries the transcript and never a credential", () => {
  const text = transcriptMarkdown(record(), "rehearsal-tools");
  assert.match(text, /# Measure the walls/);
  assert.match(text, /- Model: rehearsal-tools/);
  assert.match(text, /## You/);
  assert.match(text, /240 mm/);
  assert.equal(/api[_-]?key|password|bearer/i.test(text), false);
});

test("attachments are listed in the export", () => {
  const text = transcriptMarkdown(
    record({
      turns: [
        {
          role: "user",
          text: "check this",
          attachments: [{ name: "manual.pdf", path: "refs/manual.pdf", media: "document" }],
        },
      ],
    })
  );
  assert.match(text, /Attachments: `manual\.pdf`/);
});

test("conversation ids are unique", () => {
  assert.notEqual(conversationId(), conversationId());
});

test("a stored turn keeps the tool trail it was drawn from", () => {
  const store = new ChatHistoryStore(storage());
  store.save({
    id: "c1",
    agent: "general",
    title: "Walls",
    updated_at: 1,
    turns: [
      { role: "user", text: "how many walls?" },
      {
        role: "assistant",
        text: "Three.",
        blocks: [
          { kind: "text", text: "Looking." },
          {
            kind: "tool",
            name: "query_elements",
            stage: 0,
            state: "ok",
            summary: "3 row(s)",
            args: '{"ifc_class":"IfcWall"}',
            preview: '{"rows":[]}',
          },
          { kind: "text", text: "Three." },
          { kind: "nonsense" },
        ],
      },
    ],
  });
  const blocks = store.get("c1").turns[1].blocks;
  assert.deepEqual(blocks.map((block) => block.kind), ["text", "tool", "text"]);
  assert.equal(blocks[1].name, "query_elements");
  assert.equal(blocks[1].state, "ok");
});

test("a turn saved before blocks existed still loads", () => {
  const store = new ChatHistoryStore(storage());
  store.save({
    id: "c2",
    agent: "",
    title: "Old",
    updated_at: 1,
    turns: [{ role: "user", text: "hi" }, { role: "assistant", text: "hello" }],
  });
  assert.deepEqual(store.get("c2").turns[1].blocks, []);
});

test("history is isolated by open-model scope without losing other scopes", () => {
  const disk = storage();
  const store = new ChatHistoryStore(disk).setScope("model:a:fingerprint-a");
  store.save(record({ id: "model-a" }));
  store.setScope("model:b:fingerprint-b");
  store.save(record({ id: "model-b" }));
  assert.deepEqual(store.list().map((row) => row.id), ["model-b"]);
  assert.deepEqual(store.all().map((row) => row.id), ["model-b", "model-a"]);
  store.setScope("model:a:fingerprint-a");
  assert.deepEqual(store.list().map((row) => row.id), ["model-a"]);
});

test("the clean-start reset removes current and legacy archives", () => {
  const disk = storage({
    "ifc-console-chat-history-v1": "old",
    "ifc-console-chat-history-v2": "older",
  });
  const store = new ChatHistoryStore(disk);
  store.save(record());
  assert.equal(store.clear({ includeLegacy: true }), true);
  assert.equal(disk.map.has(HISTORY_STORAGE_NAME), false);
  assert.equal(disk.map.has("ifc-console-chat-history-v1"), false);
  assert.equal(disk.map.has("ifc-console-chat-history-v2"), false);
});

test("tool-only, stopped, and failed assistant turns survive a reload", () => {
  const store = new ChatHistoryStore(storage());
  store.save(record({
    turns: [
      { role: "user", text: "inspect it" },
      {
        role: "assistant",
        text: "",
        status: "error",
        error: "tool failed",
        blocks: [{ kind: "tool", name: "query", state: "bad", preview: "no model" }],
      },
      { role: "assistant", text: "Response stopped.", status: "stopped" },
    ],
  }));
  const saved = store.get("c-1").turns;
  assert.equal(saved[1].blocks[0].kind, "tool");
  assert.equal(saved[1].status, "error");
  assert.equal(saved[1].error, "tool failed");
  assert.equal(saved[2].status, "stopped");
});

const approvalBlock = (overrides = {}) => ({
  kind: "approval",
  name: "preview_property_change",
  capabilities: ["ifc.write.preview"],
  state: "approved",
  decidedBy: "you",
  reason: "",
  args: JSON.stringify({
    property: "FireRating",
    value: "F30",
    pset: "Pset_WallCommon",
    global_ids: ["0aB3cD4eF5gH6iJ7kL8mN9", "1aB3cD4eF5gH6iJ7kL8mN9"],
  }),
  ...overrides,
});

test("who approved a gated write survives a reload", () => {
  const store = new ChatHistoryStore(storage());
  store.save(record({
    turns: [
      { role: "user", text: "set the fire rating" },
      { role: "assistant", text: "Done.", blocks: [approvalBlock()] },
    ],
  }));
  const [saved] = store.get("c-1").turns[1].blocks;
  assert.equal(saved.kind, "approval");
  assert.equal(saved.name, "preview_property_change");
  assert.equal(saved.state, "approved");
  assert.equal(saved.decidedBy, "you");
  assert.deepEqual(saved.capabilities, ["ifc.write.preview"]);
});

test("a stored approval keeps no request id and no unknown state", () => {
  const store = new ChatHistoryStore(storage());
  store.save(record({
    turns: [
      { role: "user", text: "go" },
      {
        role: "assistant",
        text: "",
        blocks: [approvalBlock({ state: "pending", requestId: "req-7" })],
      },
    ],
  }));
  const [saved] = store.get("c-1").turns[1].blocks;
  assert.equal(saved.state, "denied");
  assert.equal(saved.requestId, undefined);
});

test("an approval reads as the change, not as the tool's arguments", () => {
  const digest = approvalDigest(approvalBlock());
  assert.equal(digest.headline, "Set FireRating to F30 on 2 elements");
  const labels = digest.facts.map((fact) => fact.label);
  assert.deepEqual(labels, ["Property", "Value", "Property set", "Targets"]);
  assert.equal(digest.facts.find((fact) => fact.label === "Value").value, "F30");
});

test("an approval for a tool with no property still says what it targets", () => {
  const digest = approvalDigest({
    name: "execute_ifc_code",
    args: JSON.stringify({ description: "tag the walls", selector: "IfcWall" }),
  });
  assert.equal(digest.headline, "Run execute_ifc_code on IfcWall");
  assert.deepEqual(digest.facts, [
    { label: "Intent", value: "tag the walls" },
    { label: "Targets", value: "IfcWall" },
  ]);
});

test("unreadable approval arguments degrade to the tool name", () => {
  const digest = approvalDigest({ name: "measure_elements", args: "{not json" });
  assert.equal(digest.headline, "Run measure_elements");
  assert.deepEqual(digest.facts, []);
});

test("the markdown export names every gated call and how it was answered", () => {
  const text = transcriptMarkdown(record({
    turns: [
      { role: "user", text: "set the fire rating" },
      { role: "assistant", text: "Done.", blocks: [approvalBlock()] },
    ],
  }));
  assert.match(text, /_Approval: `preview_property_change` approved \(you\)\./);
  assert.match(text, /Set FireRating to F30 on 2 elements/);
});

test("a handoff carries the last turns as readable, clipped text", () => {
  const slice = carrySlice(
    [
      { role: "user", text: "dropped" },
      { role: "user", text: "which walls are external?" },
      { role: "assistant", text: "x".repeat(60) },
      { role: "user", text: "measure them" },
    ],
    { count: 3, chars: 40, label: "General" },
  );
  const lines = slice.split("\n");
  assert.equal(lines[0], "Context carried from General:");
  assert.equal(lines.length, 4);
  assert.equal(lines[1], "Asked: which walls are external?");
  assert.equal(lines[2].endsWith("..."), true);
  assert.equal(lines[3], "Asked: measure them");
});

test("there is nothing to carry from an empty conversation", () => {
  assert.equal(carrySlice([]), "");
  assert.equal(carrySlice([{ role: "assistant", text: "   " }]), "");
  assert.equal(carrySlice(null), "");
});

test("plain-chat requests keep the newest complete window within a character cap", () => {
  const turns = Array.from({ length: 12 }, (_, index) => ({
    role: index % 2 ? "assistant" : "user",
    text: `${index}:` + "x".repeat(20),
  }));
  const bounded = boundedChatTurns(turns, { maxTurns: 4, maxChars: 100 });
  assert.deepEqual(bounded.map((turn) => turn.text.slice(0, 2)), ["8:", "9:", "10", "11"]);
  assert.ok(bounded.reduce((total, turn) => total + turn.text.length, 0) <= 100);
  assert.equal(bounded[0].role, "user");
});

test("one oversized latest message is explicitly clipped instead of dropped", () => {
  const [bounded] = boundedChatTurns(
    [{ role: "user", text: "x".repeat(500) }],
    { maxTurns: 5, maxChars: 80 },
  );
  assert.equal(bounded.text.length, 80);
  assert.match(bounded.text, /truncated/);
});

test("plain-chat request turns also respect the server's per-turn limit", () => {
  const [bounded] = boundedChatTurns(
    [{ role: "user", text: "x".repeat(150_000) }],
    { maxChars: 180_000, maxTurnChars: 100_000 },
  );
  assert.equal(bounded.text.length, 100_000);
  assert.match(bounded.text, /truncated/);
});
