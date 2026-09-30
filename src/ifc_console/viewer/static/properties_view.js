/* The properties panel: one element's IFC data, fetched and drawn as folding tables. */

import { $, el } from "./dom.js";
import { api } from "./session.js";

let expressOf;
let setSelection;
let modelQuery;

/** The page hands over the express-id map and the actions the panel takes. */
export function bindProperties(host) {
  ({ expressOf, setSelection, modelQuery } = host);
}

let propertiesRequest = 0;
export async function showProperties(guid) {
  const request = ++propertiesRequest;
  const panel = $("props");
  panel.textContent = "";
  panel.appendChild(el("p", "hint", "loading…"));
  let detail;
  try {
    const res = await api(`/api/elements/${encodeURIComponent(guid)}${modelQuery()}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    detail = await res.json();
  } catch (err) {
    if (request !== propertiesRequest) return;
    panel.textContent = "";
    panel.appendChild(el("p", "hint", `could not load properties (${err.message})`));
    return;
  }
  if (request !== propertiesRequest) return;
  panel.textContent = "";
  const title = detail.attributes && detail.attributes.Name
    ? String(detail.attributes.Name) : detail.class;
  panel.appendChild(el("h3", null, title));
  panel.appendChild(el("div", "guid", `${detail.class} · ${detail.global_id}`));

  if (detail.container && detail.container.length) {
    const crumb = detail.container.map((c) => c.name || c.class).reverse().join(" / ");
    panel.appendChild(el("div", "crumb", crumb));
  }

  if (detail.attributes) {
    panel.appendChild(sectionTable("Attributes", detail.attributes));
  }
  if (detail.type && detail.type.name) {
    panel.appendChild(sectionTable("Type", { class: detail.type.class, name: detail.type.name }));
  }
  const materials = materialRows(detail.materials);
  if (materials) panel.appendChild(sectionTable("Material", materials));
  if (detail.decomposition && detail.decomposition.length) {
    panel.appendChild(partsList("Parts", detail.decomposition));
  }
  for (const [pset, props] of Object.entries(detail.psets || {})) {
    if (props && typeof props === "object") {
      const { id: _id, ...rest } = props;
      panel.appendChild(sectionTable(pset, rest));
    }
  }
  for (const [qto, props] of Object.entries(detail.qtos || {})) {
    if (props && typeof props === "object") {
      const { id: _id, ...rest } = props;
      panel.appendChild(sectionTable(`${qto} (quantities)`, rest));
    }
  }
}

// element_detail returns one of several material shapes; flatten whichever
// arrived into plain key/value rows.
function materialRows(material) {
  if (!material) return null;
  if (material.kind === "material") return material.name ? { Name: material.name } : null;
  if (material.kind === "layer_set") {
    const rows = {};
    if (material.name) rows["Layer set"] = material.name;
    (material.layers || []).forEach((layer, i) => {
      const thickness = typeof layer.thickness === "number"
        ? ` · ${Number(layer.thickness.toFixed(4))}` : "";
      rows[`Layer ${i + 1}`] = `${layer.name || "?"}${thickness}`;
    });
    return Object.keys(rows).length ? rows : null;
  }
  const list = material.constituents || material.profiles || material.materials;
  if (Array.isArray(list) && list.length) {
    return Object.fromEntries(
      list.filter(Boolean).map((name, i) => [`Material ${i + 1}`, String(name)]));
  }
  return null;
}

// Parts are navigable, so they are links into the model rather than a table.
function partsList(titleText, parts) {
  const details = el("details");
  details.open = false;
  details.appendChild(el("summary", null, `${titleText} (${parts.length})`));
  const list = el("div", "part-list");
  for (const part of parts) {
    const id = expressOf.get(part.global_id);
    const row = el(id !== undefined ? "button" : "div", "part-row", `${part.name || part.class}`);
    row.appendChild(el("span", "cls", ` ${part.class}`));
    if (id !== undefined) {
      row.type = "button";
      row.classList.add("clickable");
      row.title = "Select this part";
      row.addEventListener("click", () => setSelection([id], false));
    }
    list.appendChild(row);
  }
  details.appendChild(list);
  return details;
}

function sectionTable(titleText, obj) {
  // each section folds, so long property lists stay scannable
  const details = el("details");
  details.open = true;
  details.appendChild(el("summary", null, titleText));
  const table = el("table");
  for (const [key, value] of Object.entries(obj)) {
    if (value === null || value === undefined || value === "") continue;
    const tr = el("tr");
    tr.appendChild(el("td", null, key));
    tr.appendChild(el("td", null,
      typeof value === "object" ? JSON.stringify(value) : String(value)));
    table.appendChild(tr);
  }
  details.appendChild(table);
  return details;
}

export function clearProperties() {
  propertiesRequest++;
  const panel = $("props");
  panel.textContent = "";
  panel.appendChild(el("p", "hint", "Select an element to inspect its IFC data."));
}
