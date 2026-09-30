"""Commands folded under a parent run both ways; the menu and /help show only the parents."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.tui import commands, completion
from tests.tui.test_commands import FakeConsole

pytestmark = pytest.mark.asyncio


@pytest.fixture
def console(core) -> FakeConsole:
    core.start_audit()
    return FakeConsole(core)


async def test_help_lists_the_parents_and_says_where_the_old_names_went(console) -> None:
    await commands.dispatch(console, "/help")

    listed = [line for line in console.lines if line.strip().startswith("[cyan]/")]
    text = console.text
    assert not any("/attach <path>" in line for line in listed)
    assert "/models [attach|detach|use|info]" in text
    assert "The old names still work" in text
    assert len(listed) <= 20


async def test_a_folded_command_still_has_its_own_help_page(console) -> None:
    await commands.dispatch(console, "/help attach")

    assert "/attach <path>" in console.text


async def test_models_attach_runs_the_attach_command(console, core, work_model: Path, tmp_path) -> None:
    await core.open_model(work_model)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    core.add_allowed_dir(tmp_path)

    await commands.dispatch(console, f'/models attach "{annex}"')
    await commands.dispatch(console, "/models")

    assert "annex" in console.text
    assert len(core.models.sessions) == 2


async def test_models_list_is_the_same_as_models(console, core, work_model: Path) -> None:
    await core.open_model(work_model)

    await commands.dispatch(console, "/models list")

    assert "work" in console.text and "active" in console.text


async def test_settings_theme_reaches_the_theme_command(console) -> None:
    await commands.dispatch(console, "/settings theme dark")

    assert "theme set to dark" in console.text


async def test_connect_copy_reaches_the_copy_command(console) -> None:
    await commands.dispatch(console, "/connect copy url")

    assert console.clipboard == console.core.mcp_url


async def test_status_audit_reaches_the_audit_command(console) -> None:
    console.core.audit.record("test_event")

    await commands.dispatch(console, "/status audit")

    assert "test_event" in console.text


async def test_the_old_names_still_run(console, core, work_model: Path) -> None:
    await core.open_model(work_model)

    await commands.dispatch(console, "/info")

    assert "IfcProduct" in console.text


async def test_the_menu_offers_the_parents_only(core) -> None:
    names = {c.insert for c in completion.complete("/", core).candidates}

    assert "/models" in names and "/settings" in names
    assert not names & {f"/{n}" for n in commands.HIDDEN}


async def test_the_menu_completes_a_folded_command_under_its_parent(core) -> None:
    state = completion.complete("/settings theme ", core)

    assert state.prefix == "/settings theme "
    assert {c.insert for c in state.candidates} >= {"light", "dark"}


async def test_models_offers_its_subcommands(core) -> None:
    state = completion.complete("/models ", core)

    assert {c.insert for c in state.candidates} == {"attach", "detach", "use", "info"}
