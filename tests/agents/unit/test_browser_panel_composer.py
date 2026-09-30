"""Static checks for the composer, the + menu, Settings, and the workspace tabs.

The menu's rows are data (`plusMenuModel`) and are tested in Node; these checks
cover the wiring between that data, the markup, and the stylesheet.
"""

from __future__ import annotations

import re

import pytest

from ifc_console.agents import assets as agent_assets

AGENT_STATIC = agent_assets.require_static_dir()
# The end of a top-level function body in the panel modules.
SPLIT_BLOCK_END = chr(10) + "  }"


def _asset(name: str) -> str:
    return (AGENT_STATIC / name).read_text(encoding="utf-8")


def _template(chat_js: str) -> str:
    return chat_js.split("const TEMPLATE = `", 1)[1].split("\n`;", 1)[0]


@pytest.fixture(scope="module")
def chat_js() -> str:
    return _asset("chat.js")


@pytest.fixture(scope="module")
def chat_css() -> str:
    return _asset("chat.css")


@pytest.fixture(scope="module")
def chat_flow_js() -> str:
    return _asset("chat_flow.js")


@pytest.fixture(scope="module")
def chat_memory_js() -> str:
    return _asset("chat_memory.js")


@pytest.fixture(scope="module")
def chat_workspace_js() -> str:
    return _asset("chat_workspace.js")


def test_the_composer_holds_four_controls(chat_js: str) -> None:
    toolbar = _template(chat_js).split('<div class="chat-input-toolbar">', 1)[1].split(
        "<!-- Scrolling back", 1
    )[0]
    rail = toolbar.split('<div class="chat-plus-menu"', 1)[0]
    # +, the model pill, and Ask/Edit sit in the rail; Send sits beside it
    assert len(re.findall(r"<(?:button|select)\b[^>]*>", rail)) == 3
    assert 'data-act="send"' in toolbar
    modes = chat_js.split('data-role="session-mode"', 1)[1].split("</select>", 1)[0]
    assert '"ask"' in modes and '"edit"' in modes and '"auto"' not in modes
    for gone in ("session-autonomy", "ifcmodel", 'data-act="shortcuts"', "skills-picker", "chat-memory"):
        assert gone not in toolbar, gone


def test_the_gear_opens_the_workspace_and_every_model_door_opens_settings(chat_js: str) -> None:
    template = _template(chat_js)
    header = template.split('<header class="chat-head">', 1)[1].split("</header>", 1)[0]
    assert header.count('data-act="workspace"') == 1
    assert template.count('data-act="workspace"') == 1, "the sidebar foot button is gone"
    assert 'data-role="skills-picker"' not in template
    assert "skills-picker-menu" not in chat_js and "renderSkillPicker" not in chat_js
    pill = re.search(r'<button class="chat-composer-pill chat-model-pill[^>]+>', template)
    assert pill is not None
    for attribute in ('data-act="settings"', 'aria-controls="chat-settings"', 'aria-haspopup="dialog"'):
        assert attribute in pill.group(0), attribute
    assert len(re.findall(r'(?<!\[)data-act="settings"', chat_js)) == 2, "pill and first-run setup"
    assert 'settings: () => openSettings(act("plus"))' in chat_js
    assert '{ name: "model", hint: "Choose the AI model", run: () => openSettings(input) }' in chat_js
    assert 'action === "settings") openSettings(actionButton)' in chat_js
    assert "openSettings();" in chat_js.split("async function submit()", 1)[1]
    assert 'openWorkspace(trigger, "models")' not in chat_js
    assert 'workspaceView === "models"' not in chat_js


def test_workspace_has_three_tabs_and_settings_is_its_own_dialog(chat_js: str, chat_css: str) -> None:
    template = _template(chat_js)
    workspace = template.split('<dialog class="chat-workspace"', 1)[1].split("</dialog>", 1)[0]
    assert set(re.findall(r'data-workspace-view="([\w-]+)"', workspace)) == {"agent", "content", "skills"}
    assert "settings-models" not in workspace and "settings-app" not in workspace
    assert "chat-workspace-nav-label" not in template
    settings = template.split('<dialog class="chat-settings"', 1)[1].split("</dialog>", 1)[0]
    assert 'id="chat-settings"' in template and 'data-role="settings"' in template
    assert 'data-act="close-settings"' in settings
    for role in ("settings-models", "settings-app", "provider", "model", "key", "savekey", "toolcap", "theme", "caps"):
        assert f'data-role="{role}"' in settings, role
    assert 'el(app ? "settings-app" : "settings-models").offsetTop' in chat_js
    assert "dialog.chat-settings" in chat_css and ".chat-settings-pane" not in chat_css


