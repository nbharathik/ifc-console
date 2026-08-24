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
def styles() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def worker_js() -> str:
    return (STATIC / "worker.js").read_text(encoding="utf-8")


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


def test_chat_splitter_updates_aria_and_restores_close_focus(
    script: str, chat_css: str
) -> None:
    handler = script.split('chatResize.addEventListener("keydown"', 1)[1].split(
        "\n});", 1
    )[0]
    for key in ("Home", "ArrowLeft", "ArrowRight"):
        assert f'event.key === "{key}"' in handler or f'event.key !== "{key}"' in handler
    aria = script.split("function syncChatResizeAria", 1)[1].split(
        "function setChatWidth", 1
    )[0]
    for attribute in (
        "aria-valuemin",
        "aria-valuemax",
        "aria-valuenow",
        "aria-valuetext",
    ):
        assert f'"{attribute}"' in aria
    close = script.split("chatPanel ||= mountChat", 1)[1].split("return chatPanel", 1)[0]
    assert "setChat(false)" in close
    assert "chatBtn.focus({ preventScroll: true })" in close
    assert "#chat-dock-resize:focus-visible" in chat_css


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


def test_chat_loading_is_single_flight_and_honors_the_latest_panel_state(
    script: str,
) -> None:
    assert "let chatLoadPromise = null" in script
    assert "let chatDesiredOpen = false" in script
    assert "const requestVersion = ++chatRequestVersion" in script
    assert 'chatLoadPromise ||= import("/viewer/static/chat.js")' in script
    assert "requestVersion !== chatRequestVersion || !chatDesiredOpen" in script


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

    switch = script.split('$("model-select").addEventListener("change"', 1)[1].split(
        "\n});", 1
    )[0]
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
    frame = script.split("function frameBox(box, direction)", 1)[1].split(
        "function fitTo(ids)", 1
    )[0]
    assert "THREE.MathUtils.degToRad(camera.fov)" in frame
    assert "camera.aspect" in frame
    assert "Math.min(verticalFov, horizontalFov)" in frame
    assert "/ Math.sin(halfFov)" in frame


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
    assert "length(vMeasureViewPosition) / uFar" in shader
    assert "varying float vDist" not in shader


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
    assert "worker.terminate()" in route
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


# ------------------------------------------------- the viewer is an extra now
def test_assets_resolve_through_the_companion_package():
    from ifc_console.viewer import assets

    directory = assets.require_static_dir()
    assert (directory / "index.html").is_file()
    assert directory.name == "static"
    assert "ifc_console_viewer" in directory.parts


def test_missing_assets_report_how_to_install_them(monkeypatch):
    from ifc_console.viewer import assets

    assets.static_dir.cache_clear()
    monkeypatch.setattr(assets, "_IN_TREE", Path("/nonexistent"))
    monkeypatch.setitem(__import__("sys").modules, "ifc_console_viewer", None)
    try:
        assert assets.static_dir() is None
        assert assets.available() is False
        with pytest.raises(FileNotFoundError) as excinfo:
            assets.require_static_dir()
        assert "ifc-console[viewer]" in str(excinfo.value)
    finally:
        assets.static_dir.cache_clear()


def test_the_base_package_ships_no_static_files():
    import ifc_console

    base = Path(ifc_console.__file__).parent
    assert not list(base.rglob("*.wasm"))
    assert not (base / "viewer" / "static").exists()


