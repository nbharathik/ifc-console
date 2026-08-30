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
