/* Module worker: parses IFC off the main thread so the page never freezes.
 * One IfcAPI instance lives for the worker's lifetime; each load opens and
 * closes its own model. Messages mirror parser.js emissions verbatim.
 */

import { IfcAPI } from "./vendor/web-ifc-api.js";
import { parseModel } from "./parser.js";

let apiPromise = null;

function ensureApi() {
  if (!apiPromise) {
    const api = new IfcAPI();
    api.SetWasmPath("/viewer/static/vendor/", true);
    apiPromise = api.Init()
      .then(() => api)
      .catch((error) => {
        apiPromise = null;
        throw error;
      });
  }
  return apiPromise;
}

// Fetch and instantiate the wasm now: the main thread is still downloading the
// model, so this costs nothing and the first parse starts immediately.
ensureApi().catch(() => { /* surfaced when a parse actually arrives */ });

let queue = Promise.resolve();

self.onmessage = (event) => {
  const { seq, buffer } = event.data;
  // Serialize loads: a reload arriving mid-parse waits its turn instead of
  // interleaving two models inside one wasm heap.
  queue = queue.then(async () => {
    let api;
    try {
      api = await ensureApi();
    } catch (err) {
      self.postMessage({
        seq,
        type: "error",
        message: String(err && err.message || err),
        init_failed: true,
      });
      return;
    }
    try {
      await parseModel(api, new Uint8Array(buffer), (message, transfers) => {
        self.postMessage({ seq, ...message }, transfers || []);
      });
    } catch (err) {
      self.postMessage({ seq, type: "error", message: String(err && err.message || err) });
    }
  });
};
