"""The completion menu, driven end to end through the real Textual app."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ifc_console.policy.modes import Mode
from ifc_console.tui import commands
from ifc_console.tui.app import IfcConsoleApp
from ifc_console.tui.console import CommandInput
from ifc_console.tui.modals import ConfirmModal

pytestmark = pytest.mark.asyncio


async def _until(pilot, predicate: Callable[[], bool], tries: int = 60) -> None:
    for _ in range(tries):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition not met in time")


@pytest.fixture
def app(core) -> IfcConsoleApp:
    return IfcConsoleApp(core, autostart=False)


async def test_typing_opens_and_narrows_the_menu(app: IfcConsoleApp) -> None:
    async with app.run_test() as pilot:
        console = app.screen
        menu = console.query_one("#completions")
        assert menu.display is False
        await pilot.press("/")
        assert menu.display is True
        assert menu.option_count == len(commands.REGISTRY)
        await pilot.press("m", "o")
        assert menu.option_count == 2  # /mode and /model


async def test_enter_picks_command_then_value_then_runs(app: IfcConsoleApp) -> None:
    core = app.core
    async with app.run_test() as pilot:
        console = app.screen
        prompt = console.query_one("#prompt", CommandInput)
        menu = console.query_one("#completions")
        await pilot.press("/", "m", "o", "d", "e", "enter")
        assert prompt.value == "/mode "  # picked the command, not run yet
        assert menu.option_count == 2  # its values took over the menu
        await pilot.press("down", "enter")  # highlight "edit", pick it
        await _until(pilot, lambda: isinstance(app.screen, ConfirmModal))
        await pilot.press("y")  # escalation still confirms; that modal is the point
        await _until(pilot, lambda: core.policy.mode is Mode.EDIT)
        assert prompt.value == ""
        assert prompt.history[-1] == "/mode edit"


async def test_tab_on_empty_prompt_opens_the_menu(app: IfcConsoleApp) -> None:
    async with app.run_test() as pilot:
        console = app.screen
        await pilot.press("tab")
        assert console.query_one("#prompt", CommandInput).value == "/"
        assert console.query_one("#completions").display is True


async def test_tab_completes_without_running(app: IfcConsoleApp) -> None:
    async with app.run_test() as pilot:
        console = app.screen
        prompt = console.query_one("#prompt", CommandInput)
        await pilot.press("/", "h", "e", "tab")
        assert prompt.value == "/help"  # completed, nothing dispatched
        assert not prompt.history
        await pilot.press("enter")
        await _until(pilot, lambda: bool(prompt.history))
        assert prompt.history[-1] == "/help"


async def test_escape_closes_menu_before_clearing_line(app: IfcConsoleApp) -> None:
    async with app.run_test() as pilot:
        console = app.screen
        prompt = console.query_one("#prompt", CommandInput)
        menu = console.query_one("#completions")
        await pilot.press("/", "m")
        assert menu.display is True
        await pilot.press("escape")
        assert menu.display is False
        assert prompt.value == "/m"  # first Escape only closes the menu
        await pilot.press("escape")
        assert prompt.value == ""


async def test_history_recall_keeps_menu_closed(app: IfcConsoleApp) -> None:
    async with app.run_test() as pilot:
        console = app.screen
        prompt = console.query_one("#prompt", CommandInput)
        menu = console.query_one("#completions")
        await pilot.press("/", "s", "t", "a", "t", "u", "s", "enter")
        await _until(pilot, lambda: bool(prompt.history))
        await pilot.press("up")
        assert prompt.value == "/status"
        assert menu.display is False  # browsing history must not pop the menu


async def test_mouse_click_picks_and_runs(app: IfcConsoleApp) -> None:
    async with app.run_test() as pilot:
        console = app.screen
        prompt = console.query_one("#prompt", CommandInput)
        await pilot.press("/", "m", "o", "d", "e", "enter")  # value menu open
        await pilot.click("#completions", offset=(4, 1))  # first row: ask
        await _until(pilot, lambda: bool(prompt.history))
        assert prompt.history[-1] == "/mode ask"
        assert app.core.policy.mode is Mode.ASK  # already current; no modal needed
