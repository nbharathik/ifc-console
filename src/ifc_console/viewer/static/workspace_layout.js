/* The model and properties panels and the assistant dock: widths, open state, compact layout, lazy mount. */

import { $, showOverlay } from "./dom.js";
import { requestedPanel } from "./session.js";
import { saveUi, uiState } from "./ui_state.js";

let isViewerOpen;
let resize;
let scheduleViewerContext;
let syncViewerSurface;
let viewerComponentHost;

export let treePanelController = null;
export let propsPanelController = null;
export let applyTreePanel = null;
export let applyPropsPanel = null;

/**
 * The page hands over what the layout needs from it, then the panels start.
 *
 * Nothing here runs against the page's state before this call, so the order
 * of imports never matters.
 */
export function bindWorkspaceLayout(host) {
  ({ isViewerOpen, resize, scheduleViewerContext, syncViewerSurface, viewerComponentHost } = host);
  treePanelController =
    initSidePanel(
      "tree-panel",
      "split-tree",
      "tree-panel-tab",
      "tree-panel-close",
      "treeWidth",
      "treeOpen",
      "left",
      window.innerWidth > 620,
    );
  propsPanelController = initSidePanel(
    "props-panel",
    "split-props",
    "props-panel-tab",
    "props-panel-close",
    "propsWidth",
    "propsOpen",
    "right",
    false,
  );
  applyTreePanel = treePanelController.apply;
  applyPropsPanel = propsPanelController.apply;
  reconcileCompactLayout();
}

function effectiveViewerWidth() {
  const dock = document.getElementById("chat-dock");
  const chatWidth = dock && !dock.hidden ? dock.getBoundingClientRect().width : 0;
  return window.innerWidth - chatWidth;
}

function syncPanelScrim() {
  const compact = effectiveViewerWidth() <= 620;
  const sidePanelOpen = !$("tree-panel").classList.contains("collapsed")
    || !$("props-panel").classList.contains("collapsed");
  $("panel-scrim").hidden = !compact || !sidePanelOpen;
}

function closeOtherCompactPanel(openKey) {
  if (effectiveViewerWidth() > 620 || uiState[openKey] !== true) return;
  const dock = document.getElementById("chat-dock");
  if (dock && !dock.hidden) setChat(false);
  if (openKey === "treeOpen" && uiState.propsOpen === true) {
    uiState.propsOpen = false;
    applyPropsPanel();
  } else if (openKey === "propsOpen" && uiState.treeOpen === true) {
    uiState.treeOpen = false;
    applyTreePanel();
  }
}

