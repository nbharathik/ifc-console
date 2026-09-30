// Pure decisions for following an edit without rebuilding the scene.
//
// The hub sends a model_updated frame after every edit, undo, redo and save. A
// tab that already shows the revision just before the change can take it in
// place when the change touched no geometry: only the etag moves, the labels
// and the open properties refresh. Anything else rebuilds.

/**
 * @param {object} frame  a model_updated frame from the hub
 * @param {{etag: string|null}} state  what this tab currently shows
 * @returns {{kind: "none"} | {kind: "reload"} |
 *   {kind: "adopt", elements: string[]|null, names: Object<string, string|null>}}
 */
export function planUpdate(frame, state) {
  if (!frame.etag) return { kind: "reload" };
  if (frame.etag === state.etag) return { kind: "none" };
  if (frame.reason === "loaded") return { kind: "reload" };
  if (frame.geometry !== false || frame.tree) return { kind: "reload" };
  if (!frame.base_etag || frame.base_etag !== state.etag) return { kind: "reload" };
  if (frame.labels && !frame.names) return { kind: "reload" };
  return {
    kind: "adopt",
    elements: Array.isArray(frame.elements) ? frame.elements : null,
    names: frame.labels ? frame.names : {},
  };
}

/** Whether the properties open for `guid` need a refresh after this update. */
export function touchesElement(plan, guid) {
  if (!guid) return false;
  return plan.elements === null || plan.elements.includes(guid);
}

/** Index a parsed tree by express id, once, so labels can be patched by id. */
export function indexTree(root) {
  const byId = new Map();
  const visit = (node) => {
    if (typeof node.expressID === "number") byId.set(node.expressID, node);
    for (const child of node.children || []) visit(child);
  };
  if (root) visit(root);
  return byId;
}

/** New tree label for a node: name first, class in brackets, as the tree draws it. */
export function labelParts(name, cls) {
  return {
    name: name ? `${name} ` : "",
    cls: name ? `(${cls})` : cls,
    title: name ? `${name} (${cls})` : cls,
  };
}
