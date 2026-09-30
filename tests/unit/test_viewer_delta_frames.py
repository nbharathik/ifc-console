"""What the hub tells a tab about an edit, so it can follow without a rebuild."""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.test_viewer_hub import _attach

pytestmark = pytest.mark.asyncio


async def _edit(core, action, *, description="edit"):
    """Run `action(ifc)` as one kept step and announce it like the tools do."""
    session = core.session
    _, outcome = await session.run(
        lambda: session.mutate(
            lambda: action(session.ifc), tool="test", description=description
        )
    )
    record = outcome.record
    core.events.emit(
        "model_mutated",
        tool="test",
        description=description,
        changes=session.change_count,
        **record.event_fields(),
    )
    await asyncio.sleep(0)
    return record


def _rename(name):
    def action(ifc):
        wall = ifc.by_type("IfcWall")[0]
        wall.Name = name
        return wall.GlobalId

    return action


async def test_a_rename_reaches_the_tab_as_an_in_place_update(core, work_model) -> None:
    await core.open_model(work_model)
    ws = _attach(core.viewer_hub)
    before = core.session.etag

    record = await _edit(core, _rename("Renamed"), description="rename")

    frame = ws.frames("model_updated")[-1]
    assert frame["base_etag"] == before
    assert frame["etag"] == core.session.etag != before
    assert frame["geometry"] is False
    assert frame["labels"] is True
    assert frame["names"] == record.summary.names
    assert list(frame["names"].values()) == ["Renamed"]
    assert frame["elements"] == record.summary.guids
    assert frame["change_id"] == record.change_id
    assert "tree" not in frame


async def test_undo_relabels_with_the_names_from_before(core, work_model) -> None:
    await core.open_model(work_model)
    ws = _attach(core.viewer_hub)
    ifc = core.session.ifc
    original = ifc.by_type("IfcWall")[0].Name
    await _edit(core, _rename("Renamed"))

    await core.undo(by="test")
    await asyncio.sleep(0)

    frame = ws.frames("model_updated")[-1]
    assert frame["reason"] == "undone"
    assert list(frame["names"].values()) == [original]
    assert frame["geometry"] is False
    assert frame["base_etag"] != frame["etag"]


async def test_a_geometry_edit_is_not_marked_as_in_place(core, work_model) -> None:
    await core.open_model(work_model)
    ws = _attach(core.viewer_hub)

    await _edit(
        core,
        lambda ifc: ifc.by_type("IfcWall")[0].ObjectPlacement
        and setattr(ifc.by_type("IfcWall")[0], "ObjectPlacement", None),
    )

    frame = ws.frames("model_updated")[-1]
    assert frame["geometry"] is True


async def test_a_save_keeps_the_etag_so_the_tab_does_nothing(core, work_model) -> None:
    await core.open_model(work_model)
    ws = _attach(core.viewer_hub)
    await _edit(core, _rename("Renamed"))
    edited = core.session.etag
    core.policy.allow_ai_save = True

    await core.save_model(by="test")
    await asyncio.sleep(0)

    frame = ws.frames("model_updated")[-1]
    assert frame["reason"] == "saved"
    assert frame["etag"] == edited
    assert frame["changes"] == 0


async def test_status_says_whether_undo_and_redo_are_available(core, work_model) -> None:
    await core.open_model(work_model)
    hub = core.viewer_hub
    assert hub.status_payload()["can_undo"] is False

    await _edit(core, _rename("Renamed"), description="rename the wall")
    status = hub.status_payload()

    assert status["can_undo"] is True
    assert status["undo_label"] == "rename the wall"
    assert status["can_redo"] is False

    await core.undo(by="test")
    status = hub.status_payload()

    assert status["can_undo"] is False
    assert status["can_redo"] is True
    assert status["redo_label"] == "rename the wall"


async def test_a_change_touching_too_many_elements_sends_no_element_list(
    core, work_model
) -> None:
    await core.open_model(work_model)
    ws = _attach(core.viewer_hub)
    core.events.emit(
        "model_mutated", tool="test", guids=[f"g{i}" for i in range(600)], geometry=False
    )
    await asyncio.sleep(0)

    frame = ws.frames("model_updated")[-1]
    assert "elements" not in frame
