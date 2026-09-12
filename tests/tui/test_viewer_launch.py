"""Viewer targets must retain one model session and an exact usable URL."""

from __future__ import annotations

import webbrowser

import pytest

from ifc_console.tui import commands
from tests.tui.test_commands import FakeConsole


@pytest.mark.parametrize("command", ["/viewer", "/viewer browser", "/viewer vscode"])
async def test_viewer_targets_reuse_the_loaded_session(core, work_model, monkeypatch, command):
    await core.open_model(work_model)
    core.server_running = True
    console = FakeConsole(core)
    session = core.session
    revision = session.revision
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    await commands.dispatch(console, command)

    assert console.clipboard == core.viewer_url
    assert console.clipboard in console.text
    assert core.session is session and session.revision == revision
    assert len(core.models.sessions) == 1
    assert core.viewer_hub.clients == []
    assert "model ready" not in console.text
    if command.endswith("vscode"):
        assert opened == []
        assert "Browser: Open Integrated Browser" in console.text
        assert "workbench.browser.openLocalhostLinks" in console.text
    else:
        assert opened == [core.viewer_url]
        assert "browser launch requested" in console.text


@pytest.mark.parametrize("failure", ["false", "exception"])
async def test_vscode_link_survives_clipboard_failure(core, monkeypatch, failure):
    core.server_running = True
    console = FakeConsole(core)

    def copy(_url):
        if failure == "exception":
            raise RuntimeError("clipboard unavailable")
        return False

    monkeypatch.setattr(console, "copy_to_clipboard", copy)
    await commands.dispatch(console, "/viewer vscode")
    assert "URL copied" not in console.text
    assert "copy the full URL" in console.text
    assert core.viewer_url in console.text


async def test_browser_failure_preserves_the_link(core, monkeypatch):
    core.server_running = True
    console = FakeConsole(core)

    def failed(_url):
        raise OSError("no browser")

    monkeypatch.setattr(webbrowser, "open", failed)
    await commands.dispatch(console, "/viewer browser")
    assert "browser did not open" in console.text
    assert "browser launch requested" not in console.text
    assert console.clipboard == core.viewer_url


async def test_invalid_viewer_target_has_no_side_effects(core, monkeypatch):
    core.server_running = True
    console = FakeConsole(core)
    calls = []
    monkeypatch.setattr(core, "enable_viewer", lambda: calls.append(True))
    await commands.dispatch(console, "/viewer vscode extra")
    assert "usage: /viewer" in console.text
    assert not calls
    assert console.clipboard == ""
