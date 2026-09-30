/* Parsed models kept for quick tab switches, bounded by count and bytes. */

// Parsing is the expensive WebAssembly step. Keep the parsed chunks for every
// recent IFC revision so returning to a tab is quick, but bound the typed
// arrays: an unlimited cache duplicated the GPU scene for every resident file.
export const parsedModelCache = new Map(); // model id -> { etag, parsed, bytes }
const PARSED_CACHE_MAX_ENTRIES = 2;
// Sized to the machine, and modest: a tab switch that re-parses costs a few
// seconds, a laptop that starts swapping costs the whole session.
const PARSED_CACHE_BUDGET = Math.round(
  Math.min(160, Math.max(64, (Number(navigator.deviceMemory) || 4) * 32)) * 1_048_576,
);
export let parsedModelCacheBytes = 0;
let residentModelCount = () => 0;

/** The page says how many models are resident; one model is never cached. */
export function bindParsedCache(host) {
  residentModelCount = host.residentModelCount;
}

function parsedModelBytes(parsed) {
  let bytes = 0;
  const addArrays = (record) => {
    for (const value of Object.values(record || {})) {
      if (ArrayBuffer.isView(value)) bytes += value.byteLength;
    }
  };
  for (const chunk of parsed?.chunks || []) {
    addArrays(chunk.geometry);
    addArrays(chunk.placements);
  }
  addArrays(parsed?.maps);
  for (const guid of parsed?.maps?.guids || []) bytes += guid.length * 2;
  return bytes;
}

export function dropParsedModel(modelId) {
  const previous = parsedModelCache.get(modelId);
  if (!previous) return;
  parsedModelCache.delete(modelId);
  parsedModelCacheBytes -= previous.bytes;
}

export function cacheParsedModel(modelId, etag, parsed) {
  if (!modelId || !etag || residentModelCount() < 2) return;
  const bytes = parsedModelBytes(parsed);
  dropParsedModel(modelId);
  if (bytes > PARSED_CACHE_BUDGET) return;
  parsedModelCache.set(modelId, { etag, parsed, bytes });
  parsedModelCacheBytes += bytes;
  while (
    parsedModelCache.size > PARSED_CACHE_MAX_ENTRIES
    || parsedModelCacheBytes > PARSED_CACHE_BUDGET
  ) {
    dropParsedModel(parsedModelCache.keys().next().value);
  }
}

/** A change that touched no geometry leaves the parsed model valid under a new etag. */
export function rekeyParsedModel(modelId, from, to) {
  const entry = parsedModelCache.get(modelId);
  if (entry && entry.etag === from) entry.etag = to;
}

export function cachedParsedModel(modelId, etag) {
  const entry = parsedModelCache.get(modelId);
  if (!entry || entry.etag !== etag) {
    if (entry) dropParsedModel(modelId);
    return null;
  }
  // Map insertion order is the LRU list.
  parsedModelCache.delete(modelId);
  parsedModelCache.set(modelId, entry);
  return entry;
}

export function dropParsedCache() {
  const freed = parsedModelCacheBytes;
  for (const modelId of [...parsedModelCache.keys()]) dropParsedModel(modelId);
  return freed;
}
