"""set_properties: structured, atomic, previewable, undoable."""

from __future__ import annotations

import ifcopenshell.util.element as element_util
import pytest

from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio


def _edit(gids, value="F30", *, pset="Pset_Test", prop="Rating", **extra):
    return {"global_ids": list(gids), "pset": pset, "property": prop, "value": value, **extra}


async def _walls(h, count=3) -> list[str]:
    listing = await h.call("query_elements", query="IfcWall", limit=count)
    return [row["global_id"] for row in listing["data"]["rows"]]


def _read(h, gid, pset="Pset_Test", prop="Rating"):
    element = h.core.session.ifc.by_guid(gid)
    return element_util.get_pset(element, pset, prop)


async def test_a_new_property_creates_its_set_and_reports_the_change(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, b, _ = await _walls(h)

    out = await h.call("set_properties", changes=[_edit([a, b])], description="fire rating")

    assert out["ok"] is True
    assert out["data"]["applied"] is True
    assert out["data"]["counts"]["created"] == 2
    assert out["data"]["counts"]["property_sets_created"] == 2
    assert {row["action"] for row in out["data"]["rows"]} == {"created"}
    assert _read(h, a) == "F30" and _read(h, b) == "F30"
    change = out["data"]["change"]
    assert change["geometry"] is False
    assert change["undoable"] is True
    assert h.core.session.dirty is True


async def test_an_existing_property_keeps_its_type_and_reports_before_and_after(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, *_ = await _walls(h)
    await h.call("set_properties", changes=[_edit([a], 5.0, prop="Depth", type="IfcLengthMeasure")])

    out = await h.call("set_properties", changes=[_edit([a], 7, prop="Depth")])

    row = out["data"]["rows"][0]
    assert row["action"] == "updated"
    assert row["before"] == 5.0
    assert row["after"] == 7.0
    wall = h.core.session.ifc.by_guid(a)
    pset = next(
        rel.RelatingPropertyDefinition
        for rel in wall.IsDefinedBy
        if rel.RelatingPropertyDefinition.Name == "Pset_Test"
    )
    prop = next(p for p in pset.HasProperties if p.Name == "Depth")
    assert prop.NominalValue.is_a() == "IfcLengthMeasure"


async def test_a_dry_run_reports_the_rows_and_changes_nothing(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, *_ = await _walls(h)
    session = h.core.session
    revision = session.revision

    out = await h.call("set_properties", changes=[_edit([a])], dry_run=True)

    assert out["ok"] is True
    assert out["data"]["applied"] is False
    assert out["data"]["dry_run"] is True
    assert out["data"]["verified"] is True
    assert out["data"]["rows"][0]["action"] == "created"
    assert _read(h, a) is None
    assert session.dirty is False
    assert session.revision == revision
    assert session.change_count == 0


async def test_one_bad_entry_leaves_the_model_untouched(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, *_ = await _walls(h)

    out = await h.call(
        "set_properties", changes=[_edit([a]), _edit(["0000000000000000000000"], prop="Other")]
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "PROPERTY_NOT_FOUND"
    assert out["data"]["rolled_back"] is True
    assert _read(h, a) is None
    assert h.core.session.dirty is False


async def test_an_entity_type_is_refused_as_a_value_type(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, *_ = await _walls(h)

    out = await h.call("set_properties", changes=[_edit([a], "x", type="IfcWall")])

    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert out["data"]["rolled_back"] is True


async def test_setting_the_same_value_again_is_a_no_op(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, *_ = await _walls(h)
    await h.call("set_properties", changes=[_edit([a])])
    await h.call("save_ifc_file")  # nothing to save through the assistant; keeps the count honest
    session = h.core.session
    steps = len(session.history.applied)

    out = await h.call("set_properties", changes=[_edit([a])])

    assert out["ok"] is True
    assert out["data"]["applied"] is False
    assert out["data"]["counts"]["unchanged"] == 1
    assert len(session.history.applied) == steps


async def test_the_same_target_twice_in_one_call_is_refused(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, *_ = await _walls(h)

    out = await h.call("set_properties", changes=[_edit([a]), _edit([a], "F60")])

    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert _read(h, a) is None


async def test_ask_mode_blocks_it(ask_harness) -> None:
    a, *_ = await _walls(ask_harness)

    out = await ask_harness.call("set_properties", changes=[_edit([a])])

    assert out["ok"] is False
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"


async def test_the_edit_is_one_undo_step(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, b, _ = await _walls(h)
    await h.call("set_properties", changes=[_edit([a, b])])
    session = h.core.session

    await h.core.undo(by="test")

    assert _read(h, a) is None and _read(h, b) is None
    assert session.dirty is False

    await h.core.redo(by="test")

    assert _read(h, a) == "F30" and _read(h, b) == "F30"


async def test_the_event_names_the_elements_and_no_geometry(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    a, b, _ = await _walls(h)
    seen: list[dict] = []
    h.core.events.subscribe(seen.append)

    await h.call("set_properties", changes=[_edit([a, b])])

    event = [e for e in seen if e["type"] == "model_mutated"][0]
    assert event["tool"] == "set_properties"
    assert set(event["guids"]) == {a, b}
    assert event["geometry"] is False
    assert event["labels"] is False


async def test_it_is_listed_with_the_other_tools(ask_harness) -> None:
    assert "set_properties" in await ask_harness.list_tools()
