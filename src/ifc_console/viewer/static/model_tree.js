/* The spatial tree in the model panel: lazy rows, branch visibility, selection marks. */

import { indexTree, labelParts } from "./delta.js";
import { $, el } from "./dom.js";
import { markSearchSelection } from "./search_panel.js";

let elements;
let expressOf;
let hiddenByTree;
let selection;
let setSelection;
let applyVisibility;

/** The page hands over the scene state the tree reads and the actions it takes. */
export function bindModelTree(host) {
  ({ elements, expressOf, hiddenByTree, selection, setSelection, applyVisibility } = host);
}

// The parsed tree behind the sidebar, kept so a rename can relabel one row
// without rebuilding the scene. The index is made on first use.
let renderedTree = null;
let treeIndex = null;

/** Relabel tree rows after an edit renamed their elements. */
export function applyLabels(names) {
  if (!renderedTree) return;
  if (!treeIndex) treeIndex = indexTree(renderedTree);
  for (const [guid, name] of Object.entries(names || {})) {
    const id = expressOf.get(guid);
    const node = id === undefined ? null : treeIndex.get(id);
    if (!node) continue;
    node._name = name || null;
    const label = document.querySelector(`#tree [data-express-id="${id}"]`);
    if (!label) continue;
    const parts = labelParts(node._name, String(node.type || "?"));
    const [nameSpan, classSpan] = label.children;
    if (nameSpan) nameSpan.textContent = parts.name;
    if (classSpan) classSpan.textContent = parts.cls;
    label.title = parts.title;
  }
}

const SPATIAL_TYPES = new Set([
  "IFCPROJECT", "IFCSITE", "IFCBUILDING", "IFCBUILDINGSTOREY", "IFCSPACE",
  "IFCFACILITY", "IFCBRIDGE", "IFCROAD", "IFCRAILWAY", "IFCMARINEFACILITY",
]);

function isSpatial(node) {
  return SPATIAL_TYPES.has(String(node.type || "").toUpperCase());
}

function branchElements(node) {
  const ids = new Set();
  const visit = (branch) => {
    if (elements.has(branch.expressID)) ids.add(branch.expressID);
    for (const child of branch.children || []) visit(child);
  };
  visit(node);
  return [...ids];
}

export function renderTree(rootNode) {
  const container = $("tree");
  container.textContent = "";
  renderedTree = rootNode || null;
  treeIndex = null;
  if (!rootNode) return;
  const list = el("ul");
  list.appendChild(buildTreeItem(rootNode, 0));
  container.appendChild(list);
}

// Children build lazily (on first expand, in slices) so a 100k-element model
// does not become a 100k-row DOM before the user ever opens a storey.
const TREE_SLICE = 250;

function buildTreeItem(node, depth) {
  const li = el("li");
  const row = el("div", "tree-row");
  const children = node.children || [];
  const spatial = isSpatial(node);
  // Project / Site / Building / Storey come pre-expanded; elements collapsed.
  const expanded = depth < 4;

  const toggle = el("button", "tree-toggle", children.length ? (expanded ? "▾" : "▸") : " ");
  toggle.type = "button";
  if (!children.length) {
    toggle.disabled = true;
    toggle.tabIndex = -1;
    toggle.setAttribute("aria-hidden", "true");
  }
  row.appendChild(toggle);

  if (spatial && children.length) {
    const checkbox = el("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkbox.title = "toggle visibility of this branch";
    checkbox.setAttribute(
      "aria-label",
      `Show ${node._name || String(node.type || "model branch")}`,
    );
    checkbox.addEventListener("change", () => {
      for (const id of branchElements(node)) {
        if (checkbox.checked) hiddenByTree.delete(id);
        else hiddenByTree.add(id);
      }
      applyVisibility();
    });
    row.appendChild(checkbox);
  }

  const cls = String(node.type || "?");
  const label = el("button", "tree-label");
  label.type = "button";
  label.appendChild(el("span", null, node._name ? `${node._name} ` : ""));
  label.appendChild(el("span", "cls", node._name ? `(${cls})` : cls));
  label.dataset.expressId = node.expressID;
  label.title = node._name ? `${node._name} (${cls})` : cls;
  label.setAttribute("aria-pressed", "false");
  row.appendChild(label);
  li.appendChild(row);

  let kids = null;
  let built = 0;
  const buildSlice = () => {
    const frag = document.createDocumentFragment();
    const end = Math.min(children.length, built + TREE_SLICE);
    for (; built < end; built++) {
      frag.appendChild(buildTreeItem(children[built], depth + 1));
    }
    if (built < children.length) {
      const moreItem = el("li", "tree-more-item");
      const more = el("button", "tree-more", `Show ${Math.min(TREE_SLICE, children.length - built)} more (${children.length - built} hidden)`);
      more.type = "button";
      more.addEventListener("click", () => {
        moreItem.remove();
        buildSlice();
      });
      moreItem.appendChild(more);
      frag.appendChild(moreItem);
    }
    kids.appendChild(frag);
  };
  const setOpen = (open) => {
    if (!kids) return;
    if (open && !built) buildSlice();
    kids.hidden = !open;
    toggle.textContent = kids.hidden ? "▸" : "▾";
    toggle.setAttribute("aria-label", `${kids.hidden ? "Expand" : "Collapse"} ${label.title}`);
    toggle.setAttribute("aria-expanded", String(!kids.hidden));
    label.setAttribute("aria-expanded", String(!kids.hidden));
  };
  if (children.length) {
    kids = el("ul");
    li.appendChild(kids);
    setOpen(expanded);
    if (!expanded) toggle.textContent = "▸";
    toggle.addEventListener("click", (event) => {
      event.stopPropagation();
      setOpen(kids.hidden);
    });
  }

  // Clicking a name only selects; framing stays on F or the view tools.
  label.addEventListener("click", (event) => {
    const additive = event.ctrlKey || event.metaKey;
    if (spatial) {
      setOpen(true); // the label is a much bigger target than the arrow
      setSelection(branchElements(node), additive);
    } else if (elements.has(node.expressID)) {
      setSelection([node.expressID], additive);
    }
  });
  label.addEventListener("keydown", (event) => {
    if (event.key === "ArrowRight" && children.length) {
      event.preventDefault();
      setOpen(true);
      kids.querySelector(".tree-label")?.focus();
    } else if (event.key === "ArrowLeft" && children.length && !kids.hidden) {
      event.preventDefault();
      setOpen(false);
    } else if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      const visible = [...$("tree").querySelectorAll(".tree-label")]
        .filter((item) => item.offsetParent !== null);
      const index = visible.indexOf(label);
      const target = event.key === "Home" ? 0
        : event.key === "End" ? visible.length - 1
          : Math.min(
            visible.length - 1,
            Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)),
          );
      visible[target]?.focus();
    }
  });
  return li;
}

export function markTreeSelection() {
  for (const label of document.querySelectorAll(".tree-label.selected")) {
    label.classList.remove("selected");
    label.setAttribute("aria-pressed", "false");
  }
  for (const id of selection) {
    const label = document.querySelector(`.tree-label[data-express-id="${id}"]`);
    if (label) {
      label.classList.add("selected");
      label.setAttribute("aria-pressed", "true");
    }
  }
  markSearchSelection();
}
