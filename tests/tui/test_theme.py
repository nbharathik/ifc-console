"""Theme system: palette, app application, /theme command, viewer sync."""

from __future__ import annotations

import asyncio

import pytest

from ifc_console.branding import CATEGORICAL, categorical_color
from ifc_console.tui import commands
from tests.tui.test_commands import FakeConsole

pytestmark = pytest.mark.asyncio


def test_categorical_palette_shape():
    assert len(CATEGORICAL) == 8
    assert len(set(CATEGORICAL)) == 8
    assert all(c.startswith("#") and len(c) == 7 for c in CATEGORICAL)
    assert categorical_color(0) == CATEGORICAL[0]
    assert categorical_color(len(CATEGORICAL)) == CATEGORICAL[0]  # wraps


async def test_app_applies_theme_setting(core):
    from ifc_console.tui.app import IfcConsoleApp

    core.ui_theme = "light"
    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test():
        assert app.theme == "ifc-light"


async def test_apply_theme_switches_and_persists(core):
    from ifc_console.tui.app import IfcConsoleApp

    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test():
        assert app.theme == "ifc-dark"
        app.apply_theme("light", persist=True)
        assert app.theme == "ifc-light"
    assert core.ui_theme == "light"
    assert core.store.settings.tui.theme == "light"


async def test_theme_command_without_textual(core):
    core.start_audit()
    console = FakeConsole(core)
    await commands.dispatch(console, "/theme")
    assert "theme: dark" in console.text
    console.clear_log()
    await commands.dispatch(console, "/theme light")
    assert core.ui_theme == "light"
    assert "theme set to light" in console.text
    console.clear_log()
    await commands.dispatch(console, "/theme neon")
    assert "unknown theme" in console.text


async def test_theme_change_broadcasts_to_viewer_tabs(core):
    from tests.unit.test_viewer_hub import FakeWS

    core.enable_viewer()
    ws = FakeWS(hub=core.viewer_hub)
    ws.client = core.viewer_hub.register(ws)
    assert core.viewer_hub.status_payload()["theme"] == "dark"
    core.set_ui_theme("light")
    await asyncio.sleep(0)  # broadcast is scheduled on the loop
    frames = ws.frames("theme")
    assert frames and frames[-1]["theme"] == "light"
    assert core.viewer_hub.status_payload()["theme"] == "light"
