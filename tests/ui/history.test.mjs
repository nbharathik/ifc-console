import assert from "node:assert/strict";
import { test } from "node:test";

import {
  ChatHistoryStore,
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
  const store = new ChatHistoryStore(storage({ "ifc-console-chat-history-v2": "{not json" }));
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
