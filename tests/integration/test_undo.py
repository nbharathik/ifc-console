"""Every edit is one undoable step; a failed edit leaves nothing behind."""

from __future__ import annotations

import pytest

from ifc_console.core.results import ToolError
from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio

_ADD_WALL = "ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall', name='Extra')"


def _rename(gid: str, name: str = "Renamed") -> str:
    return f"ifc.by_guid({gid!r}).Name = {name!r}"


async def _first_wall(h) -> tuple[str, str]:
    listing = await h.call("query_elements", query="IfcWall", limit=1)
    gid = listing["data"]["rows"][0]["global_id"]
    return gid, h.core.session.ifc.by_guid(gid).Name


def _events(core) -> list[dict]:
    seen: list[dict] = []
    core.events.subscribe(seen.append)
    return seen


async def test_an_edit_reports_what_it_changed(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _first_wall(h)

    out = await h.call("execute_ifc_code", code=_rename(gid), description="rename")

    change = out["data"]["change"]
    assert change["edited"] == 1
    assert change["geometry"] is False
    assert change["guids"] == [gid]
    assert change["undoable"] is True


async def test_an_edit_can_be_undone_and_redone(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, original = await _first_wall(h)
    session = h.core.session
    await h.call("execute_ifc_code", code=_rename(gid), description="rename")
    assert session.dirty and session.change_count == 1

    undone = await h.core.undo(by="test")

    assert undone.description == "rename"
    assert session.ifc.by_guid(gid).Name == original
    assert session.dirty is False
    assert session.change_count == 0
    assert session.history.can_redo

    redone = await h.core.redo(by="test")

    assert redone.change_id == undone.change_id
    assert session.ifc.by_guid(gid).Name == "Renamed"
    assert session.dirty is True
    assert session.change_count == 1


async def test_undo_walks_back_through_several_edits(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, original = await _first_wall(h)
    session = h.core.session
    walls = len(session.ifc.by_type("IfcWall"))
    await h.call("execute_ifc_code", code=_rename(gid, "One"), description="one")
    await h.call("execute_ifc_code", code=_ADD_WALL, description="two")

    await h.core.undo(by="test")
    assert len(session.ifc.by_type("IfcWall")) == walls
    assert session.ifc.by_guid(gid).Name == "One"

    await h.core.undo(by="test")
    assert session.ifc.by_guid(gid).Name == original
    assert session.dirty is False


async def test_a_new_edit_after_an_undo_drops_the_redo_stack(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _first_wall(h)
    await h.call("execute_ifc_code", code=_rename(gid, "One"), description="one")
    await h.core.undo(by="test")

    await h.call("execute_ifc_code", code=_rename(gid, "Two"), description="two")

    with pytest.raises(ToolError) as err:
        await h.core.redo(by="test")
    assert err.value.code == "NOTHING_TO_REDO"


async def test_undo_with_nothing_to_undo_says_so(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)

    with pytest.raises(ToolError) as err:
        await h.core.undo(by="test")

    assert err.value.code == "NOTHING_TO_UNDO"


async def test_undoing_after_a_save_makes_the_model_unsaved_again(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    gid, _ = await _first_wall(h)
    session = h.core.session
    await h.call("execute_ifc_code", code=_rename(gid), description="rename")
    assert (await h.call("save_ifc_file"))["ok"] is True
    assert session.dirty is False

    await h.core.undo(by="test")

    assert session.dirty is True
    assert session.change_count == 1
    await h.core.redo(by="test")
    assert session.dirty is False


async def test_a_failed_run_leaves_the_model_untouched(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, original = await _first_wall(h)
    session = h.core.session
    revision_events = _events(h.core)

    out = await h.call(
        "execute_ifc_code",
        code=f"{_rename(gid)}\n{_ADD_WALL}\nraise ValueError('boom')",
        description="fails halfway",
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "EXEC_ERROR"
    assert out["data"]["rolled_back"] is True
    assert out["data"]["verified"] is True
    assert session.ifc.by_guid(gid).Name == original
    assert not [w for w in session.ifc.by_type("IfcWall") if w.Name == "Extra"]
    assert session.dirty is False
    assert session.change_count == 0
    assert not [e for e in revision_events if e["type"] == "model_mutated"]


async def test_a_failed_run_keeps_the_edits_that_came_before(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _first_wall(h)
    session = h.core.session
    await h.call("execute_ifc_code", code=_rename(gid, "Kept"), description="kept")

    out = await h.call(
        "execute_ifc_code", code=f"{_rename(gid, 'Lost')}\nraise ValueError('boom')"
    )

    assert out["ok"] is False
    assert session.ifc.by_guid(gid).Name == "Kept"
    assert session.dirty is True
    assert session.change_count == 1


async def test_a_run_that_changes_nothing_does_not_dirty_the_model(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    session = h.core.session

    out = await h.call("execute_ifc_code", code=f"if False:\n    {_ADD_WALL}")

    assert out["ok"] is True
    assert "change" not in out["data"]
    assert session.dirty is False
    assert session.change_count == 0


async def test_generated_code_cannot_undo_for_itself(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, original = await _first_wall(h)
    session = h.core.session
    await h.call("execute_ifc_code", code=_rename(gid), description="rename")

    out = await h.call("execute_ifc_code", code="ifc.undo()\n" + _ADD_WALL)

    assert out["ok"] is False
    assert out["error"]["code"] == "EXEC_BLOCKED"
    assert "/undo" in out["error"]["message"]
    assert session.ifc.by_guid(gid).Name == "Renamed"
    assert original != "Renamed"


async def test_the_mutation_event_says_what_the_viewer_must_redraw(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _first_wall(h)
    seen = _events(h.core)

    out = await h.call("execute_ifc_code", code=_rename(gid), description="rename")

    mutated = [e for e in seen if e["type"] == "model_mutated"]
    assert len(mutated) == 1
    event = mutated[0]
    assert event["change_id"] == out["data"]["change"]["id"]
    assert event["guids"] == [gid]
    assert event["geometry"] is False
    assert event["labels"] is True


async def test_undo_and_redo_are_announced(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _first_wall(h)
    await h.call("execute_ifc_code", code=_rename(gid), description="rename")
    seen = _events(h.core)

    await h.core.undo(by="test")
    await h.core.redo(by="test")

    kinds = [e["type"] for e in seen if e["type"] in ("model_undone", "model_redone")]
    assert kinds == ["model_undone", "model_redone"]
    assert [e for e in seen if e["type"] == "model_undone"][0]["guids"] == [gid]


async def test_undo_moves_the_revision_so_caches_and_tabs_refresh(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _first_wall(h)
    await h.call("execute_ifc_code", code=_rename(gid), description="rename")
    session = h.core.session
    before = session.revision

    await h.core.undo(by="test")

    assert session.revision > before