function initSidePanel(
  panelId,
  splitId,
  tabId,
  closeId,
  widthKey,
  openKey,
  side,
  openByDefault,
) {
  const panel = $(panelId);
  const splitter = $(splitId);
  const tab = $(tabId);
  const close = $(closeId);
  const clampW = (w) => {
    // Keep a useful canvas visible when both side panels are open.
    const max = Math.max(
      160,
      Math.min(window.innerWidth * 0.45, (window.innerWidth - 280) / 2),
    );
    return Math.min(Math.max(Math.round(w), 160), max);
  };
  // Properties start closed: an empty panel should not cost the 3D view 320px.
  const isOpen = () => uiState[openKey] ?? openByDefault;
  const setWidth = (width) => {
    const value = clampW(width);
    uiState[widthKey] = value;
    panel.style.width = `${value}px`;
    splitter.setAttribute("aria-valuemin", "160");
    splitter.setAttribute("aria-valuemax", String(clampW(window.innerWidth)));
    splitter.setAttribute("aria-valuenow", String(value));
    splitter.setAttribute("aria-valuetext", `${value} pixels`);
  };
  const apply = () => {
    const open = isOpen();
    const chatDockElement = document.getElementById("chat-dock");
    const chatCoversLeftTab = panelId === "tree-panel"
      && window.innerWidth <= 1040
      && chatDockElement
      && !chatDockElement.hidden;
    panel.classList.toggle("collapsed", !open);
    panel.inert = !open;
    splitter.classList.toggle("collapsed", !open);
    tab.hidden = open || chatCoversLeftTab;
    tab.setAttribute("aria-expanded", String(open));
    if (uiState[widthKey]) setWidth(uiState[widthKey]);
    else {
      panel.style.width = "";
      const fallback = panelId === "tree-panel" ? 260 : 320;
      const value = clampW(fallback);
      splitter.setAttribute("aria-valuemin", "160");
      splitter.setAttribute("aria-valuemax", String(clampW(window.innerWidth)));
      splitter.setAttribute("aria-valuenow", String(value));
      splitter.setAttribute("aria-valuetext", `${value} pixels`);
    }
    syncPanelScrim();
    scheduleViewerContext("panels");
  };
  const setOpen = (open, { focus = true } = {}) => {
    uiState[openKey] = Boolean(open);
    if (open) closeOtherCompactPanel(openKey);
    saveUi();
    apply();
    if (focus) {
      if (open) close.focus({ preventScroll: true });
      else tab.focus({ preventScroll: true });
    }
  };
  splitter.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    splitter.setPointerCapture(e.pointerId);
    splitter.classList.add("dragging");
    const startX = e.clientX;
    const startW = panel.getBoundingClientRect().width;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      setWidth(side === "left" ? startW + dx : startW - dx);
    };
    const up = (ev) => {
      splitter.classList.remove("dragging");
      splitter.releasePointerCapture(ev.pointerId);
      splitter.removeEventListener("pointermove", move);
      splitter.removeEventListener("pointerup", up);
      saveUi();
    };
    splitter.addEventListener("pointermove", move);
    splitter.addEventListener("pointerup", up);
  });
  splitter.addEventListener("dblclick", () => {
    delete uiState[widthKey];
    saveUi();
    apply();
  });
  splitter.addEventListener("keydown", (event) => {
    if (event.key === "Home") {
      event.preventDefault();
      delete uiState[widthKey];
      saveUi();
      apply();
      return;
    }
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const current = uiState[widthKey]
      || panel.getBoundingClientRect().width
      || (panelId === "tree-panel" ? 260 : 320);
    const movement = event.key === "ArrowRight" ? 16 : -16;
    setWidth(current + (side === "left" ? movement : -movement));
    saveUi();
    resize();
  });
  tab.addEventListener("click", () => setOpen(true));
  close.addEventListener("click", () => setOpen(false));
  window.addEventListener("resize", apply);
  apply();
  return { apply, isOpen, setOpen };
}

$("panel-scrim").addEventListener("click", () => {
  const restore = propsPanelController.isOpen() ? $("props-panel-tab") : $("tree-panel-tab");
  uiState.treeOpen = false;
  uiState.propsOpen = false;
  saveUi();
  applyTreePanel();
  applyPropsPanel();
  restore.focus({ preventScroll: true });
});

// The panel is a separate component and only loaded when its launcher names
// it. A /viewer tab therefore pays no Agent JavaScript, CSS, layout or memory.
export const chatDock = $("chat-dock");
export const chatResize = $("chat-dock-resize");
export const chatBtn = $("btn-chat");
export const extensionPanelPrimary = Boolean(requestedPanel);
const CHAT_DOCK_MIN_WIDTH = 520;
const CHAT_DOCK_DEFAULT_WIDTH = 720;
const CHAT_CANVAS_MIN_WIDTH = 420;
const CHAT_DOCK_RESIZE_WIDTH = 5;
const CHAT_DOCK_OVERLAY_WIDTH = 1040;
export let chatPanel = null;
let chatLoadPromise = null;
let chatPanelDefinition = null;
let chatDesiredOpen = extensionPanelPrimary;
let chatRequestVersion = 0;

function loadPanelStylesheet(url) {
  if (!url || document.querySelector(`link[data-extension-style="${url}"]`)) return;
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = url;
  link.dataset.extensionStyle = url;
  document.head.append(link);
}

function visiblePanelFootprint(panelId, splitterId) {
  const panel = $(panelId);
  if (panel.classList.contains("collapsed")) return 0;
  return panel.getBoundingClientRect().width
    + $(splitterId).getBoundingClientRect().width;
}

function availableChatDockWidth() {
  const layoutWidth = $("layout").getBoundingClientRect().width || window.innerWidth;
  return Math.floor(
    layoutWidth
      - visiblePanelFootprint("tree-panel", "split-tree")
      - visiblePanelFootprint("props-panel", "split-props")
      - CHAT_CANVAS_MIN_WIDTH
      - CHAT_DOCK_RESIZE_WIDTH,
  );
}

