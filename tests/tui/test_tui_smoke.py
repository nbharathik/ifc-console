"""Console TUI paths via Textual Pilot."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Static

from ifc_console.app import AppCore
from ifc_console.policy.modes import Mode
from ifc_console.settings import SettingsStore
from ifc_console.tui.app import IfcConsoleApp
from ifc_console.tui.console import CommandInput, ConsoleScreen
from ifc_console.tui.launcher import FilePickerModal
from ifc_console.tui.modals import ConfirmModal, QuitModal

pytestmark = pytest.mark.asyncio


def _core(tmp_path: Path) -> AppCore:
    store = SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={})
    return AppCore(store)


def _app(tmp_path: Path) -> IfcConsoleApp:
    return IfcConsoleApp(_core(tmp_path), autostart=False)


async def _type_command(pilot, app: IfcConsoleApp, line: str) -> None:
    prompt = app.screen.query_one("#prompt", CommandInput)
    prompt.value = line
    await pilot.press("enter")
    await pilot.pause()


async def test_console_mounts_with_prompt_focused(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert isinstance(app.screen, ConsoleScreen)
        prompt = app.screen.query_one("#prompt", CommandInput)
        assert app.focused is prompt
        await app._teardown()


async def test_the_log_opens_empty_and_server_start_only_refreshes_status(
    tmp_path: Path,
) -> None:
    """No welcome banner: the feed starts empty and stays that way until work."""
    messages: list[str] = []
    refreshes: list[bool] = []
    console = SimpleNamespace(
        core=_core(tmp_path),
        print=messages.append,
        refresh_status=lambda: refreshes.append(True),
    )

    ConsoleScreen.on_core_event(console, {"type": "server_started"})
    assert refreshes == [True]
    assert messages == []


async def test_ctrl_c_quits_clean_session(tmp_path: Path) -> None:
    """Ctrl+C is the standard terminal exit; nothing unsaved means no questions."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        for _ in range(100):
            if not app.is_running:
                break
            await pilot.pause(0.05)
    assert app.return_value == 0


async def test_ctrl_c_copies_selection_instead_of_quitting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(tmp_path)
    cleared: list[bool] = []
    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(
            ConsoleScreen, "get_selected_text", lambda _self: "complete client config"
        )
        monkeypatch.setattr(ConsoleScreen, "clear_selection", lambda _self: cleared.append(True))
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.is_running
        assert app.clipboard == "complete client config"
        assert cleared == [True]
        await app._teardown()


async def test_ctrl_c_with_unsaved_changes_asks(tmp_path: Path, work_model: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app, f"/open {work_model}")
        for _ in range(100):
            if app.core.session.loaded:
                break
            await pilot.pause(0.05)
        app.core.session.mark_dirty()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert isinstance(app.screen, QuitModal)
        await pilot.press("ctrl+c")  # a second ctrl+c must not stack another dialog
        await pilot.pause()
        assert sum(isinstance(s, QuitModal) for s in app.screen_stack) == 1
        await pilot.press("escape")  # cancel: still running, still dirty
        await pilot.pause()
        assert app.is_running and app.core.session.dirty
        await app._teardown()


async def test_mode_command_with_escalation_confirm(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.core.policy.mode is Mode.ASK  # ask is the default
        await _type_command(pilot, app, "/mode edit")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmModal)  # escalation needs a yes
        await pilot.press("y")
        await pilot.pause()
        assert app.core.policy.mode is Mode.EDIT
        # de-escalation applies without a confirm
        await _type_command(pilot, app, "/mode ask")
        await pilot.pause()
        assert app.core.policy.mode is Mode.ASK
        await app._teardown()


async def test_file_command_opens_picker_and_escape_cancels(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app, "/file")
        await pilot.pause()
        assert isinstance(app.screen, FilePickerModal)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(app.screen, ConsoleScreen)
        await app._teardown()


async def test_file_picker_lists_and_opens_model(
    tmp_path: Path, work_model: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(work_model.parent)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app, "/file")
        await pilot.pause()
        picker = app.screen
        assert isinstance(picker, FilePickerModal)
        assert any(p.name == "work.ifc" for p in picker._visible)
        await pilot.press("enter")  # open the highlighted (only) file
        await pilot.pause()
        for _ in range(50):  # loading happens in a worker
            if app.core.session.loaded:
                break
            await pilot.pause(0.05)
        assert app.core.session.loaded
        assert app.core.session.name == "work.ifc"
        status = app.screen.query_one("#statusbar", Static)
        assert "work.ifc" in str(status.render())
        await app._teardown()


async def test_prompt_history_recall(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _type_command(pilot, app, "/status")
        await _type_command(pilot, app, "/recent")
        prompt = app.screen.query_one("#prompt", CommandInput)
        await pilot.press("up")
        assert prompt.value == "/recent"
        await pilot.press("up")
        assert prompt.value == "/status"
        await pilot.press("down")
        assert prompt.value == "/recent"
        await app._teardown()


async def test_console_autostart_serves_http(tmp_path: Path, work_model: Path) -> None:
    """The console boots the real MCP server without a model preselected."""
    import asyncio
    import socket

    import httpx

    from ifc_console.portcheck import IFC_CONSOLE, IFC_CONSOLE_OTHER, port_status

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    core = _core(tmp_path)
    core.port = port
    app = IfcConsoleApp(core, autostart=True)
    async with app.run_test() as pilot:
        for _ in range(100):
            if core.server_running:
                break
            await pilot.pause(0.05)
        assert core.server_running

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/api/status",
                headers={"Authorization": f"Bearer {core.token}"},
            )
        assert response.status_code == 200
        assert response.json()["model"] is None  # no model yet, by design

        # the port probe recognizes its own kind
        kind, _ = await asyncio.to_thread(port_status, port, core.token)
        assert kind == IFC_CONSOLE
        kind, _ = await asyncio.to_thread(port_status, port, "wrong-token")
        assert kind == IFC_CONSOLE_OTHER

        await _type_command(pilot, app, f"/open {work_model}")
        for _ in range(100):
            if core.session.loaded:
                break
            await pilot.pause(0.05)
        assert core.session.name == "work.ifc"
        await app._teardown()


async def test_console_reports_port_occupant(tmp_path: Path) -> None:
    """Autostart against an occupied port: the failure names the occupant."""
    import socket

    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]

        core = _core(tmp_path)
        core.port = port
        failures: list[dict] = []
        core.events.subscribe(
            lambda e: failures.append(e) if e["type"] == "server_failed" else None
        )
        app = IfcConsoleApp(core, autostart=True)
        async with app.run_test() as pilot:
            for _ in range(400):  # includes the ~2s identification probe
                if failures:
                    break
                await pilot.pause(0.05)
            assert failures, "server_failed event never fired"
            assert not core.server_running
            reason = failures[0]["reason"]
            assert "in use by" in reason
            await app._teardown()
