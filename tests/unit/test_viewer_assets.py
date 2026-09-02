"""Static checks on the viewer bundle.

There is no browser in the test environment, so these stand in for the one
class of bug that would otherwise only show up as a blank panel: markup and
script drifting apart.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ifc_console.viewer import assets

STATIC = assets.require_static_dir()
@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def script() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def component_js() -> str:
    return (STATIC / "viewer_component.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def worker_js() -> str:
    return (STATIC / "worker.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def measure_math() -> str:
    """The arithmetic app.js imports. Its numbers are tested for real in
    tests/ui/measure.test.mjs; what is checked here is that it stays pure."""
    return (STATIC / "measure_math.js").read_text(encoding="utf-8")


def test_every_element_the_script_looks_up_exists(html: str, script: str) -> None:
    ids = set(re.findall(r'id="([^"]+)"', html))
    looked_up = set(re.findall(r'\$\("([^"]+)"\)', script))
    assert not looked_up - ids, (
        f"app.js reads ids that index.html does not define: {looked_up - ids}"
    )


def test_panel_ids_passed_as_strings_exist(html: str, script: str) -> None:
    """initSidePanel and POPOVERS name their elements in string literals."""
    ids = set(re.findall(r'id="([^"]+)"', html))
    for name in (
        "tree-panel",
        "props-panel",
        "split-tree",
        "split-props",
        "settings-panel",
        "help-panel",
        "tools-panel",
    ):
        assert name in ids


def test_side_panels_use_contextual_close_controls_and_edge_tabs(
    html: str, script: str, styles: str
) -> None:
    assert 'id="btn-panel-tree"' not in html
    assert 'id="btn-panel-props"' not in html
    for side in ("tree", "props"):
        assert f'id="{side}-panel-close"' in html
        assert f'id="{side}-panel-tab"' in html
        assert f'aria-controls="{side}-panel"' in html
    panel = script.split("function initSidePanel(", 1)[1].split(
        "const treePanelController", 1
    )[0]
    assert "panel.inert = !open" in panel
    assert "tab.hidden = open || chatCoversLeftTab" in panel
    assert 'tab.setAttribute("aria-expanded", String(open))' in panel
    assert 'close.addEventListener("click", () => setOpen(false))' in panel
    assert "effectiveViewerWidth() > 620" in script
    assert 'if (dock && !dock.hidden) setChat(false);' in script
    assert ".panel-edge-tab" in styles
    assert "top: 50%" in styles


def test_view_tools_live_in_a_persistent_top_instrument_rail(
    html: str, script: str, styles: str
) -> None:
    assert 'id="viewer-rail"' in html
    assert 'id="viewer-toolbar"' in html
    for name in ("visibility", "views", "section", "display", "filters"):
        assert f'data-tool-panel="{name}"' in html
    assert 'id="btn-tool-measure"' in html
    assert 'id="tools-panel-close"' in html
    panel = script.split("function setToolPanel", 1)[1].split(
        "function closePopovers", 1
    )[0]
    assert "panel.hidden = !valid" in panel
    assert "sectionNode.hidden = !valid" in panel
    # Canvas interaction does not dismiss a tool panel; only its icon, close
    # button, Escape, or another explicit popover does.
    outside = script.split('document.addEventListener("click"', 1)[1].split(
        chr(10) + "});", 1
    )[0]
    assert "setToolPanel" not in outside
    assert '$("tools-panel-close").addEventListener("click"' in script
    rail_styles = re.findall(r"(?m)^\.rail-tool \{([^}]*)\}", styles)
    assert any("width: 32px" in rule for rule in rail_styles)
    assert ".rail-tool > span { display: none; }" in styles


def test_ifc_documents_use_closeable_tabs_with_per_tab_views(
    html: str, script: str
) -> None:
    assert 'id="model-tabs" role="tablist"' in html
    assert 'id="model-tab-add"' in html
    assert 'id="model-tab-open-active"' in html
    assert 'id="viewer-empty-open"' in html
    render = script.split("function renderModelTabs()", 1)[1].split(
        "const modelTabAdd", 1
    )[0]
    assert 'open.setAttribute("role", "tab")' in render
    assert 'open.setAttribute("aria-selected"' in render
    assert 'el("button", "model-tab-close"' in render
    assert "closedModelTabs.add(row.id)" in render
    assert "Keep at least one IFC file open" not in render
    assert "closeViewerSurface({ openAgent: true })" in render
    assert "for (const row of modelRows)" in render
    assert '"model-tab-choice-state"' in render
    switch = script.split("function selectViewerModel(picked)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "viewerDocumentOpen = true" in switch
    assert "modelTabViews.set(current.id, captureView(current.name))" in switch
    assert "pendingModelTabView" in switch
    rebuild = script.split("async function buildScene(buffer)", 1)[1].split(
        "// ---------------------------------------------------------------- spatial tree", 1
    )[0]
    assert "const switchingTabs" in rebuild
    assert "if (saved) restoreView(saved)" in rebuild






def test_filter_panel_can_restore_the_default_ifc_view(html: str, script: str) -> None:
    assert 'data-tool-panel="filters"' in html
    assert 'id="filter-count"' in html
    assert 'id="tool-clear-filters"' in html
    active = script.split("function activeViewerFilters()", 1)[1].split(
        "function renderViewerFilters", 1
    )[0]
    for key in (
        "selection", "transparency", "visibility", "section",
        "measurements", "highlights", "theme", "projection",
    ):
        assert f'key: "{key}"' in active
    clear = script.split("function clearAllViewerFilters()", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    for reset in (
        "setSelection([], false)", "setGhostContext(false)", "showEverything()",
        "clearSections()", "clearMeasurements()", 'setProjection("perspective")',
    ):
        assert reset in clear


def test_viewer_component_exposes_context_commands_and_async_results(
    script: str, component_js: str
) -> None:
    for event_name in (
        "ifc-console:viewer-context",
        "ifc-console:viewer-command",
        "ifc-console:viewer-result",
    ):
        assert event_name in component_js
    context = script.split("function viewerContext(", 1)[1].split(
        "function scheduleViewerContext", 1
    )[0]
    for field in ("model:", "models:", "selection:", "mode:", "theme:", "capabilities:"):
        assert field in context
    # one dispatch, reachable from the panel and from the server
    assert "function runViewerCommand(command)" in script
    for action in (
        "get-context",
        "set-theme",
        "set-model",
        "set-panel",
        "capture-evidence",
    ):
        assert f'command.action === "{action}"' in script
    assert "createViewerComponent({" in script
    assert "execute: runViewerCommand" in script
    assert "Promise.resolve().then(() => execute(command))" in component_js
    assert "target.addEventListener(VIEWER_COMMAND_EVENT, handleLegacyCommand)" in component_js
    assert "publishResult(command, true, await api.execute(command))" in component_js
    assert "subscribeResults(listener)" in component_js
    # the failure carries the frame it came from: a command that dies inside
    # three.js says nothing useful without it
    assert "publishResult(command, false, null, failureText(error))" in component_js
    assert 'split(String.fromCharCode(10))[1]?.trim()' in component_js


def test_theme_preference_is_persisted_and_resolves_through_workspace(
    html: str, script: str, styles: str
) -> None:
    assert 'id="set-theme"' in html
    for choice in ("light", "dark", "modern", "blue"):
        assert f'value="{choice}"' in html
    theme = script.split("function resolvedTheme()", 1)[1].split(
        "// ---------------------------------------------------------------- model state", 1
    )[0]
    assert "consoleTheme" in theme
    assert 'return "blue"' in theme
    assert "uiState.themePreference = themePreference" in script
    assert ':root[data-theme="light"]' in styles


def test_evidence_capture_carries_model_and_selection_context(script: str) -> None:
    capture = script.split("function captureViewerEvidence(options = {})", 1)[1].split(
        "function handleScreenshot", 1
    )[0]
    for field in (
        'kind: "viewer-screenshot"',
        "modelId:",
        "modelName:",
        "selectionGuids: selectedGuids()",
        "capturedAt:",
        "camera:",
        "dataUrl,",
        "width,",
        "height,",
    ):
        assert field in capture


def test_search_and_saved_view_controls_are_present(html: str) -> None:
    for name in (
        "search-input",
        "search-clear",
        "search-results",
        "saved-views",
        "view-name",
        "tool-save-view",
    ):
        assert f'id="{name}"' in html, f"{name} is missing from index.html"


def test_viewport_and_splitters_are_keyboard_operable(html: str, script: str) -> None:
    assert re.search(r'<canvas id="canvas"[^>]*tabindex="0"', html)
    for name in ("split-tree", "split-props", "chat-dock-resize"):
        assert re.search(rf'id="{name}"[^>]*role="separator"[^>]*tabindex="0"', html)
    assert "controls.listenToKeyEvents(canvas)" in script
    assert 'event.key !== "ArrowLeft"' in script
    assert 'event.key !== "ArrowRight"' in script
    assert 'aria-controls="chat-dock"' in html




def test_dynamic_model_navigation_uses_native_controls(script: str) -> None:
    assert 'el("button", "tree-label")' in script
    assert 'el("button", "tree-toggle"' in script
    assert 'el("button", "search-hit")' in script
    assert 'label.addEventListener("keydown"' in script


def test_viewer_states_are_announced_and_recoverable(html: str, script: str) -> None:
    assert 'id="overlay"' in html and 'aria-live="polite"' in html
    assert 'id="live"' in html and 'class="connection-label"' in html
    assert '{ label: "Try again", run: () => loadModel() }' in script
    assert 'motionPreference.addEventListener("change"' in script


def test_extension_panel_loading_is_explicit_single_flight_and_viewer_only_by_default(
    script: str,
) -> None:
    assert 'const requestedPanel = queryParams.get("panel") || ""' in script
    assert "let chatLoadPromise = null" in script
    assert "let chatDesiredOpen = extensionPanelPrimary" in script
    assert "if (extensionPanelPrimary && !force) open = true" in script
    assert "const requestVersion = ++chatRequestVersion" in script
    assert "chatLoadPromise ||= import(chatPanelDefinition.module_url)" in script
    assert "loadPanelStylesheet(chatPanelDefinition.stylesheet_url)" in script
    assert "mountPanel(chatDock, { viewer: viewerComponentHost.api })" in script
    assert "requestVersion !== chatRequestVersion || !chatDesiredOpen" in script


def test_chat_dock_reports_visibility_and_reserves_the_model_view(
    html: str, script: str
) -> None:
    chrome = script.split("function applyChatChrome", 1)[1].split(
        "function closePanelsForChat", 1
    )[0]
    assert chrome.index("setChatPanelVisible(false)") < chrome.index("chatDock.hidden")
    assert chrome.index("chatDock.hidden") < chrome.index("setChatPanelVisible(true)")

    loader = script.split("async function setChat", 1)[1].split(
        "function reconcileCompactLayout", 1
    )[0]
    assert loader.index("await chatLoadPromise") < loader.index(
        "setChatPanelVisible(chatDesiredOpen)"
    )

    assert html.index('id="chat-dock"') < html.index('id="canvas-wrap"')
    assert "const CHAT_CANVAS_MIN_WIDTH = 420;" in script
    width = script.split("function availableChatDockWidth", 1)[1].split(
        "function chatDockMaxWidth", 1
    )[0]
    assert 'visiblePanelFootprint("tree-panel", "split-tree")' in width
    assert 'visiblePanelFootprint("props-panel", "split-props")' in width
    assert "- CHAT_CANVAS_MIN_WIDTH" in width
    maximum = script.split("function chatDockMaxWidth", 1)[1].split(
        "function currentChatWidth", 1
    )[0]
    assert "Math.min(viewportCap, availableChatDockWidth())" in maximum
    assert 'chatLayoutObserver.observe($("tree-panel"))' in script
    assert 'chatLayoutObserver.observe($("props-panel"))' in script
    assert "startWidth + (ev.clientX - startX)" in script
    assert 'event.key === "ArrowLeft" ? -16 : 16' in script


def test_model_rebuild_clears_and_refetches_properties(script: str) -> None:
    dispose = script.split("function disposeModel()", 1)[1].split(
        "function updateStats()", 1
    )[0]
    assert "clearProperties();" in dispose

    rebuild = script.split("async function buildScene(buffer)", 1)[1].split(
        "// ---------------------------------------------------------------- spatial tree", 1
    )[0]
    assert "const keepPropertyGuid" in rebuild
    assert "showProperties(keepPropertyGuid)" in rebuild

    switch = script.split("function selectViewerModel(picked)", 1)[1].split("\n}", 1)[0]
    assert switch.index("clearProperties();") < switch.index("loadModel();")


def test_instanced_culling_covers_asymmetric_geometry(script: str) -> None:
    constructor = script.split("class InstEntry", 1)[1].split("this.capacity", 1)[0]
    assert "this.geomRadius = Math.hypot(" in constructor
    for low, high in ((0, 3), (1, 4), (2, 5)):
        assert (
            f"Math.max(Math.abs(geom.box[{low}]), Math.abs(geom.box[{high}]))"
            in constructor
        )


def test_camera_fit_accounts_for_fov_and_aspect(script: str) -> None:
    frame = script.split("function frameBox(box, direction, padding = 1)", 1)[1].split(
        "function fitTo(ids)", 1
    )[0]
    # the standoff is a perspective question even when the projection is not:
    # an orthographic camera has no field of view to ask
    assert "THREE.MathUtils.degToRad(perspectiveCamera.fov)" in frame
    assert "perspectiveCamera.aspect" in frame
    assert "Math.min(verticalFov, horizontalFov)" in frame
    assert "/ Math.sin(halfFov)" in frame
    # ... and fitting a parallel projection is a zoom, not a move
    assert "camera.zoom = Math.min(" in frame
    # padding is one multiplier on the framed sphere, so it reaches the
    # standoff and the parallel zoom the same way
    assert "sphere.radius *= padding > 0 ? padding : 1;" in frame


def test_both_projections_are_available_and_swap_in_place(script: str) -> None:
    """A length read off a perspective view means nothing; a plan needs
    parallel projection, and the swap must not move the eye."""
    assert "new THREE.OrthographicCamera(" in script
    swap = script.split("function setProjection(kind)", 1)[1].split(
        chr(10) + "/** The model's overall size", 1
    )[0]
    assert "target.position.copy(camera.position)" in swap
    assert "controls.object = camera" in swap
    # the same amount of model on screen before and after
    assert "Math.tan((perspectiveCamera.fov * Math.PI) / 360)" in swap
    assert "applyNearFar()" in swap


def test_spatial_branch_actions_include_the_branch_geometry(script: str) -> None:
    traversal = script.split("function branchElements(node)", 1)[1].split(
        "function renderTree", 1
    )[0]
    assert "if (elements.has(branch.expressID)) ids.add(branch.expressID);" in traversal
    assert "for (const child of branch.children || []) visit(child);" in traversal

    tree_item = script.split("function buildTreeItem(node, depth)", 1)[1].split(
        "function markTreeSelection", 1
    )[0]
    assert tree_item.count("branchElements(node)") == 2


def test_measurement_depth_is_computed_from_interpolated_view_position(
    script: str,
) -> None:
    shader = script.split("const depthMaterial", 1)[1].split("makeStateTextures()", 1)[0]
    assert shader.count("varying vec3 vMeasureViewPosition;") == 2
    assert "vMeasureViewPosition = mvPosition.xyz;" in shader
    # Both projections encode view-axis depth over the model's actual range,
    # rather than radial distance across the camera's deliberately huge far plane.
    assert "float measured = -vMeasureViewPosition.z;" in shader
    assert "clamp((measured - uNear) / (uFar - uNear), 0.0, 1.0)" in shader
    assert "varying float vDist" not in shader
    # The reader undoes that against the captured range and exact pixel-centre
    # ray, rather than whatever camera happens to be live when the read lands.
    reader = script.split("function depthPointFrom(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "state.near + normalized * (state.far - state.near)" in reader
    assert "state.rayOrigin" in reader and "state.rayDirection" in reader
    assert "camera.position" not in reader
    probe = script.split("function beginDepthProbe(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const range = depthPickRange();" in probe
    assert "sampleClientX" in probe and "sampleClientY" in probe
    assert "rayOrigin: raycaster.ray.origin.clone()" in probe
    assert "depthMaterial.uniforms.uNear.value = state.near;" in probe


def test_search_cancels_stale_work_and_resets_short_queries(script: str) -> None:
    search = script.split("// ---------------------------------------------------------------- search", 1)[1].split(
        "// ---------------------------------------------------------------- saved views", 1
    )[0]
    assert "let searchAbort = null;" in search
    assert "searchAbort.abort();" in search
    assert "{ signal: controller.signal }" in search
    assert "if (searchAbort === controller) searchAbort = null;" in search
    assert "searchTimer = setTimeout(() => {\n    searchTimer = null;" in search

    reset = search.split("function resetSearchResults()", 1)[1].split(
        "function clearSearch", 1
    )[0]
    assert 'box.textContent = "";' in reset
    assert 'box.setAttribute("aria-busy", "false")' in reset
    assert '$("tree").hidden = false;' in reset

    input_handler = search.split('$("search-input").addEventListener("input"', 1)[
        1
    ].split('$("search-input").addEventListener("keydown"', 1)[0]
    assert "cancelPendingSearch();" in input_handler
    assert "if (term.length < 2)" in input_handler
    assert "resetSearchResults();" in input_handler

    enter = search.split('} else if (e.key === "Enter")', 1)[1].split("\n  }", 1)[0]
    assert "clearTimeout(searchTimer)" in enter
    assert "searchTimer = null;" in enter


def test_parser_worker_init_can_retry_and_fall_back(worker_js: str, script: str) -> None:
    ensure_api = worker_js.split("function ensureApi()", 1)[1].split("// Fetch", 1)[0]
    assert "apiPromise = null;" in ensure_api
    assert ".catch((error)" in ensure_api
    assert "init_failed: true" in worker_js

    route = script.split("function routeParserMessage(msg)", 1)[1].split(
        "function spawnWorker()", 1
    )[0]
    assert "msg.init_failed" in route
    assert "stopWorker()" in route
    assert "h.onWorkerLost()" in route


def test_screenshots_are_scoped_to_the_viewed_model(script: str) -> None:
    handler = script.split("function handleScreenshot(frame)", 1)[1].split(
        "// ---------------------------------------------------------------- status bar", 1
    )[0]
    assert "const modelId = currentModelRow()?.id ?? null;" in handler
    assert "if (frame.model_id !== modelId)" in handler
    assert handler.count("model_id: modelId") == 2


def test_dead_product_count_state_is_removed(script: str) -> None:
    assert "productCount" not in script


def test_compact_layout_reconciles_open_panels_after_resize(script: str) -> None:
    assert 'window.addEventListener("resize", reconcileCompactLayout)' in script
    reconciler = script.split("function reconcileCompactLayout()", 1)[1].split(
        "function setChatAvailable", 1
    )[0]
    assert "if (chatDesiredOpen)" in reconciler
    assert "treeOpen && propsOpen" in reconciler
    assert "uiState.propsOpen = false" in reconciler


def test_orbiting_keeps_up_when_frames_are_slow(script: str) -> None:
    """Damping applied per frame is a glide at 60fps and two seconds of lag at
    the 8fps a large model gives while you are close enough to see detail."""
    assert "BASE_DAMPING" in script
    loop = script.split("function renderFrame(now)", 1)[1].split("controls.update()", 1)[0]
    assert "Math.pow(1 - BASE_DAMPING" in loop
    assert "16.7" in loop
    assert "renderer.setAnimationLoop" not in script
    assert "if (moved || cameraTween || needsRender) requestFrame();" in script
    # and the pivot follows the surface depth without ever snapping the view:
    # it slides along the view axis, so orbit and pan scale to what is in
    # front of the camera on every gesture
    assert "function repivotIfStale(" in script
    pivot = script.split("function repivotIfStale(", 1)[1].split("\n}", 1)[0]
    assert "surfacePointAt(clientX, clientY)" in pivot
    assert "camera.getWorldDirection(" in pivot
    assert "addScaledVector(_pivotDir, depth)" in pivot
    # the wheel re-anchors after cursor zoom, and a double-click frames the
    # element under the cursor
    assert "repivotIfStale(e.clientX, e.clientY)" in script
    assert 'canvas.addEventListener("wheel"' in script


def test_the_viewer_exposes_its_own_tools_to_the_panel(script: str) -> None:
    """An answer that names elements should be able to show them."""
    for command in ("isolate", "show-all", "hide", "focus", "unfocus", "set-view"):
        assert f'command.action === "{command}"' in script, command
        assert f'"{command}"' in script, command
    isolate = script.split('command.action === "isolate"', 1)[1].split(
        'command.action === "show-all"', 1
    )[0]
    # falls back to the current selection, and says so when there is none
    assert "[...selection]" in isolate
    assert "Nothing is selected to isolate" in isolate
    view = script.split('command.action === "set-view"', 1)[1].split("} else if", 1)[0]
    assert "Object.keys(VIEW_DIRECTIONS).join" in view


def test_focus_is_direct_and_does_not_create_an_object_tab_row(
    html: str, script: str, styles: str
) -> None:
    """Focus narrows the viewport; Show all is the one visible way back."""
    assert 'id="focus-tabs"' not in html
    assert ".focus-tab" not in styles
    focus = script.split('command.action === "focus"', 1)[1].split(
        'command.action === "unfocus"', 1
    )[0]
    # Focus falls back to the selection, refuses ids the model does not hold,
    # and directly updates the one active isolation set.
    assert "selectedGuids()" in focus
    assert "None of those elements are in this model" in focus
    assert "userIsolateSet = new Set(ids)" in focus
    assert "if (command.fit !== false) fitTo(ids)" in focus
    assert "openFocusTab" not in script
    assert "focusTabs" not in script
    assert "activeFocusName" not in script
    # The current isolation survives a live model rebuild without becoming a
    # list of saved analysis tabs.
    assert "const keepUserIsolate" in script
    assert "userIsolateSet = restored.length ? new Set(restored) : null" in script


def test_measurement_answers_the_questions_a_model_is_asked(
    script: str, measure_math: str
) -> None:
    """Point-to-point with a free second point cannot say how thick a wall is
    or how much room there is above a duct."""
    # an axis lock, in the model's own axes rather than the scene's
    assert "function constrainToAxis(" in script
    lock = script.split("function constrainToAxis(", 1)[1].split(chr(10) + "function ", 1)[0]
    assert "axisFrame[lock]" in lock
    # The click and preview share one constraint, so a click lands exactly
    # where the preview said it would. Only a visible X/Y/Z lock may move it;
    # the old silent near-axis inference pulled points off the clicked face.
    handler = script.split("function handleMeasureClick(", 1)[1].split("\n}", 1)[0]
    assert "constrainedMeasurePoint(anchor, hit.point)" in handler
    constraint = script.split("function constrainedMeasurePoint(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "axisLock ? constrainToAxis(anchor, raw, axisLock) : raw" in constraint
    assert "inferAxis(" not in constraint
    assert "controls.mouseButtons.LEFT = on ? null : ORBIT_LEFT_MOUSE" in script

    # element size on the element's own axes: a wall at forty degrees has a
    # thickness, and the world-axis box around it reports the diagonal
    assert "function elementDimensions(" in script
    dims = script.split("function elementDimensions(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "boxExtents(rec.box, rec.obb, axisFrame)" in dims
    for key in ("length", "width", "thickness", "diagonal", "box_volume", "centre"):
        assert key in dims, key
    extents = measure_math.split("export function boxExtents(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert 'method = "oriented bounding box"' in extents
    # the oriented extents are the local box times its own column norms
    assert "(b[3] - b[0]) * Math.hypot(m[0], m[1], m[2])" in extents
    # area and volume come off the tessellation, not off a box
    assert "area: rec.area" in dims and "volume: rec.volume" in dims

    # and clearance, both ways along each axis. Against element boxes rather
    # than the merged triangle buffers: the source geometry is released after
    # the build, and the result says which it used.
    assert "function laserFrom(" in script
    laser = script.split("function laserFrom(", 1)[1].split(chr(10) + "/**", 1)[0]
    assert 'method: "element bounding boxes"' in laser
    # the element the laser starts inside cannot be its own first hit, and a
    # hidden one is not in the way of anything
    assert "id === ignore || !isElementShown(id)" in laser
    assert "clearanceAxes(" in laser
    clearance = measure_math.split("export function clearanceAxes(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "negative.distance + positive.distance" in clearance
    # down the model's axis is not always down the scene axis it runs along
    assert "axisFrame[name].sign < 0" in clearance

    # Snapping is judged in screen space on retained real feature edges. A GPU
    # patch limits candidates to geometry visible around the cursor.
    assert "function snapAt(" in script
    assert "function snapEdgeListFor(" in script
    assert "function recordSnapParts(" in script
    assert "function beginSnapCandidateProbe(" in script
    # A proxy box can have corners in empty space on a curved/complex product.
    # Over-budget geometry therefore falls back to the exact surface, not a
    # feature the IFC never contained.
    assert "EMPTY_SNAP_EDGES" in script
    assert "boxEdgeList" not in script
    snap = script.split("function snapAt(", 1)[1].split(
        "function measurePointAt", 1
    )[0]
    assert "SNAP_RADIUS[kind]" in snap
    assert "rect.left" in snap and "rect.width" in snap
    # a point behind the lens projects to a pixel distance that is a lie
    assert "ndcZ < -1 || ndcZ > 1" in snap
    # Actual segment ends, midpoint, and anywhere along the feature edge.
    for kind in ("corner", "midpoint", "edge"):
        assert f'offer("{kind}"' in snap, kind
    assert 'offer("centre"' not in snap
    assert "for (const id of candidateIds || [])" in snap

    for command in (
        "measure-element", "measure-laser", "measure-points", "measure-angle",
        "measure-area", "clear-measurements",
    ):
        assert f'command.action === "{command}"' in script, command
        assert f'"{command}"' in script, command
    # every record says what kind it is, so the MCP side can tell them apart
    assert 'kind: "distance"' in script
    assert "function recordMeasurement(" in script


def test_a_snap_cannot_be_taken_through_a_wall_or_through_a_section(
    script: str,
) -> None:
    """Screen distance alone cannot separate a near corner from a far one. In an
    orthographic plan a wall's top and bottom corners land on the same pixel, so
    a horizontal-looking measurement could silently take one end at 0 and the
    other at 3 m; with a section on, cut-away geometry still offered all eight."""
    snap = script.split("function snapAt(", 1)[1].split("function measurePointAt", 1)[0]
    # the cut applies to the pick passes and to the draw, so it applies here
    assert "for (const plane of activeClipPlanes) {" in snap
    assert "if (plane.distanceToPoint(point) < 0) return;" in snap
    # depth is measured against the surface the cursor is actually over
    assert "const SNAP_DEPTH_SLACK_PX = 8;" in script
    assert "worldPerPixel(surface) * SNAP_DEPTH_SLACK_PX" in snap
    assert "!snapEnabled || !elements.size || !surface" in snap
    assert "camera.getWorldDirection(_snapDir)" in snap
    # Both behind and foreground pulls are bounded, and distance with a small
    # feature bias decides rather than an unconditional corner-first rank.
    assert "depth > depthSlack || depth < -frontSlack" in snap
    assert "Math.sqrt(distance) + SNAP_BIAS[kind]" in snap
    assert "best = { kind, distance, score, point: point.clone(), depth };" in snap
    # Just outside a silhouette, use the nearest occupied patch pixel as the
    # local depth guard; a visible multipart product id alone is insufficient.
    assert "const sourceX = state.px - state.half" in script
    assert "surfacePointAt(candidates.nearest.clientX" in script
    assert "await surfacePointAsync(candidates.nearest.clientX" in script
    resize = script.split("function resize()", 1)[1].split(
        "/** Give whichever camera", 1
    )[0]
    assert "cameraSerial++" in resize
    # and the residual ambiguity is shown rather than left to surprise the reader
    preview = script.split("function showSnapPreview(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "hit.depth < -1e-3" in preview
    assert "in front" in preview


def test_the_pick_passes_hide_every_marker_the_viewer_drew(script: str) -> None:
    """Both passes render the scene into a 1x1 buffer under an override
    material, so anything the viewer drew for the cursor decodes as geometry:
    the snap glyph sits under the cursor by definition."""
    pick = script.split("function pickElementAt(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const snapWasVisible = snapGroup.visible;" in pick
    assert "snapGroup.visible = false;" in pick
    assert "measureGroup.visible = false;" in pick
    # and restored on the way out, whatever happened in between
    assert "snapGroup.visible = snapWasVisible;" in pick
    assert "measureGroup.visible = measureWasVisible;" in pick
    # the depth probe hides the same things; both readbacks share the setup so
    # the two can never drift apart again
    begin = script.split("function beginSceneProbe(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "snapWasVisible: snapGroup.visible," in begin
    assert "snapGroup.visible = false;" in begin
    assert "measureGroup.visible = false;" in begin
    end = script.split("function endSceneProbe(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "snapGroup.visible = state.snapWasVisible;" in end
    assert "measureGroup.visible = state.measureWasVisible;" in end
    for reader in ("function surfacePointAt(", "async function surfacePointAsync("):
        body = script.split(reader, 1)[1].split(chr(10) + "}", 1)[0]
        assert "beginDepthProbe(clientX, clientY)" in body, reader
        assert "endSceneProbe(state);" in body, reader


def test_a_snap_is_shown_before_it_is_committed_to(script: str) -> None:
    """Clicking blind and reading the number afterwards is what made the tool
    feel broken rather than approximate."""
    preview = script.split("async function showSnapPreview(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "await measurePointAsync(clientX, clientY)" in preview
    # the rubber band from the anchor and the live length beside the cursor
    assert "previewLinePosition.setXYZ(0, anchor.x, anchor.y, anchor.z)" in preview
    assert "previewLinePosition.setXYZ(1, point.x, point.y, point.z)" in preview
    assert "formatLength(anchor.distanceTo(point))" in preview
    # the axis lock constrains the preview exactly like the click it predicts
    assert "constrainedMeasurePoint(anchor, hit.point)" in preview
    assert "AXIS_COLORS[lockAxis]" in preview
    # the snap point wears its CAD glyph, not an anonymous dot
    assert "snapGlyphTexture(previewKind)" in preview
    # One probe is in flight at a time, but pointer moves are coalesced to the
    # newest sample instead of discarded and left visibly stale.
    queue = script.split("function queueSnapPreview(", 1)[1].split(
        "async function runQueuedSnapPreview", 1
    )[0]
    assert "snapPreviewQueued = [clientX, clientY]" in queue
    assert "snapPreviewBusy || snapPreviewTimer" in queue
    assert "snapPreviewCost = performance.now() - now" in preview
    # a probe still in flight when the preview is taken down cannot put it back
    assert "if (generation !== snapPreviewGen) return;" in preview
    assert "snapPreviewGen++;" in script
    # only while measuring, and never while the pointer is down
    assert "if (measureMode && !downAt) queueSnapPreview(e.clientX, e.clientY);" in script
    assert 'controls.addEventListener("change", () => clearSnapPreview());' in script
    # A click on the shown glyph reuses that exact fresh answer rather than
    # stalling for and potentially landing on a second GPU sample.
    cached = script.split("function cachedMeasurePoint(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "cached.serial !== cameraSerial" in cached
    assert "cached.hit.point.clone()" in cached
    pointer = script.split('canvas.addEventListener("pointerdown"', 1)[1].split(
        chr(10) + "});", 1
    )[0]
    assert "cachedMeasurePoint(e.clientX, e.clientY)" in pointer


def test_the_hover_probe_does_not_block_and_click_has_an_exact_fallback(script: str) -> None:
    """readRenderTargetPixels flushes the command stream and blocks JavaScript
    until the GPU catches up, which is what held the preview to 25 Hz. The
    click reuses a fresh preview, with a blocking fallback for touch and for a
    pointer that never paused long enough to preview."""
    probe = script.split("async function surfacePointAsync(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "renderer.readRenderTargetPixelsAsync(pickTarget, 0, 0, 1, 1, probeBuffer)" in probe
    # the scene goes back before the wait, or the next frame draws under the
    # probe's override material
    assert probe.index("endSceneProbe(state);") < probe.index("await read;")
    # an answer measured off a camera that has since moved is not an answer
    assert "if (state.serial !== cameraSerial) return null;" in probe
    assert "controls.addEventListener(\"change\", () => { cameraSerial++; });" in script
    # a context that cannot read back asynchronously falls back rather than
    # leaving the preview permanently blank
    assert "asyncProbeWorks = false;" in probe
    # the click path stays synchronous, start to finish; its fallback cannot
    # land a frame after the double-click that closes an outline.
    click = script.split("function handleMeasureClick(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "await" not in click
    assert "const hit = prefetched || measurePointAt(clientX, clientY);" in click
    blocking = script.split("function surfacePointAt(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "renderer.readRenderTargetPixels(pickTarget, 0, 0, 1, 1, pickBuffer);" in blocking
    # neither pass owes the canvas a redraw unless it changed the resolution
    assert "if (scaled) invalidate();" in script
    assert "if (state.scaled) invalidate();" in script
    ensure = script.split("function ensureFullResolution()", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "if (resScale === 1) return false;" in ensure


def test_the_measure_keys_belong_to_the_viewport(script: str) -> None:
    """S, X, Y, Z and Backspace were read off the window with no target check,
    so while measure mode was on, typing into the chat composer toggled
    snapping, set an invisible axis lock and swallowed the delete key."""
    surface = script.split("function isShortcutSurface(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "target === document.body" in surface
    assert "target === canvas" in surface
    assert ".tree-label, .search-hit" in surface
    # the measure keys and the general shortcuts ask the same question
    assert script.count("if (!isShortcutSurface(e.target)) return;") == 2
    measure = script.split("if (!measureMode || e.ctrlKey", 1)[1].split(
        chr(10) + "});", 1
    )[0]
    assert "isShortcutSurface(e.target)" in measure
    assert measure.index("isShortcutSurface") < measure.index("e.key.toLowerCase()")


def test_the_measurement_maths_is_pure_and_all_of_it_is_used(
    script: str, measure_math: str
) -> None:
    """These are the functions whose answers leave the viewer, so they live
    where plain Node can check them: tests/ui/measure.test.mjs asserts the
    numbers, and this asserts nothing has crept back in that needs a browser."""
    code = re.sub(r"/\*.*?\*/", "", measure_math, flags=re.S)
    code = re.sub(r"(?m)^\s*//.*$", "", code)
    assert "import " not in code
    for forbidden in ("THREE", "document", "window", "canvas", "renderer", "camera"):
        assert forbidden not in code, forbidden
    assert 'from "./measure_math.js"' in script
    exported = set(re.findall(r"export function (\w+)", measure_math))
    assert len(exported) > 10
    imports = script.split('} from "./measure_math.js";', 1)[0].rsplit("import {", 1)[1]
    for name in sorted(exported):
        # exported either because app.js calls it or because another export
        # builds on it; anything else is a function nothing runs
        internal = len(re.findall(rf"\b{name}\(", code)) > 1
        assert name in imports or internal, f"{name} is exported but nothing uses it"


def test_markers_hold_a_constant_screen_size(script: str) -> None:
    """A model-span marker radius filled the screen on a close-up."""
    assert "function screenScaledDot(" in script
    assert "function syncScreenMarkers(" in script
    sync = script.split("function syncMarkerScale(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "perPixel * marker.userData.px" in sync
    assert "perPixel * marker.userData.pxW" in sync
    render = script.split("function renderNow(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "syncScreenMarkers();" in render
    assert "markerRadius" not in script


def test_each_measurement_is_one_labelled_deletable_thing(script: str) -> None:
    """The value floats on the measurement, and one x removes exactly it."""
    assert "function labelSprite(" in script
    commit = script.split("function commitMeasurement(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "adoptPending(formatLength(distance), mid)" in commit
    assert "function deleteMeasurement(" in script
    assert "function emphasizeMeasurement(" in script
    rows = script.split("function renderMeasurements(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "deleteMeasurement(m)" in rows
    assert "emphasizeMeasurement(m, true)" in rows
    # Backspace takes back the last click; Escape sheds points and lock
    # before it sheds the mode
    assert "function undoPendingPoint(" in script
    assert 'if (key === "backspace")' in script
    assert "if (pending.length || axisLock)" in script


def test_angle_and_area_share_the_click_flow(script: str, measure_math: str) -> None:
    assert "const MEASURE_KINDS = { distance: 2, path: 0, angle: 3, area: 0 };" in script
    area = measure_math.split("export function polygonMeasure(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    # Newell, so a polygon that is not quite flat still has an area and the
    # reader is told how far from flat it was
    assert "polygonNormal(points)" in area
    assert "flatness" in area
    # the scratch vector the flatness loop used to borrow from the snap layer
    # is gone, so the answer cannot depend on what snapped last
    assert "_snapV" not in measure_math
    angle = measure_math.split("export function angleMeasure(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "Math.acos(Math.min(1, Math.max(-1, cos)))" in angle
    # the viewer's own copies are the frame conversion and nothing else
    assert "polygonCore(points)" in script
    assert "angleCore(from, at, to)" in script
    # Open paths and area outlines finish explicitly, never on a point count.
    assert "function finishPath()" in script
    assert "function finishArea()" in script
    assert "const MAX_MEASURE_POINTS = 200;" in script
    assert "pending.length >= MAX_MEASURE_POINTS" in script
    assert "if (finishOpenMeasurement()) renderMeasurements();" in script
    assert "polylineCore(points)" in script
    assert "export function polylineMeasure(points)" in measure_math
    # pointerup commits a point for each half of a double-click, so closing on
    # the last corner used to leave a duplicate: a rectangle read "6 points"
    # and polygonMeasure integrated the zero-length edges into the perimeter
    assert 'measureKind === "area" || measureKind === "path"' in script
    assert "outlinePoints(points, AREA_MIN_EDGE_SQ)" in script
    outline = measure_math.split("export function outlinePoints(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "out.pop();" in outline  # a closing point on top of the first one
    finish = script.split("function finishArea()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "areaOutline(pending.map((entry) => entry.point))" in finish
    assert "if (points.length < 3) return false;" in finish
    # the command path takes its outline from a caller and gets the same guard
    assert "areaOutline(raw.map((entry) => {" in script
    assert "measure-area needs at least three distinct points" in script


def test_measurements_are_anchored_to_globalids_and_outlive_a_rebuild(
    script: str,
) -> None:
    """buildScene ran clearMeasurements() on every rebuild and every revision
    bump rebuilds, so the assistant writing one property set deleted the user's
    whole measurement set, server copy included. A reload lost them too."""
    build = script.split("async function buildScene(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "clearMeasurements();" not in build
    assert "const keepMeasurements = switchingTabs ? [] : measurementCarry();" in build
    assert "restoreMeasurements(keepMeasurements);" in build
    # snapshot before the scene is torn down, replay once the ids are rebuilt
    assert build.index("measurementCarry()") < build.index("disposeModel();")
    assert build.index("disposeModel();") < build.index("restoreMeasurements(")

    # each end keeps the identity measurePointAt already resolved and threw away
    click = script.split("function handleMeasureClick(", 1)[1].split(chr(10) + "}", 1)[0]
    assert 'kind: movedByLock ? "axis" : hit.kind' in click
    assert "express_id: movedByLock ? null : hit.express_id" in click
    assert "pendingAnchors()" in click
    anchor = script.split("function anchorAt(", 1)[1].split(chr(10) + "}", 1)[0]
    # in the element's own box, so a wall that moved carries its dimension
    assert "_anchorM.fromArray(rec.obb.m).invert()" in anchor
    assert "anchor.reach = rec.obbReach;" in anchor
    # plus the model-axis point, which no rebuild and no origin shift can move
    assert "world: toModelPoint(point)" in anchor

    place = script.split("function placeAnchor(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const id = expressOf.get(anchor.guid);" in place
    assert 'if (id === undefined) return loose("gone", undefined);' in place
    # an element that came back a different shape is not the thing measured
    assert "ANCHOR_REACH_TOLERANCE" in place
    assert 'return loose("changed", id);' in place
    assert "toScenePoint(anchor.world)" in place
    # and the row says which of the two happened rather than reading as measured
    rows = script.split("function renderMeasurements()", 1)[1].split(chr(10) + "}", 1)[0]
    assert 'm.drift === "gone"' in rows
    assert "point kept where it was taken" in rows

    # F5 and saved views carry them too
    save = script.split("function saveMeasurements()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "uiState.measurements = { model: measuredModelKey, items }" in save
    carry = script.split("function measurementCarry()", 1)[1].split(chr(10) + "}", 1)[0]
    # a set taken on another model would place its fallback points in space
    assert "const known = measuredModelKey !== null && key !== null;" in carry
    assert "return !known || measuredModelKey === key ? measurementItems() : [];" in carry
    assert "return saved.model === key ? saved.items : [];" in carry
    capture = script.split("function captureView(name)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "measurements: measurementItems()," in capture
    restore = script.split("function restoreView(view)", 1)[1].split(chr(10) + "}", 1)[0]
    assert "restoreMeasurements(view.measurements);" in restore
    # a view saved without any, or on another model, leaves the screen alone
    assert "Array.isArray(view.measurements) && view.measurements.length" in restore
    assert "view.model === currentModelKey()" in restore
    assert "model: currentModelKey()," in capture

    # a half-replayed list reaches neither the server nor the browser store
    publish = script.split("function publishMeasurements()", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "if (measureQuiet) return;" in publish
    assert "if (measureQuiet) return;" in rows
    assert "sendMeasurements();" not in build


def test_coordinates_leave_the_viewer_in_the_model_s_own_axes(script: str) -> None:
    """web-ifc draws Y-up and slides the model to the origin. Both are right
    for drawing and wrong for saying where something is."""
    assert "const IFC_TO_GL = new THREE.Matrix4().set(" in script
    frames = script.split("function refreshFrames()", 1)[1].split(chr(10) + "/**", 1)[0]
    # the coordination matrix carries the origin shift, IFC_TO_GL the axes
    assert "modelToScene.copy(coordinationMatrix).multiply(IFC_TO_GL)" in frames
    assert "makeTranslation(-origin[0], -origin[1], -origin[2])" in frames
    assert "sceneToModel.copy(modelToScene).invert()" in frames
    # nothing may swap axes by hand any more
    assert "AXIS_LOCK_TO_SCENE" not in script
    assert "SCENE_AXIS_TO_MODEL" not in script
    for helper in ("toModelPoint", "toScenePoint", "toModelAxis", "toSceneAxis"):
        assert f"function {helper}(" in script, helper
    # the parser is where the matrix comes from
    parser = (STATIC / "parser.js").read_text(encoding="utf-8")
    assert "api.GetCoordinationMatrix(modelID)" in parser
    assert 'emit({ type: "coordination", matrix: coordination })' in parser


def test_a_section_can_keep_a_slice_rather_than_a_half_space(script: str) -> None:
    """A half-space answers "what is below this level"; a floor plan is the
    other question."""
    clip = script.split("function updateClipping()", 1)[1].split(chr(10) + "function ", 1)[0]
    assert "if (sliceDepth > 0)" in clip
    assert "back.normal.copy(AXIS_NORMALS[axis]).multiplyScalar(-sign)" in clip
    assert "back.constant = -sign * (at - sign * sliceDepth)" in clip
    # positions cross the command surface as real heights, not slider fractions
    state = script.split("function sectionState()", 1)[1].split(chr(10) + "function ", 1)[0]
    assert "toModelAxis(axis, scenePosition)" in state
    assert 'command.action === "set-section"' in script


def test_section_slider_shows_the_real_coordinated_cut_plane(
    html: str, script: str
) -> None:
    assert "section-plane-helper" in script
    helper = script.split("function showSectionHelper(axis)", 1)[1].split(
        "function hideSectionHelper", 1
    )[0]
    assert "const modelAxis = MODEL_OF_SCENE[axis] || axis" in helper
    assert "SECTION_HELPER_COLORS[modelAxis]" in helper
    assert "sectionHelperRoot.visible = true" in helper
    slider = script.split('for (const slider of document.querySelectorAll(".section-at"))', 1)[1]
    slider = slider.split(chr(10) + "}", 1)[0]
    assert "showSectionHelper(axis)" in slider
    assert "updateClipping()" in slider
    row = script.split("function syncSectionRow(axis)", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "MODEL_OF_SCENE[axis]" in row
    assert 'class="tool-note section-note"' in html


def test_geometry_measurements_are_taken_while_the_mesh_is_still_there(
    script: str, measure_math: str
) -> None:
    """The tessellation is freed after the build, so anything the measuring
    tools want from it has to be taken during the parse."""
    mass = measure_math.split("export function geometryMass(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    # divergence theorem over the same triangles the area came from
    assert "volume6 += ax * nx + ay * ny + az * nz;" in mass
    assert "area: area2 / 2" in mass and "volume: Math.abs(volume6) / 6" in mass
    # deduplicated and calculated in the parser worker: once per shape however
    # many times it is placed, without repeating the triangle walk on the UI
    parser = (STATIC / "parser.js").read_text(encoding="utf-8")
    assert 'geometryMass,' in parser
    assert "geometryMass(positions, indices)" in parser
    assert "areas: Float64Array.from(this.areas)" in parser
    assert "volumes: Float64Array.from(this.volumes)" in parser
    # Inline or legacy chunks still have a correct fallback.
    assert "? { area, volume } : geometryMass(positions, indices)" in script
    accrue = script.split("function accrueMass(", 1)[1].split(chr(10) + "function ", 1)[0]
    assert "rec.volume += geom.mass.volume * det;" in accrue
    # a scaled placement cannot report an exact area, and says so
    assert "rec.scaled = true;" in accrue
    # the oriented box is the local box still attached to its placement
    assert "rec.obb = { m: local, box: Float32Array.from(geom.box) };" in accrue


def test_the_model_bounds_are_whole_before_the_first_chunk_is_batched(
    script: str,
) -> None:
    """cellKeyFor buckets merged geometry against modelBox. A box that grows one
    element at a time during ingest puts the earliest chunks in cells chosen
    from a fraction of the model, and the same placements were transformed
    twice to get there."""
    decide = script.split("function decideOrigin(", 1)[1].split(chr(10) + "function ", 1)[0]
    # one walk carries the origin, the bounds, every placement's world box and
    # how many times each geometry is placed
    assert "layout.set(chunk, { boxes, verts });" in decide
    assert "return { box: isFinite(probe[0]) ? probe : null, layout, uses, verts: total };" in decide
    assert "uses.set(key, (uses.get(key) || 0) + 1);" in decide
    # the shift lands on boxes already measured rather than measuring again
    assert "boxes[i + k] -= origin[k];" in decide
    build = script.split("async function buildScene(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const placed = decideOrigin(parsed.chunks);" in build
    assert "modelBox = placed.box;" in build
    assert "ingestChunk(chunk, placed.layout.get(chunk), placed.uses);" in build
    # the grid is planned from the whole model, before anything is batched
    assert "planSpatialGrid(placed.box, placed.verts, {" in build
    assert build.index("cellSize = spatial.size;") < build.index("ingestChunk(")
    ingest = script.split("function ingestChunk(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "layout.boxes" in ingest
    assert "cellKeyFor(boxes, at, transparent)" in ingest
    # ingest reads the boxes back: it neither re-transforms the corners nor
    # grows the model bounds under the batcher
    assert "unionBoxCorners" not in ingest
    assert "modelBox" not in ingest


def test_a_repeated_geometry_is_instanced_only_when_that_is_cheaper(
    script: str,
) -> None:
    """One InstancedMesh was created the moment a geometry was seen twice, so
    IFC's long tail of mirrored doors and paired windows bought thousands of
    dedicated, uncullable draw calls to save a few thousand vertices."""
    ingest = script.split("function ingestChunk(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "const copies = uses.get(useKey) || 1;" in ingest
    assert "shouldInstanceGeometry(" in ingest
    assert "copies, vertices, INSTANCE_MIN, INSTANCE_MIN_VERTICES" in ingest
    assert "const INSTANCE_MIN = 8;" in script
    assert "const INSTANCE_MIN_VERTICES = 2_000;" in script
    # nothing decides on a second sighting any more
    assert "Second sighting" not in script
    assert '"merged"' not in ingest
    # past a point one instanced mesh spans the model and can never be culled,
    # and it is split by octant: splitting on the merge grid would turn a
    # curtain wall into one draw call per panel
    assert "const split = copies >= INSTANCE_SPLIT;" in ingest
    assert 'split ? `${useKey}#${octantKeyFor(boxes, at)}` : useKey' in ingest
    assert "const INSTANCE_SPLIT = 256;" in script
    octant = script.split("function octantKeyFor(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "(modelBox[k] + modelBox[k + 3]) / 2" in octant
    # the count is known up front, so the instance arrays are sized once
    assert "Math.max(16, this.expected)" in script


def test_normals_are_packed_once_and_instanced_lod_does_not_break_picking(
    script: str, measure_math: str
) -> None:
    """Direction does not need 32-bit components, and a display-only LOD must
    never make an exact selection or measurement probe disagree with the IFC."""
    parser = (STATIC / "parser.js").read_text(encoding="utf-8")
    assert "const normals = new Int16Array(vTotal * 3);" in parser
    assert "const normals = new Int16Array(count * 3);" in parser
    assert "packNormalComponent(raw[s + 3])" in parser
    assert "export function packNormalBuffer(source)" in measure_math
    assert "this.normals = new GrowArray(Int16Array);" in script
    assert "new THREE.BufferAttribute(acc.normals.trim(), 3, true)" in script
    assert "new THREE.BufferAttribute(normals, 3, true)" in script

    render = script.split("function renderNow()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "applyInstanceLod();" in render
    lod = script.split("function applyInstanceLod()", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "const spread = Math.max(0, entry.sphere.radius - radius);" in lod
    assert "entry.mesh.visible = !entry.lodHidden;" in lod

    pick = script.split("function pickElementAt(", 1)[1].split(
        chr(10) + "// Click-to-select", 1
    )[0]
    assert "const lodWasSuspended = suspendInstanceLod();" in pick
    assert "resumeInstanceLod(lodWasSuspended);" in pick
    probe = script.split("function beginSceneProbe(", 1)[1].split(
        chr(10) + "function endSceneProbe", 1
    )[0]
    assert "lodWasSuspended: suspendInstanceLod()," in probe
    end = script.split("function endSceneProbe(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "resumeInstanceLod(state.lodWasSuspended);" in end


def test_merged_chunks_are_bucketed_by_where_they_are(script: str) -> None:
    """A 2x2x2 split of the model cannot cull: a camera inside a room
    intersects most octants, so nearly every triangle was submitted on every
    frame, every 1x1 pick and every hover probe."""
    cell = script.split("function cellKeyFor(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "Math.floor((box[at] - modelBox[0]) / cellSize)" in cell
    assert "Math.floor((box[at + 1] - modelBox[1]) / cellSize)" in cell
    assert "Math.floor((box[at + 2] - modelBox[2]) / cellSize)" in cell
    # the old midpoint split is gone from the merge grid
    assert "(modelBox[0] + modelBox[3]) / 2" not in cell
    # a cell is baked at the budget the plan set, not at a fixed limit, or the
    # open cells would hold the whole model between them
    bake = script.split("function bakeMerged(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "if (acc.vertexCount >= cellFlushAt) {" in bake
    assert "cellFlushAt = spatial.flushAt;" in script
    # and a rebuild starts from no grid at all
    dispose = script.split("function disposeModel()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "cellSize = 0;" in dispose


def test_the_load_loops_take_a_square_root_rather_than_math_hypot(
    script: str, measure_math: str
) -> None:
    """V8 does not lower Math.hypot to a sqrt. The merge loop runs one per
    vertex and the mass loop one per triangle, inside the synchronous block the
    code itself warns about."""
    assert "export function norm3(x, y, z) {" in measure_math
    assert "return Math.sqrt(x * x + y * y + z * z);" in measure_math
    mass = measure_math.split("export function geometryMass(", 1)[1].split(
        chr(10) + "}", 1
    )[0]
    assert "Math.hypot" not in mass
    assert "norm3(" in mass
    for name in ("bakeMerged", "accrueMass"):
        body = script.split(f"function {name}(", 1)[1].split(chr(10) + "}", 1)[0]
        assert "Math.hypot" not in body, name
        assert "norm3(" in body, name


def test_no_style_attributes_or_inline_scripts(html: str) -> None:
    """The CSP forbids both; a violation only shows up in a browser console."""
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<style[\s>]", html)


def test_stylesheet_defines_every_custom_property_it_uses(styles: str) -> None:
    used = set(re.findall(r"var\((--[a-z-]+)", styles))
    defined = set(re.findall(r"^\s*(--[a-z-]+):", styles, re.MULTILINE))
    assert not used - defined, f"undefined CSS variables: {sorted(used - defined)}"


def _assert_plain_object_check(script: str) -> None:
    check = script.split("function isPlainObject(value)", 1)[1].split("\n}", 1)[0]
    assert "value !== null" in check
    assert 'typeof value === "object"' in check
    assert "!Array.isArray(value)" in check
    assert "Object.getPrototypeOf(value) === Object.prototype" in check


def test_viewer_persisted_ui_requires_a_plain_object_and_write_is_best_effort(
    script: str,
) -> None:
    _assert_plain_object_check(script)
    loader = script.split("const uiState = (() =>", 1)[1].split("function saveUi()", 1)[0]
    assert "const saved = JSON.parse(" in loader
    assert "return isPlainObject(saved) ? saved : {};" in loader

    writer = script.split("function saveUi()", 1)[1].split("function applySceneSettings()", 1)[0]
    assert "try {" in writer
    assert 'localStorage.setItem("ifc-console-viewer-ui"' in writer
    assert "} catch {" in writer


# ------------------------------------------ the viewer ships with the main app
def test_assets_resolve_from_the_main_package():
    from ifc_console.viewer import assets

    directory = assets.require_static_dir()
    assert (directory / "index.html").is_file()
    assert directory.name == "static"
    assert directory.parent.name == "viewer"
    assert directory.parent.parent.name == "ifc_console"


def test_missing_bundled_assets_report_how_to_repair_the_install(monkeypatch, tmp_path):
    from ifc_console.viewer import assets

    missing = tmp_path / "missing-static"
    monkeypatch.setattr(assets, "STATIC_DIR", missing)

    assert assets.static_dir() == missing
    assert assets.available() is False
    with pytest.raises(FileNotFoundError) as excinfo:
        assets.require_static_dir()
    assert "reinstall ifc-console" in str(excinfo.value)


def test_the_main_package_ships_the_complete_browser_runtime():
    import ifc_console

    base = Path(ifc_console.__file__).parent
    static = base / "viewer" / "static"
    assert (static / "index.html").is_file()
    assert (static / "vendor" / "web-ifc.wasm").is_file()


def test_the_viewer_reports_and_releases_its_memory(script: str) -> None:
    """The Agent panel shows what the page holds and can ask for it back.
    Parsed models re-parse on the next tab switch; the scene itself stays."""
    context = script.split("function viewerContext(", 1)[1].split(
        "function scheduleViewerContext", 1
    )[0]
    assert "memory: viewerMemory()," in context
    assert '"release-memory",' in context
    report = script.split("function viewerMemory()", 1)[1].split(chr(10) + "}", 1)[0]
    for field in ("parsedCacheBytes", "parsedCacheEntries", "elements", "triangles", "workerAlive"):
        assert f"{field}:" in report
    release = script.split("function releaseViewerMemory(", 1)[1].split(chr(10) + "}", 1)[0]
    assert "dropParsedCache()" in release
    assert "releaseInlineParser()" in release
    assert "stopIdleWorker && worker && !workerBusy" in release
    assert 'command.action === "release-memory"' in script
    passive = script.split("const passiveWhileClosed = new Set([", 1)[1].split("]);", 1)[0]
    assert '"release-memory"' in passive
    # Out of sight, the cache is given back after a grace period.
    hidden = script.split('document.addEventListener("visibilitychange"', 1)[1].split(
        "window.addEventListener(\"pagehide\"", 1
    )[0]
    assert "HIDDEN_CACHE_MS" in hidden
    assert "dropParsedCache();" in hidden
    assert "const HIDDEN_CACHE_MS = 60_000;" in script
    # The inline web-ifc fallback must not keep its WebAssembly heap forever.
    idle = script.split("function scheduleWorkerIdle()", 1)[1].split(chr(10) + "}", 1)[0]
    assert "releaseInlineParser();" in idle
    budget = script.split("const PARSED_CACHE_BUDGET = ", 1)[1].split(";", 1)[0]
    assert "Math.min(160, Math.max(64," in budget


def test_the_selection_can_launch_a_workflow_from_the_status_bar(
    html: str, script: str
) -> None:
    """Click an object, press Run workflow: the agent panel opens on the
    workflow library with the selection as the scope. The action is hidden
    when nothing is selected or no agent panel is available."""
    assert 'id="sel-workflow"' in html
    block = script.split("function updateSelectionInfo", 1)[1].split(
        "function updateHighlightInfo", 1
    )[0]
    assert '$("sel-workflow").hidden = !n || $("btn-chat").hidden' in block
    handler = script.split('$("sel-workflow").addEventListener("click"', 1)[1]
    assert handler.index("await setChat(true)") < handler.index(
        'chatPanel?.openWorkflows?.({ scope: "selection" })'
    )
