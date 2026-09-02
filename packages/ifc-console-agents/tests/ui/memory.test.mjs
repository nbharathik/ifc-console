import assert from "node:assert/strict";
import { test } from "node:test";

import {
  KEEP_FULL_TURNS,
  formatBytes,
  memoryReport,
  reliefPlan,
  sampleHeap,
  sampleInterval,
  transcriptBytes,
} from "../../src/ifc_console_agents/static/chat_memory.js";

const MIB = 1024 * 1024;

test("bytes format at the scale a reader expects", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(20 * 1024), "20 KB");
  assert.equal(formatBytes(300 * MIB), "300 MB");
  assert.equal(formatBytes(1.5 * 1024 * MIB), "1.5 GB");
  assert.equal(formatBytes(-1), "");
  assert.equal(formatBytes("nope"), "");
});

test("the heap sample tolerates browsers without performance.memory", () => {
  assert.equal(sampleHeap({}), null);
  assert.equal(sampleHeap(undefined), null);
  const heap = sampleHeap({ memory: { usedJSHeapSize: 10, jsHeapSizeLimit: 100, totalJSHeapSize: 20 } });
  assert.deepEqual(heap, { used: 10, limit: 100, total: 20 });
});

test("an empty panel reads as ok with an honest summary", () => {
  const report = memoryReport({});
  assert.equal(report.level, "ok");
  assert.equal(report.headline, "");
  assert.match(report.detail, /Transcript: about 0 B/);
});

test("a heap near its limit is critical and a large parsed cache is high", () => {
  const critical = memoryReport({ heap: { used: 90, limit: 100 } });
  assert.equal(critical.level, "critical");
  const high = memoryReport({
    heap: { used: 10, limit: 100 },
    viewer: { parsedCacheBytes: 250 * MIB, elements: 5, triangles: 1000 },
  });
  assert.equal(high.level, "high");
  assert.match(high.detail, /250 MB of parsed models cached/);
});

test("the machine running out of memory outranks a small page", () => {
  const report = memoryReport({
    heap: { used: 10, limit: 1000 },
    server: { rss_bytes: 900 * MIB, total_bytes: 16 * 1024 * MIB, available_bytes: 500 * MIB },
  });
  assert.equal(report.level, "critical");
  assert.match(report.summary, /console 900 MB/);
  assert.match(report.summary, /500 MB free/);
});

test("transcript weight counts every text field of every block", () => {
  const turns = [
    { text: "ab", blocks: [{ text: "cd", args: "e", preview: "f", detail: "g" }] },
    { text: "hi" },
  ];
  assert.equal(transcriptBytes(turns), (2 + 2 + 1 + 1 + 1 + 2) * 4);
  assert.equal(transcriptBytes(null), 0);
});

test("the relief plan releases the viewer first and trims only old turns", () => {
  const idle = reliefPlan(memoryReport({ heap: { used: 1, limit: 100 } }), { turnCount: 10 });
  assert.deepEqual(idle, { releaseViewer: false, trimTurns: false, keepTurns: KEEP_FULL_TURNS, stopWorker: false });
  const pressed = reliefPlan(
    memoryReport({ heap: { used: 80, limit: 100 }, viewer: { parsedCacheBytes: MIB } }),
    { turnCount: 10 },
  );
  assert.equal(pressed.releaseViewer, true);
  assert.equal(pressed.trimTurns, true);
  const forced = reliefPlan(memoryReport({ viewer: { workerAlive: true } }), { turnCount: 1, force: true });
  assert.equal(forced.releaseViewer, true);
  assert.equal(forced.trimTurns, false);
});

test("sampling is frequent during a run and rare when idle", () => {
  assert.ok(sampleInterval({ busy: true }) < sampleInterval({ busy: false }));
  assert.ok(sampleInterval({ busy: true, level: "high" }) < sampleInterval({ busy: true }));
});
