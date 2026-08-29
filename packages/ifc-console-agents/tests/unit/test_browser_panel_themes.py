"""Agent browser surfaces stay synchronized with the core viewer palettes."""

from __future__ import annotations

import re

from ifc_console.themes import THEME_IDS
from ifc_console.viewer import assets as viewer_assets

from ifc_console_agents import assets as agent_assets

AGENT_STATIC = agent_assets.require_static_dir()
VIEWER_STATIC = viewer_assets.require_static_dir()


def test_web_surfaces_offer_and_load_named_palettes() -> None:
    index = (VIEWER_STATIC / "index.html").read_text(encoding="utf-8")
    chat = (AGENT_STATIC / "chat.html").read_text(encoding="utf-8")
    app_js = (VIEWER_STATIC / "app.js").read_text(encoding="utf-8")
    app_css = (VIEWER_STATIC / "app.css").read_text(encoding="utf-8")
    chat_js = (AGENT_STATIC / "chat.js").read_text(encoding="utf-8")
    chat_css = (AGENT_STATIC / "chat.css").read_text(encoding="utf-8")
    theme_css = (VIEWER_STATIC / "themes.css").read_text(encoding="utf-8")

    assert "/viewer/static/themes.css" in index
    assert "/viewer/static/themes.css" in chat
    assert index.index("/viewer/static/app.css") < index.index(
        "/viewer/static/themes.css"
    )
    assert "/agents/static/chat.css" not in index
    assert chat.index("/agents/static/chat.css") < chat.index(
        "/viewer/static/themes.css"
    )
    picker = index.split('id="set-theme"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="([^"]+)">', picker) == list(THEME_IDS)
    for name in THEME_IDS:
        assert f'value="{name}"' in index
        assert f'value="{name}"' in chat_js
        assert f"{name}: {{ canvas:" in app_js
    for name in ("dark", "modern"):
        assert f':root[data-theme="{name}"]' in theme_css
        assert f'.chat-root[data-theme="{name}"]' in theme_css
        assert f'html[data-console-theme="{name}"] body.chat-page' in theme_css
    assert ':root[data-theme="light"]' in app_css
    assert '.chat-root[data-theme="light"]' in chat_css


def test_viewer_theme_always_reaches_the_chat_and_agent_workspace() -> None:
    app_js = (VIEWER_STATIC / "app.js").read_text(encoding="utf-8")
    chat_js = (AGENT_STATIC / "chat.js").read_text(encoding="utf-8")
    chat_page_js = (AGENT_STATIC / "chat-page.js").read_text(encoding="utf-8")

    paint = app_js.split("function paintTheme(theme)", 1)[1].split(
        "function applyTheme(name)", 1
    )[0]
    assert 'scheduleViewerContext("theme")' in paint

    listener = chat_js.split("function applyViewerContext(detail)", 1)[1].split(
        "async function handleViewerResult", 1
    )[0]
    assert "applyThemePreference(viewerTheme);" in listener
    assert "rememberThemePreference(viewerTheme);" in listener
    assert 'settings.theme === "system"' not in listener
    assert "viewer.subscribe(applyViewerContext)" in chat_js
    assert "document.documentElement.dataset.consoleTheme = resolved;" in chat_js
    assert (
        "document.documentElement.dataset.consoleTheme = root.dataset.theme"
        in chat_page_js
    )


def test_chat_status_colors_follow_the_shared_semantic_palette() -> None:
    chat_css = (AGENT_STATIC / "chat.css").read_text(encoding="utf-8")

    assert "--chat-ok: var(--ok, var(--chat-accent-bright));" in chat_css
    assert "--chat-warn: var(--warn, var(--chat-accent-bright));" in chat_css
    assert "--chat-bad: var(--danger, var(--chat-accent-bright));" in chat_css