def test_shortcuts_open_from_the_plus_menu(chat_js: str, chat_css: str) -> None:
    assert "shortcuts: () => openShortcuts()," in chat_js
    assert 'data-act="shortcuts"' not in chat_js and "chat-shortcuts-toggle" not in chat_js
    assert 'role="dialog" aria-label="Keyboard shortcuts"' in chat_js
    assert 'focusQuietly(act("plus"))' in chat_js
    for copy in (
        "Send a message",
        "Start a new line",
        "Queue a message while a response is running",
        "Stop the active response",
    ):
        assert copy in chat_js
    assert ".chat-shortcuts[hidden] { display: none; }" in chat_css
    chain = chat_js.split('if (event.key !== "Escape") return;', 1)[1].split("event.stopPropagation();", 1)[0]
    assert chain.index("closeShortcuts") < chain.index("stopRun()")


def test_the_plus_menu_draws_the_model_and_confirms_auto(chat_js: str, chat_css: str) -> None:
    assert 'aria-haspopup="menu"' in chat_js
    assert "function setSkillPinned(" in chat_js and 'item.id.startsWith("skill:")' in chat_js
    assert '"menuitemcheckbox"' in chat_js and '"menuitemradio"' in chat_js
    assert 'sendViewerCommand({ action: "set-model", modelId: item.id.slice(6) })' in chat_js
    # opening it is a menu, so it closes on Escape and on any outside click
    assert 'if (!within(".chat-plus-menu, .chat-plus")) closePlusMenu();' in chat_js
    assert 'else if (!el("plus-menu").hidden) closePlusMenu({ restoreFocus: true });' in chat_js
    keys = chat_js.split('el("plus-menu").addEventListener("keydown"', 1)[1].split("});", 1)[0]
    assert '["ArrowUp", "ArrowDown", "Home", "End"]' in keys
    # Auto is only reached through the inline confirmation, and the pill says when it is on
    ask = chat_js.split("function activatePlusItem(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert 'autonomyRequest(sessionAutonomy()) === "confirm"' in ask and "plusConfirmAuto = true;" in ask
    assert 'postJSON("/api/session/mode", { ...patch, confirmed: true })' in chat_js
    assert '.classList.toggle("auto", sessionAutonomy() === "auto")' in chat_js
    for selector in (".chat-plus-tick", ".chat-plus-confirm", ".chat-mode-select.auto em"):
        assert selector in chat_css, selector
    assert ".chat-autonomy-select" not in chat_css


def test_memory_is_a_plus_menu_row_with_a_dot_and_a_line_from_high_up(
    chat_js: str, chat_css: str, chat_memory_js: str
) -> None:
    assert 'data-role="memory"' not in _template(chat_js) and "chat-memory" not in chat_css
    assert "memory: memoryMenuItem(memoryState)," in chat_js
    assert "memory: () => relieveMemory()," in chat_js
    render = chat_js.split("function renderMemory()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "plus.dataset.memory = pressure" in render and "syncPlusMenu();" in render
    assert "memory is ${pressure}: use <b>Free memory</b> in the + menu" in chat_js
    assert '.chat-root .chat-plus[data-memory="critical"]::after' in chat_css
    assert "export function memoryPressure(" in chat_memory_js
    snapshot = chat_js.split("function memorySnapshot()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    for source in ("heap: sampleHeap()", "server: sessionStatus.memory", "sessionStatus.viewer_memory", "turns,"):
        assert source in snapshot, source
    # Automatic relief is rate limited; a press is not.
    tick = chat_js.split("function memoryTick()", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "Date.now() - memoryRelievedAt > 60_000" in tick
    relief = chat_js.split("function relieveMemory(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert 'action: "release-memory"' in relief and "trimTranscriptMemory(plan.keepTurns)" in relief
    trim = chat_js.split("function trimTranscriptMemory(", 1)[1].split(SPLIT_BLOCK_END, 1)[0]
    assert "block.output = null;" in trim
    assert "export function reliefPlan(" in chat_memory_js
    # The console's reading rides on /api/status; the viewer's on its context.
    assert "viewer_memory: viewerOpen" in chat_js
    assert "sessionStatus = { ...nextStatus, viewer_memory: sessionStatus.viewer_memory || null };" in chat_js


def test_code_with_no_caller_stays_deleted(
    chat_flow_js: str, chat_workspace_js: str, chat_css: str
) -> None:
    for name in ("stagesForTools", "export function timeline("):
        assert name not in chat_flow_js, name
    for name in ("tabsFor", "TABS"):
        assert name not in chat_workspace_js, name
    for selector in (".chat-agent-switcher", ".chat-ws-blocks", ".chat-skill-picker", ".chat-side-foot"):
        assert selector not in chat_css, selector
