"""The measurement toolkit end to end through the MCP surface."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def room_model(tmp_path: Path) -> Path:
    """A metre file with one IfcSpace: body geometry, no stored quantities."""
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.project
    import ifcopenshell.api.root
    import ifcopenshell.api.unit

    ifc = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcProject", name="Rooms")
    metre = ifcopenshell.api.unit.add_si_unit(ifc, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(ifc, units=[metre])
    model = ifcopenshell.api.context.add_context(ifc, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        ifc,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model,
    )
    space = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcSpace", name="Office 101")
    profile = ifc.createIfcRectangleProfileDef("AREA", "Room", None, 4.0, 3.0)
    solid = ifc.createIfcExtrudedAreaSolid(
        profile, None, ifc.createIfcDirection((0.0, 0.0, 1.0)), 2.7
    )
    representation = ifc.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=space, representation=representation
    )
    ifcopenshell.api.geometry.edit_object_placement(ifc, product=space)
    path = tmp_path / "rooms.ifc"
    ifc.write(str(path))
    return path


async def test_geometry_probe_measures_the_walls(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("get_element_geometry", selector="IfcWall")
    assert out["ok"] is True
    data = out["data"]
    assert data["returned"] == 3
    thicknesses = [r["local_extents"]["y"] for r in data["elements"]]
    assert all(abs(t - 0.2) < 0.01 for t in thicknesses)
    assert data["units"]["length_unit"] == "MILLIMETRE"


async def test_measure_elements_reports_both_units(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call(
        "measure_elements",
        selector="IfcWall",
        method="geometry_extent",
        metric="thickness",
    )
    assert out["ok"] is True
    record = out["data"]["elements"][0]
    assert abs(record["value"] - 200.0) < 5.0
    assert abs(record["value_si"] - 0.2) < 0.005
    assert record["method"] == "geometry_extent"
    assert record["metric"] == "thickness"


async def test_measure_distance_between_two_walls(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call(
        "measure_distance",
        set_a="IfcWall, Name=Wall-1",
        set_b="IfcWall, Name=Wall-3",
    )
    assert out["ok"] is True
    assert out["data"]["aabb_gap"]["si"] == pytest.approx(0.0, abs=1e-6)


async def test_derived_quantities_through_the_tool(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("compute_quantities", selector="IfcWall", source="derived")
    assert out["ok"] is True
    data = out["data"]
    assert data["source"] == "stored+derived"
    assert data["derived_elements"] == 3
    assert data["totals"]["Width"] == pytest.approx(600.0, rel=0.02)
    # walls are 5x0.2x3, 4x0.2x3 and 5x0.2x3 metres, totals in mm^2
    assert data["totals"]["GrossSideArea"] == pytest.approx(42e6, rel=0.02)
    assert data["totals"]["GrossTopArea"] == pytest.approx(2.8e6, rel=0.02)


async def test_spaces_are_measurable_in_the_derived_fallback(harness_factory, room_model: Path):
    h = await harness_factory(model=room_model)
    out = await h.call("compute_quantities", selector="IfcSpace", source="derived")
    assert out["ok"] is True
    data = out["data"]
    assert data["derived_elements"] == 1
    assert data["derived_without_geometry"] == 0
    totals = data["totals"]
    assert totals["GrossFloorArea"] == pytest.approx(12.0, rel=0.02)
    assert totals["GrossVolume"] == pytest.approx(32.4, rel=0.02)
    assert totals["Height"] == pytest.approx(2.7, rel=0.02)


async def test_one_of_selector_or_ids_is_enforced(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("get_element_geometry")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "global_ids" in out["error"]["hint"]
