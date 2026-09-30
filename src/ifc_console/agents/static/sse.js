/* Server-sent event framing for the agent streams. */

/** Split a growing SSE buffer into decoded events plus the unfinished tail.
 *
 * A read can land mid-frame, so the caller keeps `rest` and prepends it to the
 * next chunk. A malformed frame is dropped so one bad line cannot end a stream
 * that is otherwise fine.
 */
export function decodeSSE(buffer = "") {
  const source = typeof buffer === "string" ? buffer : buffer == null ? "" : String(buffer);
  const frames = source.replaceAll("\r\n", "\n").split("\n\n");
  const rest = frames.pop() || "";
  const events = [];
  let done = false;
  for (const frame of frames) {
    const data = frame
      .split("\n")
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (!data) continue;
    if (data === "[DONE]") {
      done = true;
      continue;
    }
    try {
      events.push(JSON.parse(data));
    } catch {
      // A malformed frame must not break the remaining stream.
    }
  }
  return { events, rest, done };
}
