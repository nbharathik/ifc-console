"""Client choice and model-scoped status for the shared console workflow."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from textual.widgets import OptionList

from ifc_console.app import AppCore
from ifc_console.extensions import ExtensionManager
from ifc_console.settings import SettingsStore
from ifc_console.tui import commands, completion
from ifc_console.tui.app import IfcConsoleApp
from ifc_console.tui.console import CommandInput, ConsoleScreen
from ifc_console.viewer.hub import ViewerClient


class Console:
    def __init__(self, core: AppCore) -> None:
        self.core = core
        self.app = self
        self.lines: list[str] = []
        self.clipboard = "unchanged"
        self.last_tool_activity = None

    def print(self, markup: str) -> None:
        self.lines.append(markup)

    def refresh_status(self) -> None:
        pass

    def copy_to_clipboard(self, value: str) -> None:
        self.clipboard = value

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def console(core) -> Console:
    return Console(core)


async def test_bare_connect_requires_a_client_without_copying(console) -> None:
    await commands.dispatch(console, "/connect")
    assert console.clipboard == "unchanged"
    assert "choose an MCP client" in console.text
    for client in commands._CONNECT_CLIENTS:
        assert f"/connect {client}" in console.text
    assert "setup copied" not in console.text
    assert "shared console via stdio bridge" not in console.text


async def test_bare_connect_opens_inline_choices_and_can_select_codex(core, monkeypatch) -> None:
    monkeypatch.setattr(core, "start_knowledge", lambda: None)
    app = IfcConsoleApp(core, autostart=False)
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, ConsoleScreen)
        await commands.dispatch(screen, "/connect")
        await pilot.pause()
        prompt = screen.query_one("#prompt", CommandInput)
        menu = screen.query_one("#completions", OptionList)
        assert prompt.value == "/connect "
        assert menu.display
        assert app.focused is prompt
        assert copied == []
        codex_index = next(
            index
            for index, candidate in enumerate(screen._menu_state.candidates)
            if candidate.insert == "codex"
        )
        menu.highlighted = codex_index
        await pilot.press("enter")
        await pilot.pause()
        assert len(copied) == 1
        assert "[mcp_servers.ifc-console]" in copied[0]
        assert screen.last_tool_activity is None
        await app._teardown()


def test_connect_completion_has_choices_without_a_default(core) -> None:
    command = completion.complete("/connect", core).candidates[0]
    assert command.advance and not command.terminal
    choices = completion.complete("/connect ", core).candidates
    assert {item.insert for item in choices} == {*commands._CONNECT_CLIENTS, "all"}
    assert all("default" not in item.annotation for item in choices)


@pytest.mark.parametrize("failure", [False, RuntimeError("clipboard unavailable")])
async def test_connect_clipboard_failure_keeps_configuration_visible(
    console, monkeypatch, failure
) -> None:
    def cannot_copy(_value: str):
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr(console, "copy_to_clipboard", cannot_copy)
    await commands.dispatch(console, "/connect codex")
    assert "[mcp_servers.ifc-console]" in console.text
    assert "could not copy the setup" in console.text
    assert "setup copied to clipboard" not in console.text
    assert "active model ID/revision" in console.text
    assert "GlobalIds" in console.text
    assert "does not verify a client connection" in console.text


async def test_client_setup_remains_on_runtime_when_model_changes(console, work_model) -> None:
    console.core.port = 9341
    await console.core.open_model(work_model)
    await commands.dispatch(console, "/connect codex")
    initial_config = console.clipboard
    next_model = work_model.with_name("next model.ifc")
    shutil.copyfile(work_model, next_model)
    await console.core.open_model(next_model)
    console.lines.clear()
    await commands.dispatch(console, "/connect codex")
    assert console.clipboard == initial_config
    assert "9341" in console.clipboard
    assert work_model.name not in console.clipboard
    assert next_model.name not in console.clipboard
    assert "Codex connected" not in console.text
    console.lines.clear()
    await commands.dispatch(console, "/status")
    assert next_model.name in console.text
    assert str(console.core.models.active_id) in console.text
    assert "no tool activity observed" in console.text


async def test_status_distinguishes_active_and_selected_models(console, work_model) -> None:
    core = console.core
    await core.open_model(work_model)
    active_id = core.models.active_id
    attached_path = work_model.with_name("reference model.ifc")
    shutil.copyfile(work_model, attached_path)
    await core.open_model(attached_path, attach=True)
    attached_id = next(mid for mid in core.models.sessions if mid != active_id)
    client = ViewerClient(None)
    core.viewer_hub.clients.append(client)
    core.viewer.enabled = True
    core.viewer.connected = 1
    guid = core.session.ifc.by_type("IfcWall")[0].GlobalId
    await core.viewer_hub.handle_frame(
        client,
        {
            "type": "selection",
            "model_id": attached_id,
            "guids": [guid],
            "selections": [
                {"model_id": active_id, "guids": [guid]},
                {"model_id": attached_id, "guids": [guid]},
            ],
        },
    )
    await commands.dispatch(console, "/status")
    assert f"active   {active_id}" in console.text
    assert f"revision {core.session.fingerprint}:{core.session.revision}" in console.text
    assert "2 element(s) across 2 model(s)" in console.text
    assert f"{attached_id} (reference model.ifc; attached; read-only): 1 element(s)" in console.text
    assert "/use <model>" in console.text
    assert core.models.active_id == active_id
    core.viewer_hub.clients.clear()


async def test_status_reports_current_save_destination_after_save_as(console, work_model) -> None:
    core = console.core
    await core.open_model(work_model)
    await core.enter_edit_mode(by="user")
    working_copy = core.session.working_copy
    assert working_copy is not None
    target = work_model.with_name("reviewed output.ifc")
    await core.save_model(by="user", target=target)
    await commands.dispatch(console, "/status")
    assert f"original {work_model}" in console.text
    assert f"copy     {working_copy.path}" in console.text
    assert f"save to  {target}" in console.text
    assert "none unsaved" in console.text


@pytest.mark.parametrize(
    "state, expected",
    [
        ("empty", "/file to choose"),
        ("no-viewer", "/viewer to open"),
        ("no-selection", "select an element in the viewer"),
        ("dirty", "review the edits, then /save"),
    ],
)
async def test_status_next_action_uses_current_state(console, work_model, state, expected) -> None:
    core = console.core
    if state != "empty":
        await core.open_model(work_model)
    if state in {"no-selection", "dirty"}:
        core.viewer.enabled = True
        core.viewer.connected = 1
    if state == "dirty":
        core.session.dirty = True
        core.session.change_count = 1
    await commands.dispatch(console, "/status")
    assert expected in console.text


async def test_tool_activity_is_observed_without_claiming_client_connection(
    core, monkeypatch
) -> None:
    monkeypatch.setattr(core, "start_knowledge", lambda: None)
    app = IfcConsoleApp(core, autostart=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        event = {
            "type": "tool_called",
            "tool": "get_viewer_selection",
            "ok": True,
            "ts": "2026-09-11T19:10:00+00:00",
            "duration_ms": 2,
        }
        screen.on_core_event(event)
        assert screen.last_tool_activity == event
        lines: list[str] = []
        screen.print = lines.append
        await commands.dispatch(screen, "/status")
        text = "\n".join(lines)
        assert "last observed: get_viewer_selection" in text
        assert "2026-09-11T19:10:00+00:00" in text
        assert "Codex connected" not in text
        await app._teardown()


async def test_status_works_without_agents_or_external_document_state(tmp_path: Path) -> None:
    core = AppCore(
        SettingsStore(home=tmp_path / "core-only", project_dir=tmp_path, env={}),
        extension_manager=ExtensionManager(entry_points=()),
    )
    try:
        console = Console(core)
        await commands.dispatch(console, "/status")
        assert "agent    unavailable" in console.text
        assert "document" not in console.text.lower()
        assert "indexed" not in console.text.lower()
        assert "failed:" not in console.text
    finally:
        await core.ashutdown()
