/* Memory awareness for the agent panel, kept apart from the DOM.
 *
 * A long agent run on a large IFC file can push a laptop into swapping: the
 * console process holds the model, the browser holds the parsed geometry and a
 * transcript full of tool output. This module turns the numbers each side
 * reports into one level and one plan, so the panel can show the state and act
 * on it without guessing.
 */

const KIB = 1024;
const MIB = KIB * 1024;

// Above these fractions the panel says so and starts releasing what it can.
export const HEAP_HIGH = 0.7;
export const HEAP_CRITICAL = 0.86;
export const SYSTEM_HIGH = 0.12;
export const SYSTEM_CRITICAL = 0.06;
// Full tool output is kept for the newest turns only once memory is high.
export const KEEP_FULL_TURNS = 2;
// Estimated bytes of a transcript that still count as small.
export const TRANSCRIPT_HIGH = 12 * MIB;

export const LEVELS = ["ok", "high", "critical"];

const number = (value) => (Number.isFinite(Number(value)) && Number(value) >= 0 ? Number(value) : null);

export function formatBytes(bytes) {
  const value = number(bytes);
  if (value === null) return "";
  if (value >= 10 * MIB * 100) return `${(value / (MIB * 1024)).toFixed(1)} GB`;
  if (value >= MIB) return `${Math.round(value / MIB)} MB`;
  if (value >= KIB) return `${Math.round(value / KIB)} KB`;
  return `${Math.round(value)} B`;
}

/** The browser heap, from the non-standard `performance.memory` when present. */
export function sampleHeap(perf = globalThis.performance) {
  const memory = perf?.memory;
  if (!memory) return null;
  const used = number(memory.usedJSHeapSize);
  const limit = number(memory.jsHeapSizeLimit);
  if (used === null) return null;
  return { used, limit: limit || null, total: number(memory.totalJSHeapSize) };
}

/** Rough byte weight of the panel's own transcript state. */
export function transcriptBytes(turns = []) {
  let chars = 0;
  for (const turn of Array.isArray(turns) ? turns : []) {
    chars += String(turn?.text || "").length;
    for (const block of Array.isArray(turn?.blocks) ? turn.blocks : []) {
      chars += String(block?.text || "").length
        + String(block?.args || "").length
        + String(block?.preview || "").length
        + String(block?.detail || "").length;
    }
  }
  // UTF-16 in memory, and the DOM roughly doubles it again.
  return chars * 4;
}

function levelOf(heap, server, transcript, viewer) {
  let level = 0;
  if (heap?.limit) {
    const ratio = heap.used / heap.limit;
    if (ratio >= HEAP_CRITICAL) level = Math.max(level, 2);
    else if (ratio >= HEAP_HIGH) level = Math.max(level, 1);
  }
  if (server?.total && server.available !== null && server.available !== undefined) {
    const free = server.available / server.total;
    if (free <= SYSTEM_CRITICAL) level = Math.max(level, 2);
    else if (free <= SYSTEM_HIGH) level = Math.max(level, 1);
  }
  if (transcript >= TRANSCRIPT_HIGH) level = Math.max(level, 1);
  if (viewer?.parsedCacheBytes >= 200 * MIB) level = Math.max(level, 1);
  return LEVELS[level];
}

/**
 * One report from every source the panel can see.
 *
 * `server` is the console's own reading, `viewer` is what the 3D component
 * publishes with its context, `heap` is this page. Any of them may be missing
 * and the report still says what it can.
 */
export function memoryReport({ heap = null, server = null, viewer = null, turns = [] } = {}) {
  const serverView = server && typeof server === "object"
    ? {
        rss: number(server.rss_bytes),
        peak: number(server.peak_rss_bytes),
        total: number(server.total_bytes),
        available: number(server.available_bytes),
      }
    : null;
  const viewerView = viewer && typeof viewer === "object"
    ? {
        parsedCacheBytes: number(viewer.parsedCacheBytes ?? viewer.parsed_cache_bytes) || 0,
        parsedCacheEntries: number(viewer.parsedCacheEntries ?? viewer.parsed_cache_entries) || 0,
        elements: number(viewer.elements) || 0,
        triangles: number(viewer.triangles) || 0,
        workerAlive: Boolean(viewer.workerAlive ?? viewer.worker_alive),
      }
    : null;
  const transcript = transcriptBytes(turns);
  const level = levelOf(heap, serverView, transcript, viewerView);
  const parts = [];
  if (heap) parts.push(`page ${formatBytes(heap.used)}`);
  if (serverView?.rss) parts.push(`console ${formatBytes(serverView.rss)}`);
  if (serverView?.available !== null && serverView?.available !== undefined && serverView?.total) {
    parts.push(`${formatBytes(serverView.available)} free`);
  }
  const headline = heap
    ? formatBytes(heap.used)
    : serverView?.rss
      ? formatBytes(serverView.rss)
      : "";
  const detail = [];
  if (heap) {
    detail.push(
      `This page: ${formatBytes(heap.used)}${heap.limit ? ` of ${formatBytes(heap.limit)}` : ""}.`,
    );
  }
  if (serverView?.rss) {
    detail.push(
      `Console process: ${formatBytes(serverView.rss)}`
      + (serverView.peak ? ` (peak ${formatBytes(serverView.peak)})` : "")
      + ".",
    );
  }
  if (serverView?.total) {
    detail.push(
      `Machine: ${formatBytes(serverView.available ?? 0)} free of ${formatBytes(serverView.total)}.`,
    );
  }
  if (viewerView) {
    detail.push(
      `3D view: ${viewerView.elements} products, ${Math.round(viewerView.triangles / 1000)}k triangles`
      + (viewerView.parsedCacheBytes
        ? `, ${formatBytes(viewerView.parsedCacheBytes)} of parsed models cached`
        : "")
      + (viewerView.workerAlive ? ", parser worker resident" : "")
      + ".",
    );
  }
  detail.push(`Transcript: about ${formatBytes(transcript)}.`);
  return {
    level,
    headline,
    summary: parts.join(" · "),
    detail: detail.join("\n"),
    heap,
    server: serverView,
    viewer: viewerView,
    transcript,
  };
}

/**
 * What to release for this report, most valuable first.
 *
 * Nothing here loses a conversation: parsed models re-parse on the next tab
 * switch, and a trimmed tool card still holds its preview.
 */
export function reliefPlan(report, { turnCount = 0, force = false } = {}) {
  const plan = { releaseViewer: false, trimTurns: false, keepTurns: KEEP_FULL_TURNS, stopWorker: false };
  if (!report) return plan;
  const pressed = force || report.level !== "ok";
  if (!pressed) return plan;
  if (report.viewer?.parsedCacheBytes > 0 || report.viewer?.workerAlive) {
    plan.releaseViewer = true;
    plan.stopWorker = true;
  }
  if (turnCount > KEEP_FULL_TURNS) plan.trimTurns = true;
  return plan;
}

/** How often to look, in milliseconds: often while a run is live, rarely idle. */
export function sampleInterval({ busy = false, level = "ok" } = {}) {
  if (busy) return level === "ok" ? 4000 : 2000;
  return level === "ok" ? 30000 : 10000;
}
