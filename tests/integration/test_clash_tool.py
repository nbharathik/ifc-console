"""detect_clashes end to end, including the cross-model coordination path."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def clashing_model(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    """A copy whose first wall is duplicated in place, so one clash exists."""
    import ifcopenshell
    import ifcopenshell.util.element as element_util

    dest = tmp_path / "clashing.ifc"
    shutil.copy2(minimal_ifc4_path, dest)
    ifc = ifcopenshell.open(str(dest))
    copy = element_util.copy(ifc, ifc.by_type("IfcWall")[0])
    copy.GlobalId = ifcopenshell.guid.new()
    copy.Name = "Wall-1-duplicate"
    ifc.write(str(dest))
    return dest


async def test_finds_the_duplicated_wall(harness_factory, clashing_model: Path):
    h = await harness_factory(model=clashing_model)
    out = await h.call("detect_clashes", set_a="IfcWall", mode="overlap")
    assert out["ok"] is True
    data = out["data"]
    assert data["total"] == 1
    clash = data["clashes"][0]
    assert clash["type"] == "overlap"
    assert clash["volume"] == pytest.approx(3.0, rel=0.05)
    assert len(data["global_ids"]) == 2


async def test_global_ids_are_shaped_for_highlight_elements(
    harness_factory, clashing_model: Path
):
    """The coordination loop hands these straight to the viewer, so they must be
    a flat list of unique GlobalId strings."""
    h = await harness_factory(model=clashing_model)
    found = await h.call("detect_clashes", set_a="IfcWall")
    ids = found["data"]["global_ids"]
    assert all(isinstance(gid, str) and len(gid) == 22 for gid in ids)
    assert len(set(ids)) == len(ids)


async def test_clean_model_reports_no_clashes(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("detect_clashes", set_a="IfcWall", mode="overlap")
    assert out["ok"] is True
    assert out["data"]["total"] == 0
    assert out["data"]["global_ids"] == []


async def test_clearance_mode_reports_gaps(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("detect_clashes", set_a="IfcWall", mode="clearance", tolerance=0.5)
    assert out["ok"] is True
    assert out["data"]["total"] > 0
    assert all(c["type"] == "clearance" for c in out["data"]["clashes"])


async def test_clash_across_two_attached_models(
    harness_factory, tmp_path: Path, minimal_ifc4_path: Path
):
    """Federated coordination: architecture against structure, two files."""
    root = tmp_path / "project"
    root.mkdir()
    architecture = root / "tower-architecture.ifc"
    structure = root / "tower-structure.ifc"
    shutil.copy2(minimal_ifc4_path, architecture)
    shutil.copy2(minimal_ifc4_path, structure)

    h = await harness_factory(model=architecture)
    attached = await h.call("attach", path=str(structure))
    other = attached["data"]["model_id"]

    out = await h.call(
        "detect_clashes", set_a="IfcWall", set_b="IfcWall", other_model=other, mode="overlap"
    )
    assert out["ok"] is True
    data = out["data"]
    assert data["models"]["set_b"] == other
    assert data["models"]["set_a"] != other
    # the two files are identical copies, so every wall overlaps its twin
    assert data["total"] >= 3


async def test_works_in_ask_mode_and_changes_nothing(harness_factory, clashing_model: Path):
    h = await harness_factory(model=clashing_model)
    out = await h.call("detect_clashes", set_a="IfcWall")
    assert out["ok"] is True
    assert out["meta"]["dirty"] is False


async def test_unknown_model_is_a_clear_error(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("detect_clashes", set_a="IfcWall", other_model="nope")
    assert out["ok"] is False
    assert out["error"]["hint"]


async def test_empty_selector_is_a_clear_error(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    out = await h.call("detect_clashes", set_a="IfcPile")
    assert out["ok"] is False
    assert out["error"]["code"] == "NO_MATCH"
    assert "query_elements" in out["error"]["hint"]
