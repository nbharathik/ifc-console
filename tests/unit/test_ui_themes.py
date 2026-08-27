"""Named appearance palettes stay synchronized across every UI surface."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from ifc_console.settings import TuiSettings
from ifc_console.themes import RESOLVED_THEME_IDS, THEME_IDS, THEME_LABELS, resolve_theme

STATIC = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "ifc-console-viewer"
    / "src"
    / "ifc_console_viewer"
    / "static"
)


def test_named_theme_catalog_is_stable_and_settings_accept_every_choice() -> None:
    assert THEME_IDS == ("light", "dark", "modern", "blue")
    assert len(set(THEME_LABELS.values())) == len(THEME_LABELS)
    for name in (*THEME_IDS, "auto"):
        assert TuiSettings(theme=name).theme == name
    assert TuiSettings().theme == "blue"
    assert resolve_theme("auto") == "blue"
    assert resolve_theme("unknown") == "blue"


def test_textual_registers_every_resolved_palette() -> None:
    from ifc_console.tui.app import THEMES

    assert tuple(THEMES) == RESOLVED_THEME_IDS
    for name in THEME_IDS:
        assert THEMES[name].name == f"ifc-{name}"
        assert THEMES[name].dark is (name != "light")
        assert THEMES[name].success == THEMES[name].warning == THEMES[name].error


@pytest.mark.parametrize("name", THEME_IDS)
async def test_named_theme_broadcasts_to_viewer(core, name: str) -> None:
    from tests.unit.test_viewer_hub import FakeWS

    core.enable_viewer()
    ws = FakeWS(hub=core.viewer_hub)
    ws.client = core.viewer_hub.register(ws)
    assert core.set_ui_theme(name) == name
    await asyncio.sleep(0)
    assert ws.frames("theme")[-1]["theme"] == name
    assert core.viewer_hub.status_payload()["theme"] == name


def test_web_surfaces_offer_and_load_named_palettes() -> None:
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    chat = (STATIC / "chat.html").read_text(encoding="utf-8")
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    chat_js = (STATIC / "chat.js").read_text(encoding="utf-8")
    chat_css = (STATIC / "chat.css").read_text(encoding="utf-8")
    theme_css = (STATIC / "themes.css").read_text(encoding="utf-8")

    assert '/viewer/static/themes.css' in index
    assert '/viewer/static/themes.css' in chat
    assert index.index('/viewer/static/app.css') < index.index('/viewer/static/themes.css')
    assert index.index('/viewer/static/chat.css') < index.index('/viewer/static/themes.css')
    assert chat.index('/viewer/static/chat.css') < chat.index('/viewer/static/themes.css')
    picker = index.split('id="set-theme"', 1)[1].split("</select>", 1)[0]
    assert re.findall(r'<option value="([^"]+)">', picker) == list(THEME_IDS)
    for name in THEME_IDS:
        assert f'value="{name}"' in index
        assert f'value="{name}"' in chat_js
        assert f'{name}: {{ canvas:' in app_js
    for name in ("dark", "modern"):
        assert f':root[data-theme="{name}"]' in theme_css
        assert f'.chat-root[data-theme="{name}"]' in theme_css
        assert f'html[data-console-theme="{name}"] body.chat-page' in theme_css
    assert ':root[data-theme="light"]' in app_css
    assert '.chat-root[data-theme="light"]' in chat_css


def test_viewer_theme_always_reaches_the_chat_and_agent_workspace() -> None:
    app_js = (STATIC / "app.js").read_text(encoding="utf-8")
    chat_js = (STATIC / "chat.js").read_text(encoding="utf-8")
    chat_page_js = (STATIC / "chat-page.js").read_text(encoding="utf-8")

    paint = app_js.split("function paintTheme(theme)", 1)[1].split(
        "function applyTheme(name)", 1
    )[0]
    assert 'scheduleViewerContext("theme")' in paint

    listener = chat_js.split(
        'document.addEventListener("ifc-console:viewer-context"', 1
    )[1].split(
        'document.addEventListener("ifc-console:viewer-result"', 1
    )[0]
    assert "applyThemePreference(viewerTheme);" in listener
    assert "rememberThemePreference(viewerTheme);" in listener
    assert 'settings.theme === "system"' not in listener
    assert "document.documentElement.dataset.consoleTheme = resolved;" in chat_js
    assert "document.documentElement.dataset.consoleTheme = root.dataset.theme" in chat_page_js


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


def test_named_theme_text_remains_readable_at_every_hierarchy() -> None:
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    theme_css = (STATIC / "themes.css").read_text(encoding="utf-8")
    blocks = {
        "blue": app_css.split(":root {", 1)[1].split("}", 1)[0],
        "light": app_css.split(':root[data-theme="light"]', 1)[1].split("}", 1)[0],
        **{
            name: theme_css.split(f':root[data-theme="{name}"]', 1)[1].split("}", 1)[0]
            for name in ("dark", "modern")
        },
    }
    for name, block in blocks.items():
        colors = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6});", block))
        for foreground in ("text", "text-muted", "text-quiet", "accent-bright"):
            for background in ("canvas", "chrome", "surface"):
                assert _contrast(colors[foreground], colors[background]) >= 4.5, (
                    name,
                    foreground,
                    background,
                )


def test_dark_theme_stays_cool_neutral_instead_of_brown_or_blue() -> None:
    theme_css = (STATIC / "themes.css").read_text(encoding="utf-8")
    block = theme_css.split(':root[data-theme="dark"]', 1)[1].split("}", 1)[0]
    colors = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6});", block))

    for token in (
        "canvas",
        "chrome",
        "surface",
        "surface-hover",
        "surface-active",
        "line",
        "line-strong",
    ):
        red, green, blue = (
            int(colors[token][index : index + 2], 16) for index in (1, 3, 5)
        )
        assert max(red, green, blue) - min(red, green, blue) <= 16
        assert blue >= red


def test_modern_theme_uses_a_quiet_grey_highlight() -> None:
    theme_css = (STATIC / "themes.css").read_text(encoding="utf-8")
    block = theme_css.split(':root[data-theme="modern"]', 1)[1].split("}", 1)[0]
    colors = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6});", block))

    for token in ("accent", "accent-bright"):
        channels = [int(colors[token][index : index + 2], 16) for index in (1, 3, 5)]
        assert max(channels) - min(channels) <= 16
    assert _contrast(colors["accent-bright"], colors["surface"]) < _contrast(
        colors["text"], colors["surface"]
    )


def test_each_palette_uses_one_accent_family_for_ui_semantics() -> None:
    app_css = (STATIC / "app.css").read_text(encoding="utf-8")
    theme_css = (STATIC / "themes.css").read_text(encoding="utf-8")
    chat_css = (STATIC / "chat.css").read_text(encoding="utf-8")

    for source, palettes in ((app_css, 2), (theme_css, 2)):
        for token, value in (
            ("measure", "accent"),
            ("measure-bright", "accent-bright"),
            ("measure-wash", "accent-wash"),
            ("measure-line", "accent-line"),
            ("ok", "accent-bright"),
            ("warn", "accent-bright"),
            ("danger", "accent-bright"),
            ("mode-ask", "accent-bright"),
            ("mode-edit", "accent-bright"),
        ):
            assert source.count(f"--{token}: var(--{value});") == palettes

    assert "--chat-ok: var(--ok, var(--chat-accent-bright));" in chat_css
    assert "--chat-warn: var(--warn, var(--chat-accent-bright));" in chat_css
    assert "--chat-bad: var(--danger, var(--chat-accent-bright));" in chat_css
