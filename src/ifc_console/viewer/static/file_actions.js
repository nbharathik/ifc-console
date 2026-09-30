/* The topbar file actions: save, undo, redo, download, the working-copy badge, and the status note. */

import { $ } from "./dom.js";
import { api } from "./session.js";

let isViewerOpen;
let pinnedModelId;

/** The page says whether a model tab is open and which model this tab pins. */
export function bindFileActions(host) {
  ({ isViewerOpen, pinnedModelId } = host);
}

let noteTimer = 0;

/* One line in the status bar; it clears itself so nothing goes stale. */
function toast(text, isError = false) {
  const note = $("action-note");
  if (!note) return;
  note.textContent = text;
  note.dataset.error = isError ? "1" : "0";
  clearTimeout(noteTimer);
  noteTimer = setTimeout(() => {
    note.textContent = "";
    note.dataset.error = "0";
  }, isError ? 12000 : 6000);
}

// Edit mode works in a copy of the opened file, so writing it costs the user
// nothing: the topbar offers the save and says how much is waiting for it.
let modelChanges = 0;
let workingCopy = null;

export function setWorkingCopy(copy, origin) {
  workingCopy = copy;
  const badge = $("copy-badge");
  badge.hidden = !copy;
  if (copy) {
    badge.title = `Editing a copy: ${copy.name}. `
      + `${copy.origin_name || origin || "the file you opened"} is not written.`;
  }
  updateSaveControls();
}

export function setModelChanges(count) {
  modelChanges = Number(count) || 0;
  updateSaveControls();
}

function updateSaveControls() {
  const save = $("btn-save-model");
  const download = $("btn-download-model");
  const badge = $("save-count");
  if (!save) return;
  const dirty = !$("dirty").hidden;
  save.hidden = !dirty;
  download.hidden = !isViewerOpen();
  badge.hidden = modelChanges === 0;
  badge.textContent = String(modelChanges);
  const target = workingCopy ? workingCopy.name : ($("model-name").textContent || "the IFC file");
  const scope = modelChanges
    ? `${modelChanges} change${modelChanges === 1 ? "" : "s"}`
    : "the in-memory changes";
  save.title = workingCopy
    ? `Write ${scope} to the working copy ${target}. `
      + `${workingCopy.origin_name || "the file you opened"} stays untouched.`
    : `Write ${scope} to ${target}.`;
}

async function saveModelFile() {
  const save = $("btn-save-model");
  const label = $("save-label");
  const previous = label.textContent;
  save.disabled = true;
  label.textContent = "Saving...";
  try {
    const response = await api("/api/model/save", { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    setModelChanges(0);
    $("dirty").hidden = true;
    toast(payload.saved
      ? `Saved ${payload.working_copy ? "the working copy" : ""} ${payload.path}`.trim()
      : "Nothing to save");
  } catch (exc) {
    toast(`Could not save: ${exc.message || exc}`, true);
  } finally {
    save.disabled = false;
    label.textContent = previous;
    updateSaveControls();
  }
}

/** Show Undo and Redo only while there is something for them to do. */
export function setHistoryControls(frame) {
  if (frame.can_undo === undefined && frame.can_redo === undefined) return;
  const undo = $("btn-undo");
  const redo = $("btn-redo");
  undo.hidden = !frame.can_undo;
  redo.hidden = !frame.can_redo;
  undo.title = frame.undo_label ? `Undo: ${frame.undo_label}` : "Undo the last edit";
  redo.title = frame.redo_label ? `Redo: ${frame.redo_label}` : "Redo the edit you undid";
}

async function stepHistory(direction) {
  const button = $(direction === "undo" ? "btn-undo" : "btn-redo");
  button.disabled = true;
  try {
    const response = await api(`/api/model/${direction}`, { method: "POST" });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    }
    const what = payload.step?.description || "the edit";
    toast(`${direction === "undo" ? "Undid" : "Redid"} ${what}`);
  } catch (exc) {
    toast(`Could not ${direction}: ${exc.message || exc}`, true);
  } finally {
    button.disabled = false;
  }
}

/* Stream what is in memory now, without writing anything. */
async function downloadModelFile() {
  const button = $("btn-download-model");
  button.disabled = true;
  try {
    const params = new URLSearchParams({ download: "1" });
    if (pinnedModelId()) params.set("model", pinnedModelId());
    const response = await api(`/api/model.ifc?${params}`);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const disposition = response.headers.get("content-disposition") || "";
    const named = /filename="([^"]+)"/.exec(disposition);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = named ? named[1] : "model.ifc";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  } catch (exc) {
    toast(`Could not download: ${exc.message || exc}`, true);
  } finally {
    button.disabled = false;
  }
}

$("btn-save-model").addEventListener("click", () => { void saveModelFile(); });
$("btn-download-model").addEventListener("click", () => { void downloadModelFile(); });
$("btn-undo").addEventListener("click", () => { void stepHistory("undo"); });
$("btn-redo").addEventListener("click", () => { void stepHistory("redo"); });
