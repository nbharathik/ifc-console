/* The model panel search: server-side matching, result rows, and their selection marks. */

import { $, el } from "./dom.js";
import { api } from "./session.js";

let elements;
let expressOf;
let selection;
let setSelection;
let fitTo;
let isolateOnly;
let modelQuery;

/** The page hands over the scene state the search reads and the actions it takes. */
export function bindSearch(host) {
  ({ elements, expressOf, selection, setSelection, fitTo, isolateOnly, modelQuery } = host);
}

// The server does the matching: the client only ever learns GlobalIds and
// express ids for the geometry it drew, never names or types.
const SEARCH_DEBOUNCE = 250;
let searchTimer = null;
let searchRequest = 0;
let searchAbort = null;
let searchHits = [];  // expressIDs of the current result set, in row order

function searchIds() {
  return searchHits.filter((id) => elements.has(id));
}

function cancelPendingSearch() {
  searchRequest++;
  if (searchTimer !== null) clearTimeout(searchTimer);
  searchTimer = null;
  if (searchAbort) searchAbort.abort();
  searchAbort = null;
}

function resetSearchResults() {
  searchHits = [];
  const box = $("search-results");
  box.hidden = true;
  box.textContent = "";
  box.setAttribute("aria-busy", "false");
  $("tree").hidden = false;
}

function clearSearch(refocus) {
  cancelPendingSearch();
  $("search-input").value = "";
  $("search-clear").hidden = true;
  resetSearchResults();
  if (refocus) $("search-input").focus();
}

function renderSearch(payload) {
  const box = $("search-results");
  box.textContent = "";
  box.setAttribute("aria-busy", "false");
  searchHits = [];

  const head = el("div", "search-head");
  const found = payload.truncated
    ? `${payload.results.length} of ${payload.total}`
    : `${payload.total} match${payload.total === 1 ? "" : "es"}`;
  head.appendChild(el("span", null, found));
  head.appendChild(el("span", "spacer"));
  const selectAll = el("button", null, "Select");
  selectAll.title = "Select every element in the result list";
  const isolate = el("button", null, "Isolate");
  isolate.title = "Show only the elements in the result list";
  head.appendChild(selectAll);
  head.appendChild(isolate);
  box.appendChild(head);

  if (!payload.total) {
    box.appendChild(el("p", "hint", "No elements match. Try an IFC class such as IfcDoor."));
  }

  for (const row of payload.results) {
    const id = expressOf.get(row.global_id);
    if (id !== undefined) searchHits.push(id);
    const hit = el("button", "search-hit");
    hit.type = "button";
    hit.appendChild(el("span", "name", row.name || row.class));
    const detail = [row.class, row.storey, row.type_name].filter(Boolean).join(" · ");
    hit.appendChild(el("span", "meta-line", detail));
    if (id === undefined) {
      hit.disabled = true;
      hit.title = "No geometry in this model";
    } else {
      hit.dataset.expressId = id;
      hit.setAttribute("aria-pressed", "false");
      hit.title = "Select this element. Press Enter to select and zoom.";
      hit.addEventListener("click", () => setSelection([id], false));
      hit.addEventListener("dblclick", () => fitTo([id]));
      hit.addEventListener("keydown", (event) => {
        if (event.key !== "Enter") return;
        event.preventDefault();
        setSelection([id], false);
        fitTo([id]);
      });
    }
    box.appendChild(hit);
  }

  const ids = searchIds();
  selectAll.disabled = isolate.disabled = ids.length === 0;
  selectAll.addEventListener("click", () => setSelection(searchIds(), false));
  isolate.addEventListener("click", () => {
    const targets = searchIds();
    if (!targets.length) return;
    isolateOnly(targets);
  });

  markSearchSelection();
  box.hidden = false;
  $("tree").hidden = true;
}

export function markSearchSelection() {
  for (const hit of document.querySelectorAll(".search-hit")) {
    const id = Number(hit.dataset.expressId);
    const selected = selection.has(id);
    hit.classList.toggle("selected", selected);
    if (!hit.disabled) hit.setAttribute("aria-pressed", String(selected));
  }
}

async function runSearch(term) {
  if (searchAbort) searchAbort.abort();
  const request = ++searchRequest;
  const controller = new AbortController();
  searchAbort = controller;
  const box = $("search-results");
  box.textContent = "";
  box.setAttribute("aria-busy", "true");
  box.appendChild(el("p", "hint", "Searching elements…"));
  box.hidden = false;
  $("tree").hidden = true;
  let payload;
  try {
    const res = await api(
      `/api/search?q=${encodeURIComponent(term)}${modelQuery().replace("?", "&")}`,
      { signal: controller.signal },
    );
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    payload = await res.json();
  } catch (err) {
    if (request !== searchRequest || err.name === "AbortError") return;
    box.textContent = "";
    box.setAttribute("aria-busy", "false");
    box.appendChild(el("p", "hint", `Search failed (${err.message}). Press Enter to try again.`));
    return;
  } finally {
    if (searchAbort === controller) searchAbort = null;
  }
  if (request !== searchRequest) return;
  renderSearch(payload);
}

$("search-input").addEventListener("input", () => {
  const term = $("search-input").value.trim();
  $("search-clear").hidden = !term;
  cancelPendingSearch();
  if (term.length < 2) {
    resetSearchResults();
    return;
  }
  searchTimer = setTimeout(() => {
    searchTimer = null;
    runSearch(term);
  }, SEARCH_DEBOUNCE);
});

$("search-input").addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    e.stopPropagation();
    clearSearch(true);
  } else if (e.key === "Enter") {
    if (searchTimer !== null) clearTimeout(searchTimer);
    searchTimer = null;
    const term = $("search-input").value.trim();
    cancelPendingSearch();
    if (term.length >= 2) runSearch(term);
    else resetSearchResults();
  }
});

$("search-clear").addEventListener("click", () => clearSearch(true));

export function refreshSearch() {
  cancelPendingSearch();
  const term = $("search-input").value.trim();
  if (term.length >= 2) runSearch(term);
  else resetSearchResults();
}
