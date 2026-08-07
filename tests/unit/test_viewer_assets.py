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


def test_every_element_the_script_looks_up_exists(html: str, script: str) -> None:
    ids = set(re.findall(r'id="([^"]+)"', html))
    looked_up = set(re.findall(r'\$\("([^"]+)"\)', script))
    assert not looked_up - ids, f"app.js reads ids that index.html does not define: {looked_up - ids}"


def test_panel_ids_passed_as_strings_exist(html: str, script: str) -> None:
    """initSidePanel and POPOVERS name their elements in string literals."""
    ids = set(re.findall(r'id="([^"]+)"', html))
    for name in ("tree-panel", "props-panel", "split-tree", "split-props",
                 "settings-panel", "help-panel", "tools-panel"):
        assert name in ids


def test_search_and_saved_view_controls_are_present(html: str) -> None:
    for name in ("search-input", "search-clear", "search-results",
                 "saved-views", "view-name", "tool-save-view"):
        assert f'id="{name}"' in html, f"{name} is missing from index.html"


def test_no_style_attributes_or_inline_scripts(html: str) -> None:
    """The CSP forbids both; a violation only shows up in a browser console."""
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"<style[\s>]", html)


def test_stylesheet_defines_every_custom_property_it_uses(styles: str) -> None:
    used = set(re.findall(r"var\((--[a-z-]+)", styles))
    defined = set(re.findall(r"^\s*(--[a-z-]+):", styles, re.MULTILINE))
    assert not used - defined, f"undefined CSS variables: {sorted(used - defined)}"


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


def test_settings_are_a_dialog_not_an_inline_panel(chat_js: str, chat_css: str):
    """An inline settings panel squeezed the conversation; it is a modal now."""
    assert 'class="chat-modal"' in chat_js and 'class="chat-dialog"' in chat_js
    assert ".chat-modal { position: absolute" in chat_css
    assert "chat-scrim" in chat_js, "the dialog needs a scrim to close on"


def test_escape_closes_the_dialog_before_stopping_the_stream(chat_js: str):
    handler = chat_js.split('root.addEventListener("keydown"', 1)[1].split("});", 1)[0]
    assert handler.index("closeSettings()") < handler.index("aborter?.abort()")


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


def test_the_transcript_survives_a_reload_but_not_the_tab(chat_js: str):
    assert "sessionStorage.setItem(HISTORY" in chat_js
    assert "localStorage.setItem(HISTORY" not in chat_js


def test_tool_chips_are_paired_by_id_not_by_name(chat_js: str):
    """Two calls to one tool in a round left a chip spinning forever."""
    assert "pending.set(event.id" in chat_js
    assert "pending.get(event.id" in chat_js
    assert "pending.get(event.name" not in chat_js