function chatDockMaxWidth() {
  const viewportCap = Math.round(window.innerWidth * 0.68);
  // Below this breakpoint the dock overlays the viewer and therefore does not
  // consume a canvas flex track. On desktop, account for every visible viewer
  // panel before allowing the chat to grow.
  if (window.innerWidth <= CHAT_DOCK_OVERLAY_WIDTH) {
    return Math.max(CHAT_DOCK_MIN_WIDTH, viewportCap);
  }
  return Math.max(
    CHAT_DOCK_MIN_WIDTH,
    Math.min(viewportCap, availableChatDockWidth()),
  );
}

function currentChatWidth() {
  return uiState.chatWidth
    || chatDock.getBoundingClientRect().width
    || CHAT_DOCK_DEFAULT_WIDTH;
}

export function syncChatResizeAria(width = currentChatWidth()) {
  const max = chatDockMaxWidth();
  const value = Math.min(Math.max(Math.round(width), CHAT_DOCK_MIN_WIDTH), max);
  chatResize.setAttribute("aria-valuemin", String(CHAT_DOCK_MIN_WIDTH));
  chatResize.setAttribute("aria-valuemax", String(max));
  chatResize.setAttribute("aria-valuenow", String(value));
  chatResize.setAttribute("aria-valuetext", `${value} pixels`);
  return value;
}

function setChatWidth(width) {
  const value = syncChatResizeAria(width);
  chatDock.style.width = `${value}px`;
  uiState.chatWidth = value;
}

export function applyChatWidthForViewport() {
  if (window.innerWidth <= CHAT_DOCK_OVERLAY_WIDTH) {
    chatDock.style.width = "";
    syncChatResizeAria();
  } else if (uiState.chatWidth) {
    setChatWidth(uiState.chatWidth);
  } else {
    syncChatResizeAria();
  }
}

function setChatPanelVisible(visible) {
  if (chatPanel && typeof chatPanel.setVisible === "function") {
    chatPanel.setVisible(Boolean(visible));
  }
}

function applyChatChrome(open) {
  // Let the panel dismiss transient UI while it can still measure its host.
  if (!open) setChatPanelVisible(false);
  document.body.classList.toggle("chat-open", open);
  chatDock.hidden = !open;
  chatResize.hidden = !open || !isViewerOpen();
  chatBtn.setAttribute("aria-pressed", String(open));
  if (isViewerOpen()) applyChatWidthForViewport();
  else chatDock.style.width = "";
  applyTreePanel();
  if (open) setChatPanelVisible(true);
  syncViewerSurface();
}

export function closePanelsForChat(force = false) {
  let changed = false;
  const propsOpen = propsPanelController.isOpen();
  if (
    propsOpen
    && (force || availableChatDockWidth() < CHAT_DOCK_MIN_WIDTH)
  ) {
    uiState.propsOpen = false;
    applyPropsPanel();
    changed = true;
  }

  const treeOpen = treePanelController.isOpen();
  if (
    treeOpen
    && (force || availableChatDockWidth() < CHAT_DOCK_MIN_WIDTH)
  ) {
    uiState.treeOpen = false;
    applyTreePanel();
    changed = true;
  }
  return changed;
}

export async function setChat(open, { force = false } = {}) {
  if (extensionPanelPrimary && !force) open = true;
  const requestVersion = ++chatRequestVersion;
  chatDesiredOpen = Boolean(open);
  uiState.chatOpen = chatDesiredOpen;
  if (chatDesiredOpen && !chatPanelDefinition) {
    applyChatChrome(false);
    return;
  }
  // three panels plus the 3D view do not fit a normal window; the properties
  // panel is the one the chat replaces, so fold it away rather than letterbox
  // the model.
  if (chatDesiredOpen && isViewerOpen()) closePanelsForChat(true);
  applyChatChrome(chatDesiredOpen);
  saveUi();
  resize();
  if (!chatDesiredOpen) return;

  try {
    if (!chatPanel) {
      loadPanelStylesheet(chatPanelDefinition.stylesheet_url);
      chatLoadPromise ||= import(chatPanelDefinition.module_url)
        .then((module) => {
          const mountPanel = module.mountPanel || module.mountChat;
          if (typeof mountPanel !== "function") {
            throw new TypeError("extension panel module has no mountPanel export");
          }
          chatPanel ||= mountPanel(chatDock, { viewer: viewerComponentHost.api });
          return chatPanel;
        })
        .finally(() => { chatLoadPromise = null; });
      await chatLoadPromise;
      // Opening and closing can race the lazy import. Reconcile the mounted
      // panel with the latest request before the stale caller returns.
      setChatPanelVisible(chatDesiredOpen);
    }
  } catch (error) {
    console.error("[ifc-console] chat module failed", error);
    if (requestVersion === chatRequestVersion && chatDesiredOpen) {
      chatDesiredOpen = false;
      uiState.chatOpen = false;
      applyChatChrome(false);
      saveUi();
      resize();
      showOverlay(
        "Could not open the assistant",
        "The local chat module did not load.",
        { label: "Try again", run: () => setChat(true) },
        "error",
      );
    }
    return;
  }
  if (requestVersion !== chatRequestVersion || !chatDesiredOpen || !chatPanel) return;
  chatPanel.focus();
}

