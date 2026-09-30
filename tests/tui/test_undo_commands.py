"""/undo, /redo and /changes against a fake console."""

from __future__ import annotations

import pytest

from ifc_console.tui import commands
from tests.tui.test_commands import FakeConsole

pytestmark = pytest.mark.asyncio


@pytest.fixture
def console(core) -> FakeConsole:
    core.start_audit()
    return FakeConsole(core)


async def _rename_first_wall(core, name: str) -> str:
    session = core.session
    wall = session.ifc.by_type("IfcWall")[0]
    guid = wall.GlobalId
    await session.run(
        lambda: session.mutate(
            lambda: setattr(wall, "Name", name), tool="test", description=f"rename to {name}"
        )
    )
    return guid


async def test_undo_says_so_when_there_is_nothing_to_undo(console, core, work_model) -> None:
    await core.open_model(work_model)

    await commands.dispatch(console, "/undo")

    assert "no edit to undo" in console.text


async def test_undo_and_redo_step_through_edits(console, core, work_model) -> None:
    await core.open_model(work_model)
    guid = await _rename_first_wall(core, "First")
    events: list[dict] = []
    core.events.subscribe(events.append)

    await commands.dispatch(console, "/undo")

    assert core.session.ifc.by_guid(guid).Name != "First"
    assert [e["type"] for e in events] == ["model_undone"]

    await commands.dispatch(console, "/redo")

    assert core.session.ifc.by_guid(guid).Name == "First"
    assert [e["type"] for e in events] == ["model_undone", "model_redone"]


async def test_redo_says_so_when_nothing_was_undone(console, core, work_model) -> None:
    await core.open_model(work_model)
    await _rename_first_wall(core, "First")

    await commands.dispatch(console, "/redo")

    assert "no undone edit" in console.text


async def test_changes_lists_the_edits_and_marks_the_saved_state(
    console, core, work_model
) -> None:
    await core.open_model(work_model)
    await commands.dispatch(console, "/changes")
    assert "no edits since" in console.text

    await _rename_first_wall(core, "First")
    core.session.history.mark_saved()
    await _rename_first_wall(core, "Second")
    console.lines.clear()
    await commands.dispatch(console, "/changes")

    text = console.text
    assert "rename to First" in text and "rename to Second" in text
    assert text.index("rename to First") < text.index("file on disk") < text.index(
        "rename to Second"
    )


async def test_the_commands_are_registered_and_listed(console) -> None:
    assert {"undo", "redo", "changes"} <= set(commands.REGISTRY)

    await commands.dispatch(console, "/help")

    assert "/undo" in console.text and "/changes" in console.text
