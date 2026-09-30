/* The parser worker, its inline fallback, and the model download. */

import { showProgress } from "./dom.js";

// worker.js does the parsing; if workers are unavailable the same parser
// module runs inline on the main thread as a fallback.
export let worker = null;
export let workerBusy = false;
let workerIdleTimer = 0;
let loadGen = 0;
let activeHandlers = null;
const WORKER_IDLE_MS = 30_000;

function clearWorkerIdle() {
  clearTimeout(workerIdleTimer);
  workerIdleTimer = 0;
}

export function stopWorker() {
  clearWorkerIdle();
  if (worker) worker.terminate();
  worker = null;
  workerBusy = false;
}

function scheduleWorkerIdle() {
  clearWorkerIdle();
  if (!worker || workerBusy) return;
  // web-ifc closes the model but its WASM heap stays at its high-water mark.
  // Keep it briefly for a tab switch, then return that memory to the browser.
  workerIdleTimer = setTimeout(() => {
    if (workerBusy) return;
    stopWorker();
    releaseInlineParser();
  }, WORKER_IDLE_MS);
}

function routeParserMessage(msg) {
  if (!activeHandlers || msg.seq !== loadGen) return;
  const h = activeHandlers;
  if (msg.type === "chunk") h.onChunk(msg);
  else if (msg.type === "progress") h.onProgress(msg);
  else if (msg.type === "coordination") h.onCoordination(msg);
  else if (msg.type === "maps") h.onMaps(msg);
  else if (msg.type === "tree") h.onTree(msg);
  else if (msg.type === "done") h.onDone(msg);
  else if (msg.type === "error" && msg.init_failed) {
    stopWorker();
    h.onWorkerLost();
  }
  else if (msg.type === "error") h.onError(new Error(msg.message));
}

export function spawnWorker() {
  clearWorkerIdle();
  worker = new Worker("/viewer/static/worker.js", { type: "module" });
  worker.onmessage = (event) => routeParserMessage(event.data);
  worker.onerror = (event) => {
    // A worker that cannot boot (or crashed) fails the current load over to
    // the inline path; the next load will try a fresh worker again.
    console.warn("[ifc-console] parser worker failed", event.message || event);
    const h = activeHandlers;
    stopWorker();
    if (h) h.onWorkerLost();
  };
}

async function parseInline(buffer, handlers) {
  const seq = loadGen;
  const [{ IfcAPI }, { parseModel }] = await Promise.all([
    import("./vendor/web-ifc-api.js"),
    import("./parser.js"),
  ]);
  if (!parseInline.api) {
    const api = new IfcAPI();
    api.SetWasmPath("/viewer/static/vendor/", true);
    await api.Init();
    parseInline.api = api;
  }
  await parseModel(parseInline.api, buffer, (message) => {
    if (seq === loadGen) routeParserMessage({ seq, ...message });
  });
  return handlers;
}

export function parseBuffer(buffer) {
  clearWorkerIdle();
  loadGen++;
  const seq = loadGen;
  if (workerBusy && worker) {
    // A parse is still running for a previous revision: wasm cannot be
    // interrupted, so drop the whole worker and start fresh.
    stopWorker();
  }
  return new Promise((resolve, reject) => {
    let finished = false;
    let fallbackStarted = false;
    let chunks = [];
    let maps = null;
    let tree = null;
    let coordination = null;
    const handlers = {
      onChunk: (msg) => chunks.push(msg),
      onCoordination: (msg) => { coordination = msg.matrix; },
      onProgress: (msg) => {
        if (msg.stage === "geometry") {
          showProgress(`Reading geometry: ${msg.products} elements`, null);
        } else {
          showProgress("Reading names and IDs", msg.total ? msg.resolved / msg.total : null);
        }
      },
      onMaps: (msg) => { maps = msg; },
      onTree: (msg) => { tree = msg.tree; },
      onDone: () => {
        finished = true;
        workerBusy = false;
        if (activeHandlers === handlers) activeHandlers = null;
        scheduleWorkerIdle();
        resolve({ chunks, maps, tree, coordination });
      },
      onError: (err) => {
        finished = true;
        if (activeHandlers === handlers) activeHandlers = null;
        stopWorker();
        reject(err);
      },
      onWorkerLost: () => {
        if (finished || fallbackStarted || seq !== loadGen) return;
        fallbackStarted = true;
        chunks = [];
        maps = null;
        tree = null;
        coordination = null;
        showProgress("Retrying model load", null);
        parseInline(buffer, handlers).catch(handlers.onError);
      },
    };
    activeHandlers = handlers;
    if (typeof Worker === "undefined") {
      // No worker to lose: parse the original buffer with no copy at all.
      handlers.onWorkerLost();
      return;
    }
    try {
      if (!worker) spawnWorker();
      workerBusy = true;
      // A copy is transferred, not the original: onWorkerLost still needs
      // readable bytes to fall back to the inline parser.
      const copy = buffer.slice();
      worker.postMessage({ seq, buffer: copy.buffer }, [copy.buffer]);
    } catch {
      stopWorker();
      handlers.onWorkerLost();
    }
  });
}

/** The inline web-ifc fallback keeps a WebAssembly heap; let it go when idle. */
export function releaseInlineParser() {
  if (!parseInline.api) return false;
  parseInline.api = null;
  return true;
}

export async function fetchModelBytes(res) {
  const total = Number(res.headers.get("content-length")) || 0;
  if (!res.body || !res.body.getReader) {
    return new Uint8Array(await res.arrayBuffer());
  }
  const reader = res.body.getReader();
  // FileResponse supplies Content-Length. Fill that one allocation directly
  // instead of retaining every network chunk and then allocating the whole
  // model again at the end. Unknown/chunked responses keep the fallback list.
  let buffer = total ? new Uint8Array(total) : null;
  const parts = buffer ? null : [];
  let received = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    if (buffer && received + value.length <= buffer.length) {
      buffer.set(value, received);
    } else if (buffer) {
      // A misleading Content-Length should cost one growth, not corrupt data.
      let size = Math.max(received + value.length, buffer.length * 2, 64 * 1024);
      const grown = new Uint8Array(size);
      grown.set(buffer.subarray(0, received));
      grown.set(value, received);
      buffer = grown;
    } else {
      parts.push(value);
    }
    received += value.length;
    showProgress(
      `Downloading model: ${(received / 1_048_576).toFixed(1)} MB`,
      total ? received / total : null);
  }
  if (buffer) return received === buffer.length ? buffer : buffer.slice(0, received);
  buffer = new Uint8Array(received);
  let offset = 0;
  for (const part of parts) {
    buffer.set(part, offset);
    offset += part.length;
  }
  return buffer;
}