function reconcileCompactLayout() {
  let changed = chatDesiredOpen && isViewerOpen() ? closePanelsForChat() : false;
  if (isViewerOpen()) applyChatWidthForViewport();
  else chatDock.style.width = "";
  if (window.innerWidth > 620) {
    if (changed) saveUi();
    syncPanelScrim();
    return;
  }
  const treeOpen = treePanelController.isOpen();
  const propsOpen = propsPanelController.isOpen();
  let compactChanged = false;
  if (chatDesiredOpen) {
    if (treeOpen) {
      uiState.treeOpen = false;
      compactChanged = true;
    }
    if (propsOpen) {
      uiState.propsOpen = false;
      compactChanged = true;
    }
  } else if (treeOpen && propsOpen) {
    uiState.propsOpen = false;
    compactChanged = true;
  }
  if (compactChanged) {
    applyTreePanel();
    applyPropsPanel();
    changed = true;
  }
  if (changed) saveUi();
  syncPanelScrim();
}

// A viewer session with no assistant extension should not offer a button that
// opens a dead panel. The extension manifest supplies its module and styling;
// core never needs to know where the companion package stores those assets.
export function setChatAvailable(panel, enabled) {
  const available = Boolean(
    requestedPanel && panel?.name === requestedPanel && enabled,
  );
  chatPanelDefinition = available ? panel : null;
  chatBtn.hidden = !available;
  if (panel?.label) {
    chatBtn.title = `Toggle ${panel.label.toLowerCase()} panel (C)`;
    chatBtn.setAttribute("aria-label", `Toggle the ${panel.label.toLowerCase()} panel`);
  }
  if (!available && chatDesiredOpen) setChat(false, { force: true });
  else if (available && chatDesiredOpen) void setChat(true);
  syncViewerSurface();
}

chatBtn.addEventListener("click", () => {
  if (extensionPanelPrimary && !chatDock.hidden) chatPanel?.focus();
  else setChat(chatDock.hidden);
});

chatResize.addEventListener("pointerdown", (e) => {
  e.preventDefault();
  chatResize.setPointerCapture(e.pointerId);
  const startX = e.clientX;
  const startWidth = chatDock.getBoundingClientRect().width;
  const move = (ev) => {
    setChatWidth(startWidth + (ev.clientX - startX));
    resize();
  };
  const up = () => {
    chatResize.removeEventListener("pointermove", move);
    chatResize.removeEventListener("pointerup", up);
    saveUi();
  };
  chatResize.addEventListener("pointermove", move);
  chatResize.addEventListener("pointerup", up);
});

chatResize.addEventListener("keydown", (event) => {
  if (event.key === "Home") {
    event.preventDefault();
    delete uiState.chatWidth;
    chatDock.style.width = "";
    syncChatResizeAria(CHAT_DOCK_DEFAULT_WIDTH);
    saveUi();
    resize();
    return;
  }
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  event.preventDefault();
  const movement = event.key === "ArrowLeft" ? -16 : 16;
  setChatWidth(currentChatWidth() + movement);
  saveUi();
  resize();
});

// Side-panel toggles and drags change the chat's safe maximum without a
// viewport resize. Re-clamp before paint so they cannot squeeze away the 3D
// canvas or leave the dock wider than its current workspace permits.
const chatLayoutObserver = new ResizeObserver(() => {
  if (!chatDesiredOpen || !isViewerOpen()) return;
  const changed = closePanelsForChat();
  applyChatWidthForViewport();
  if (changed) saveUi();
  resize();
});
chatLayoutObserver.observe($("tree-panel"));
chatLayoutObserver.observe($("props-panel"));

window.addEventListener("resize", reconcileCompactLayout);
