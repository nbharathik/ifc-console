/* The viewer's persisted UI state: panel widths and visibility, scene settings, saved views. */

// Panel widths/visibility and scene settings persist across sessions.
export function isPlainObject(value) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype;
}

export const uiState = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem("ifc-console-viewer-ui") || "{}");
    return isPlainObject(saved) ? saved : {};
  } catch {
    return {};
  }
})();
export function saveUi() {
  try {
    localStorage.setItem("ifc-console-viewer-ui", JSON.stringify(uiState));
  } catch {
    // Storage can be unavailable in private mode or full.
  }
}
