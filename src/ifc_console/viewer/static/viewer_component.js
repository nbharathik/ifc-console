/* Public in-page contract for the IFC viewer component.
 *
 * The 3D surface owns every viewer feature (selection, camera, sections,
 * measurements and screenshots). Optional panels receive this small facade
 * when they mount, so they call the same implementation as the WebSocket/MCP
 * path instead of carrying a second viewer or reaching into viewer globals.
 *
 * DOM events remain as a compatibility adapter for third-party panels built
 * before the component contract existed.
 */

export const VIEWER_CONTEXT_EVENT = "ifc-console:viewer-context";
export const VIEWER_COMMAND_EVENT = "ifc-console:viewer-command";
export const VIEWER_RESULT_EVENT = "ifc-console:viewer-result";

function isPlainObject(value) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

function failureText(error) {
  const where = String(error?.stack || "").split(String.fromCharCode(10))[1]?.trim();
  return where ? `${error} (${where})` : String(error);
}

function resultPayload(command, ok, result = null, error = null) {
  return {
    version: 1,
    commandId: command.commandId || null,
    action: command.action || "",
    ok,
    result,
    error,
  };
}

export function createViewerComponent({ readContext, execute, target = document }) {
  if (typeof readContext !== "function" || typeof execute !== "function") {
    throw new TypeError("viewer component needs readContext and execute functions");
  }

  const subscribers = new Set();
  const resultSubscribers = new Set();

  const api = Object.freeze({
    version: 1,
    getContext(reason = "component") {
      return readContext(reason);
    },
    execute(command = {}) {
      if (!isPlainObject(command)) {
        return Promise.reject(new TypeError("viewer command must be an object"));
      }
      return Promise.resolve().then(() => execute(command));
    },
    subscribe(listener) {
      if (typeof listener !== "function") {
        throw new TypeError("viewer subscriber must be a function");
      }
      subscribers.add(listener);
      return () => subscribers.delete(listener);
    },
    subscribeResults(listener) {
      if (typeof listener !== "function") {
        throw new TypeError("viewer result subscriber must be a function");
      }
      resultSubscribers.add(listener);
      return () => resultSubscribers.delete(listener);
    },
  });

  function publish(context) {
    for (const listener of [...subscribers]) {
      try {
        listener(context);
      } catch (error) {
        console.error("[ifc-console] viewer component subscriber failed", error);
      }
    }
    target.dispatchEvent(new CustomEvent(VIEWER_CONTEXT_EVENT, { detail: context }));
  }

  function publishResult(command, ok, result = null, error = null) {
    const detail = resultPayload(command, ok, result, error);
    for (const listener of [...resultSubscribers]) {
      try {
        listener(detail);
      } catch (subscriberError) {
        console.error("[ifc-console] viewer result subscriber failed", subscriberError);
      }
    }
    target.dispatchEvent(new CustomEvent(VIEWER_RESULT_EVENT, {
      detail,
    }));
  }

  async function handleLegacyCommand(event) {
    const command = isPlainObject(event.detail) ? event.detail : {};
    try {
      publishResult(command, true, await api.execute(command));
    } catch (error) {
      publishResult(command, false, null, failureText(error));
    }
  }

  target.addEventListener(VIEWER_COMMAND_EVENT, handleLegacyCommand);

  return Object.freeze({
    api,
    publish,
    publishResult,
    dispose() {
      subscribers.clear();
      resultSubscribers.clear();
      target.removeEventListener(VIEWER_COMMAND_EVENT, handleLegacyCommand);
    },
  });
}
