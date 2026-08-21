"""The measurement toolkit end to end through the MCP surface."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


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


async def test_one_of_selector_or_ids_is_enforced(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("get_element_geometry")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "global_ids" in out["error"]["hint"]
