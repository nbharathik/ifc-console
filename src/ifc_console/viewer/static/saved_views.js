/* Named camera views kept in the browser's UI state, with the list that offers them. */

import { $, el } from "./dom.js";
import { saveUi, uiState } from "./ui_state.js";

let captureView;
let restoreView;

/** The page hands over how a view is captured from the scene and put back. */
export function bindSavedViews(host) {
  ({ captureView, restoreView } = host);
}

// Camera poses live in localStorage next to the panel layout: they belong to
// this browser, not to the model, and survive reloads and model edits.
export const MAX_SAVED_VIEWS = 12;

export function savedViews() {
  return Array.isArray(uiState.views) ? uiState.views : (uiState.views = []);
}

export function renderSavedViews() {
  const box = $("saved-views");
  box.textContent = "";
  const views = savedViews();
  if (!views.length) {
    box.appendChild(el("div", "tool-note empty", "none saved yet"));
    return;
  }
  views.forEach((view, index) => {
    const row = el("div", "saved-view");
    const go = el("button", "tool-btn go", view.name);
    go.title = `Go to ${view.name}`;
    go.addEventListener("click", () => restoreView(view));
    const drop = el("button", "drop", "×");
    drop.title = `Delete ${view.name}`;
    drop.setAttribute("aria-label", `Delete ${view.name}`);
    drop.addEventListener("click", () => {
      views.splice(index, 1);
      saveUi();
      renderSavedViews();
    });
    row.appendChild(go);
    row.appendChild(drop);
    box.appendChild(row);
  });
}

function saveCurrentView() {
  const views = savedViews();
  const input = $("view-name");
  const name = input.value.trim() || `View ${views.length + 1}`;
  const existing = views.findIndex((v) => v.name === name);
  if (existing >= 0) {
    views[existing] = captureView(name);
  } else {
    views.push(captureView(name));
    if (views.length > MAX_SAVED_VIEWS) views.shift();
  }
  input.value = "";
  saveUi();
  renderSavedViews();
}

$("tool-save-view").addEventListener("click", saveCurrentView);
$("view-name").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveCurrentView();
});