# ------------------------------------------------------------- the chat panel
@pytest.fixture(scope="module")
def chat_js() -> str:
    return (STATIC / "chat.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_css() -> str:
    return (STATIC / "chat.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_history_js() -> str:
    return (STATIC / "chat_history.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_markdown_js() -> str:
    return (STATIC / "chat_markdown.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_flow_js() -> str:
    return (STATIC / "chat_flow.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_sidebar_js() -> str:
    return (STATIC / "chat_sidebar.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_workspace_js() -> str:
    return (STATIC / "chat_workspace.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def chat_page_js() -> str:
    return (STATIC / "chat-page.js").read_text(encoding="utf-8")


def test_the_chat_page_runs_no_inline_script():
    """The page's CSP is script-src 'self'; an inline module would be blocked."""
    html = (STATIC / "chat.html").read_text(encoding="utf-8")
    assert "chat-page.js" in html
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), "inline script in chat.html"


def test_every_role_the_script_reads_exists_in_the_markup(chat_js: str):
    """Markup and script drifting apart is the one bug a browser-less suite
    can still catch: el("x") must have a data-role="x" behind it."""
    declared = set(re.findall(r'data-role="([\w-]+)"', chat_js))
    used = set(re.findall(r'el\("([\w-]+)"\)', chat_js))
    assert used - declared == set(), f"chat.js reads roles that do not exist: {used - declared}"


def test_every_action_the_script_handles_exists_in_the_markup(chat_js: str):
    declared = set(re.findall(r'data-act="([\w-]+)"', chat_js))
    handled = set(re.findall(r'action === "([\w-]+)"', chat_js))
    assert handled <= declared, f"handlers without a button: {handled - declared}"
    # every button must be handled somewhere: as an action, or wired directly
    for action in declared:
        assert action in handled or f'[data-act="{action}"]' in chat_js, f"dead button: {action}"


def test_the_dock_takes_its_colours_from_the_viewer(chat_css: str):
    """Its own hardcoded palette made the dock clash with the viewer chrome
    and stay dark when the console switched to the light theme."""
    block = chat_css.split(".chat-root {", 1)[1].split("}", 1)[0]
    for line in block.splitlines():
        if line.strip().startswith("--chat-") and "#" in line:
            assert "var(--" in line, f"hardcoded colour in the dock: {line.strip()}"


def _contrast(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_quiet_text_tokens_meet_wcag_aa(styles: str, chat_css: str) -> None:
    for source in (styles, chat_css):
        assert "--text-quiet: #83909c;" in source
        assert "--text-quiet: #596875;" in source
    for foreground, background in (
        ("#83909c", "#171e27"),
        ("#83909c", "#101720"),
        ("#596875", "#f5f8fb"),
        ("#596875", "#e7edf3"),
    ):
        assert _contrast(foreground, background) >= 4.5


def test_docked_transcript_text_is_selectable(chat_css: str) -> None:
    log = chat_css.split(".chat-log {", 1)[1].split("}", 1)[0]
    assert "user-select: text;" in log


def test_markdown_escapes_attribute_delimiters(chat_markdown_js: str) -> None:
    escaping = chat_markdown_js.split("export const esc =", 1)[1].split(
        "function mdInline", 1
    )[0]
    assert '.replace(/"/g, "&quot;")' in escaping
    assert ".replace(/'/g, \"&#39;\")" in escaping


def test_settings_are_a_dialog_not_an_inline_panel(chat_js: str, chat_css: str):
    """An inline settings panel squeezed the conversation; it is a modal now."""
    assert 'class="chat-modal"' in chat_js
    assert 'class="chat-dialog t-modal"' in chat_js
    assert ".chat-modal { position: absolute" in chat_css
    assert "chat-scrim" in chat_js, "the dialog needs a scrim to close on"


def test_chat_dialog_contains_focus_and_restores_it(chat_js: str):
    assert "settingsReturnFocus" in chat_js
    assert 'event.key === "Tab"' in chat_js
    assert 'aria-controls="chat-settings"' in chat_js
    assert "target.focus()" in chat_js


def test_chat_status_and_send_state_are_accessible(chat_js: str):
    assert 'role="log" aria-label="Conversation"' in chat_js
    assert 'data-role="announce" role="status"' in chat_js
    assert 'send.setAttribute("aria-label", "Stop response")' in chat_js
    assert 'send.setAttribute("aria-label", "Send message")' in chat_js


def test_agent_builder_uses_allowlisted_blocks_and_an_accessible_dialog(
    chat_js: str, chat_css: str
) -> None:
    assert 'id="chat-builder"' in chat_js
    assert 'aria-label="Build an assistant"' in chat_js
    assert 'postJSON("/api/agents/custom"' in chat_js
    assert 'api("/api/agents/blocks")' in chat_js
    assert ".chat-block input:checked + span" in chat_css


def test_agent_attachments_and_ai_proposals_are_first_class(
    chat_js: str, chat_css: str
) -> None:
    assert "pendingAttachments" in chat_js
    assert "attachments: turns.findLast" in chat_js
    assert 'event.type === "proposal"' in chat_js
    assert "AI-marked · preview only" in chat_js
    assert ".chat-proposal" in chat_css
    assert ".chat-step" in chat_css


def test_escape_closes_the_dialog_before_stopping_the_stream(chat_js: str):
    handler = chat_js.split('root.addEventListener("keydown"', 1)[1].split(
        'root.addEventListener("click"', 1
    )[0]
    assert handler.index("closeSettings()") < handler.index("aborter?.abort()")


def test_stopping_before_content_keeps_an_alternating_visible_transcript(
    chat_js: str, chat_css: str
) -> None:
    run = chat_js.split("async function run()", 1)[1].split(
        "async function submit()", 1
    )[0]
    assert 'const stoppedMessage = stopped && !text ? "Response stopped before content."' in run
    assert "view.answer.textContent = stoppedMessage;" in run
    assert 'turns.push({ role: "assistant", text: transcriptText })' in run
    assert ".chat-answer.stopped" in chat_css


def test_standalone_chat_uses_status_theme_after_os_default(
    chat_js: str, chat_css: str, chat_page_js: str
) -> None:
    assert 'typeof options.onStatus === "function"' in chat_js
    startup = chat_js.split("if (!restoreHistory()) empty();", 1)[1].split(
        "return {", 1
    )[0]
    assert startup.index("refreshContext();") < startup.index("loadProviders();")
    assert "onStatus: (status) =>" in chat_page_js
    assert "document.documentElement.dataset.consoleTheme = status.theme" in chat_page_js
    assert "html:not([data-console-theme]) body.chat-page" in chat_css
    assert 'html[data-console-theme="light"] body.chat-page' in chat_css


def test_a_stale_model_list_cannot_win(chat_js: str):
    """Switching provider mid-load once mixed two providers' models together."""
    assert "modelRequest" in chat_js
    assert chat_js.count("ticket !== modelRequest") >= 2


def test_the_panel_never_calls_a_provider_directly(chat_js: str):
    """Keys stay server side; every request goes to this origin."""
    for line in chat_js.splitlines():
        if "fetch(" in line or "postJSON(" in line:
            assert "http://" not in line and "https://" not in line, line.strip()


def test_the_send_button_is_gated_until_a_model_is_chosen(chat_js: str):
    assert "send.disabled = !ready" in chat_js


def test_no_api_key_is_ever_written_to_browser_storage(chat_js: str):
    """The panel promises the key only lives in the running console. Local
    storage is disk, so the key must not go near the settings blob."""
    saved = chat_js.split("function saveSettings()", 1)[1].split("\n  }", 1)[0]
    assert "localStorage" in saved, "the settings blob is still saved here"
    assert "settings.keys" not in chat_js, "keys are back in the persisted settings"
    for line in chat_js.splitlines():
        if "localStorage" in line or "sessionStorage" in line:
            assert "key" not in line.lower(), line.strip()


def test_chat_persisted_settings_require_a_plain_object_and_write_is_best_effort(
    chat_js: str,
) -> None:
    _assert_plain_object_check(chat_js)
    loader = chat_js.split("function loadSettings()", 1)[1].split("function chosenModel()", 1)[0]
    assert "if (!isPlainObject(saved)) saved = {};" in loader
    assert "byProvider: isPlainObject(saved.byProvider) ? saved.byProvider : {}" in loader

    writer = chat_js.split("function saveSettings()", 1)[1].split("const provider =", 1)[0]
    assert "try {" in writer
    assert "localStorage.setItem(STORE, JSON.stringify(settings));" in writer
    assert "} catch {" in writer


def test_conversations_are_local_exportable_and_separate_from_credentials(
    chat_js: str, chat_history_js: str
):
    assert 'from "./chat_history.js"' in chat_js
    assert "historyStore.save(conversationRecord())" in chat_js
    assert "transcriptMarkdown(record" in chat_js
    assert "provider" in chat_history_js.casefold()
    assert "credentials" in chat_history_js.casefold()
    assert "api_key" not in chat_history_js
    assert "password" not in chat_history_js


def test_context_flow_and_secure_key_controls_are_visible(chat_js: str, chat_css: str):
    """Route, model, and mode stay on screen; the rest moved to the workspace."""
    assert 'data-role="context"' in chat_js
    assert 'data-role="modelname"' in chat_js
    assert 'data-act="export"' in chat_js
    assert "operating-system credential store" in chat_js
    assert ".chat-history-panel" in chat_css
    assert ".chat-head .chat-context" in chat_css


def test_tool_chips_are_paired_by_id_not_by_name(chat_js: str):
    """Two calls to one tool in a round left a chip spinning forever."""
    assert "pending.set(event.id" in chat_js
    assert "pending.get(event.id" in chat_js
    assert "pending.get(event.name" not in chat_js


def test_chat_distinguishes_the_ai_model_from_the_open_ifc_model(chat_js: str):
    assert "no AI model" in chat_js
    assert "Configure AI model" in chat_js
    assert 'for="chat-model">AI model<' in chat_js


def test_chat_makes_provider_egress_visible(chat_js: str, chat_css: str):
    for label in ("local", "network", "blocked"):
        assert f".chat-route.{label}" in chat_css
    assert "Prompts stay on this machine." in chat_js
    assert "Blocked by chat.local_only" in chat_js


def test_compact_layout_uses_overlays_instead_of_squeezing_the_canvas(
    script: str, styles: str, chat_css: str
) -> None:
    assert "window.innerWidth > 620" in script
    compact = styles.split("@media (max-width: 620px)", 1)[1]
    assert "#tree-panel," in compact and "position: absolute" in compact
    dock = chat_css.split("@media (max-width: 900px)", 1)[1]
    assert "position: absolute" in dock
    assert "width: min(480px, 100%)" in dock


def test_the_rail_lists_agents_and_conversations_and_is_keyboard_reachable(
    chat_js: str, chat_css: str
) -> None:
    """The rail is the panel's map: which assistants exist and what was asked."""
    assert 'data-role="rail-agents"' in chat_js
    assert 'data-role="rail-history"' in chat_js
    assert 'rail.addEventListener("focusin", expandRail)' in chat_js
    assert "if (!rail.contains(event.relatedTarget)) collapseRail();" in chat_js
    assert ".chat-root.rail-open .chat-rail" in chat_css
    assert "@media (prefers-reduced-motion: reduce)" in chat_css


def test_the_rail_pin_state_survives_a_reload_without_storing_secrets(
    chat_js: str,
) -> None:
    assert 'localStorage.setItem("ifc-console-chat-rail"' in chat_js
    assert 'localStorage.getItem("ifc-console-chat-rail")' in chat_js


def test_custom_agents_can_be_deleted_from_the_panel(
    chat_js: str, chat_sidebar_js: str
) -> None:
    assert 'postJSON("/api/agents/custom/delete"' in chat_js
    assert "agent.deletable" in chat_js, "the rail asks the model, not the raw payload"
    assert 'deletable: agent.kind === "custom"' in chat_sidebar_js


def test_a_stale_install_is_reported_before_an_upload_fails(
    chat_js: str, chat_css: str
) -> None:
    """The old failure mode was a PDF upload dying with a package name."""
    assert 'api("/api/agents/capabilities")' in chat_js
    assert "capabilities.repair" in chat_js
    assert ".chat-alert" in chat_css


def test_run_progress_lives_in_the_message_not_in_permanent_chrome(
    chat_js: str, chat_css: str, chat_flow_js: str
) -> None:
    """A stage rail pinned above every conversation was chrome the reader paid
    for on every turn; progress now appears in the message making it."""
    assert 'from "./chat_flow.js"' in chat_js
    assert "applyEvent(state, event)" in chat_js
    assert "function showStep(" in chat_js and "function settleWork(" in chat_js
    for stage in ("scope", "evidence", "method", "verify", "propose"):
        assert f'id: "{stage}"' in chat_flow_js
        assert f"{stage}:" in chat_js, f"{stage} has no human-readable step text"
    assert ".chat-step" in chat_css and ".chat-work" in chat_css
    assert 'class="chat-workflow"' not in chat_js, "the permanent rail is gone"


def test_a_proposal_card_shows_its_provenance(chat_js: str, chat_css: str) -> None:
    assert "proposal.method" in chat_js
    assert "proposal.source" in chat_js
    assert "provenance marker missing" in chat_js
    assert ".chat-proposal-facts" in chat_css


def test_standing_instructions_reach_the_agent_not_the_message(chat_js: str) -> None:
    assert "additional_instructions: el(\"system\").value.trim() || undefined" in chat_js
    assert "openInstructions" in chat_js
    assert 'data-act="instructions"' in chat_js


def test_the_workspace_explains_the_agent_instead_of_the_transcript(
    chat_js: str, chat_css: str, chat_workspace_js: str
) -> None:
    """"What is this thing and what can it reach" has its own panel now."""
    assert 'api(`/api/agents/workspace?' in chat_js
    assert "workspaceModel(payload)" in chat_js
    for name in ("wsOverview", "wsTools", "wsFiles", "wsSettings"):
        assert f"function {name}(" in chat_js, name
    for tab in ("overview", "tools", "files", "settings"):
        assert f'id: "{tab}"' in chat_workspace_js
    assert ".chat-ws-tab" in chat_css and ".chat-ws-pipeline" in chat_css


def test_a_rejected_token_is_forgotten_and_explained_as_a_link_problem(
    script: str,
) -> None:
    """A bookmarked /viewer URL with no #t= reused a dead token forever."""
    assert "function forgetStaleToken()" in script
    assert script.count("forgetStaleToken();") == 2, "both the fetch 401 and the WS 4401"
    assert "const tokenFromLink = hashParams.has(\"t\")" in script
    assert "missing its #t= access token" in script
    assert "Viewer authorization expired" not in script


def test_no_entrance_animation_can_strand_an_element(chat_css: str) -> None:
    """`both` fill on an initially display:none panel left it 12px off its
    anchor permanently, because the animation never started."""
    assert "animation:" in chat_css
    for line in chat_css.splitlines():
        stripped = line.strip()
        if stripped.startswith("animation:") and "none" not in stripped:
            assert " both" not in stripped, f"use forwards, not both: {stripped}"


def test_nothing_animates_layout(chat_css: str) -> None:
    """Animating width on a grid item fights the layout engine and jams."""
    assert "transition: width" not in chat_css
    assert "transition: height" not in chat_css


def test_the_sidebar_does_not_open_on_hover(chat_js: str) -> None:
    """It opened whenever the pointer crossed the left edge on its way past."""
    assert "pointerenter" not in chat_js
    assert "pointerleave" not in chat_js
    assert 'rail.addEventListener("focusin", expandRail)' in chat_js, (
        "keyboard focus must still reveal it"
    )
    assert 'data-act="toggle-rail"' in chat_js and 'data-act="pin-rail"' in chat_js


def test_the_panel_cannot_widen_the_viewer_it_is_docked_in(chat_css: str) -> None:
    """The dock must never be able to push its host into a sideways scroll."""
    block = chat_css.split(".chat-root {")[2].split("}", 1)[0]
    assert "overflow: hidden" in block
    assert "grid-template-columns" in block


def test_chat_layout_responds_to_its_container_not_only_the_viewport(
    chat_js: str, chat_css: str
) -> None:
    """A narrow dock can live inside a wide browser, so viewport breakpoints
    alone squeeze the conversation between a rail and workspace."""
    assert "new ResizeObserver(syncShellLayout)" in chat_js
    assert 'root.classList.toggle("chat-compact", compact)' in chat_js
    assert 'root.classList.toggle("chat-overlay", overlay)' in chat_js
    assert ".chat-root.chat-compact" in chat_css
    assert ".chat-root.chat-overlay .chat-workspace" in chat_css


def test_compact_chat_drawers_have_a_dismissible_scrim(chat_js: str, chat_css: str) -> None:
    assert 'class="chat-shell-scrim"' in chat_js
    assert 'action === "close-overlays"' in chat_js
    assert ".chat-shell-scrim" in chat_css
    assert 'workspace").setAttribute("aria-modal", "true")' in chat_js
    assert 'root.classList.contains("rail-open")' in chat_js


def test_the_workspace_overlay_is_anchored_to_the_whole_grid(chat_css: str) -> None:
    """An abspos grid child anchors to its grid area, not the container."""
    overlay = chat_css.split("@media (max-width: 1100px)", 1)[1]
    assert "grid-area: 1 / 1 / -1 / -1" in overlay
    assert "width: min(var(--ws-width), 92%)" in overlay
