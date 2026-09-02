/* Pure logic behind the workflows surface: stream framing, run state, and
 * input coercion. Kept apart from the DOM so it can be tested directly. */

/** Split a growing SSE buffer into decoded events plus the unfinished tail.
 *
 * A read can land mid-frame, so the caller keeps `rest` and prepends it to the
 * next chunk. Frames that are not JSON are dropped rather than throwing: one
 * malformed line must not end a run that is otherwise fine.
 */
export function parseEventStream(buffer) {
  const chunks = buffer.split("\n\n");
  const rest = chunks.pop() ?? "";
  const events = [];
  for (const chunk of chunks) {
    for (const line of chunk.split("\n")) {
      const text = line.trim();
      if (!text.startsWith("data:")) continue;
      try {
        events.push(JSON.parse(text.slice(5).trim()));
      } catch {
        // A frame we cannot read is not a frame we can act on.
      }
    }
  }
  return { events, rest };
}

/** Bound one piece of run detail so a long run cannot grow the page without limit. */
export function clipText(value, limit) {
  const text = String(value ?? "");
  if (!Number.isFinite(limit) || limit <= 0 || text.length <= limit) return text;
  const marker = "\n… truncated";
  return text.slice(0, Math.max(0, limit - marker.length)) + marker;
}

/** Coerce one input field's browser value for the run request. */
export function inputValue(spec, raw) {
  if (spec.type === "boolean") return Boolean(raw);
  if (spec.type === "number") {
    if (raw === "" || raw === null || raw === undefined) return "";
    const value = Number(raw);
    return Number.isFinite(value) ? value : raw;
  }
  return raw === null || raw === undefined ? "" : String(raw);
}

/** Add the loopback session token without discarding caller headers. */
export function authenticatedOptions(token, options = {}) {
  return {
    ...options,
    headers: {
      Authorization: `Bearer ${token || ""}`,
      ...(options.headers || {}),
    },
  };
}

/** Which inputs a run is missing, by label, so the page can say so first. */
export function missingInputs(inputs, values) {
  return inputs
    .filter((item) => {
      if (!item.required) return false;
      const value = values[item.id];
      return value === undefined || value === null || String(value).trim() === "";
    })
    .map((item) => item.label);
}

/** What the reader should see when the stream ends.
 *
 * A stream that stops without `workflow_completed` is not a success: the run
 * was interrupted, and saying "finished" there would be a lie.
 */
export function runOutcome(events) {
  const completed = [...events].reverse().find((event) => event.type === "workflow_completed");
  if (completed) {
    return {
      state: completed.state,
      summary: completed.summary || "",
      done: true,
    };
  }
  const failure = [...events].reverse().find((event) => event.type === "error");
  return {
    state: failure ? "failed" : "interrupted",
    summary: "",
    done: false,
    error: failure?.text || "",
  };
}

/** Normalize viewer facade and API payloads into one workflow context. */
export function normalizeViewerContext(value = {}) {
  const current = value && typeof value === "object" ? value : {};
  const active = current.model && typeof current.model === "object" ? current.model : null;
  const models = (Array.isArray(current.models) ? current.models : [])
    .map((row) => ({
      id: String(row?.id ?? row?.model_id ?? ""),
      name: String(row?.name ?? row?.model ?? row?.id ?? "IFC"),
      active: Boolean(row?.active) || Boolean(active?.id && row?.id === active.id),
    }))
    .filter((row) => row.id);
  if (active?.id && !models.some((row) => row.id === active.id)) {
    models.unshift({ id: String(active.id), name: String(active.name || active.id), active: true });
  }
  let selections = Array.isArray(current.selections) ? current.selections : [];
  if (!selections.length && Array.isArray(current.selection?.guids) && active?.id) {
    selections = [{ model_id: active.id, model: active.name, guids: current.selection.guids }];
  }
  return {
    connected: current.connected !== undefined ? Boolean(current.connected) : Boolean(current.open),
    models,
    selections: selections
      .map((row) => ({
        model_id: String(row?.model_id ?? row?.id ?? ""),
        model: String(row?.model ?? row?.name ?? row?.model_id ?? "IFC"),
        guids: Array.isArray(row?.guids) ? row.guids.map(String).filter(Boolean) : [],
      }))
      .filter((row) => row.model_id && row.guids.length),
  };
}

export function selectionCount(context) {
  const rows = Array.isArray(context?.selections) ? context.selections : [];
  return rows.reduce(
    (total, row) => total + (Array.isArray(row?.guids) ? row.guids.length : 0),
    0,
  );
}

export function initialScope(flow, viewerContext) {
  if (flow?.scope === "selection") return "selection";
  if (flow?.scope === "either" && selectionCount(viewerContext)) return "selection";
  return "model";
}
