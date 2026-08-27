"""compare_models, query_spatial and check_model_health end to end.

The three tools answer the questions a coordinator asks first: what changed
since last week, what is inside this room, and is this file any good.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


def _body_context(ifc):
    return next(
        c
        for c in ifc.by_type("IfcGeometricRepresentationSubContext")
        if c.ContextIdentifier == "Body"
    )


def _translation(x: float, y: float, z: float):
    import numpy as np

    matrix = np.eye(4)
    matrix[0][3] = x
    matrix[1][3] = y
    matrix[2][3] = z
    return matrix


def _add_wall(ifc, name: str, *, length: float, thickness: float, height: float, at, storey=None):
    import ifcopenshell.api.geometry
    import ifcopenshell.api.root
    import ifcopenshell.api.spatial

    wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name=name)
    if storey is not None:
        ifcopenshell.api.spatial.assign_container(
            ifc, products=[wall], relating_structure=storey
        )
    representation = ifcopenshell.api.geometry.add_wall_representation(
        ifc, context=_body_context(ifc), length=length, height=height, thickness=thickness
    )
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=wall, representation=representation
    )
    ifcopenshell.api.geometry.edit_object_placement(
        ifc, product=wall, matrix=_translation(*at)
    )
    return wall


@pytest.fixture
def revision(tmp_path: Path, minimal_ifc4_path: Path) -> tuple[Path, Path]:
    """Two revisions of one project: a wall moved, a property edited, one wall
    deleted and one added."""
    import ifcopenshell
    import ifcopenshell.api.geometry
    import ifcopenshell.api.pset
    import ifcopenshell.api.root
    import ifcopenshell.util.element as element_util

    room = tmp_path / "project"
    room.mkdir()
    before = room / "tower-r1.ifc"
    after = room / "tower-r2.ifc"
    shutil.copy2(minimal_ifc4_path, before)
    shutil.copy2(minimal_ifc4_path, after)

    ifc = ifcopenshell.open(str(after))
    walls = {wall.Name: wall for wall in ifc.by_type("IfcWall")}
    ifcopenshell.api.geometry.edit_object_placement(
        ifc, product=walls["Wall-1"], matrix=_translation(0.0, 0.5, 0.0)
    )
    pset = element_util.get_pset(walls["Wall-2"], "Pset_WallCommon", should_inherit=False)
    ifcopenshell.api.pset.edit_pset(
        ifc, pset=ifc.by_id(pset["id"]), properties={"IsExternal": True}
    )
    ifcopenshell.api.root.remove_product(ifc, product=walls["Wall-3"])
    ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="Wall-4")
    ifc.write(str(after))
    return before, after


@pytest.fixture
def regenerated_ids(tmp_path: Path, minimal_ifc4_path: Path) -> tuple[Path, Path]:
    """The same model exported by a tool that does not preserve GlobalIds."""
    import ifcopenshell
    import ifcopenshell.guid

    room = tmp_path / "roundtrip"
    room.mkdir()
    before = room / "source.ifc"
    after = room / "reexported.ifc"
    shutil.copy2(minimal_ifc4_path, before)
    shutil.copy2(minimal_ifc4_path, after)

    ifc = ifcopenshell.open(str(after))
    for entity in ifc.by_type("IfcRoot"):
        entity.GlobalId = ifcopenshell.guid.new()
    ifc.write(str(after))
    return before, after


@pytest.fixture
def nested_model(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    """A 10 x 10 x 4 box with one element wholly inside it and one that pokes
    out through a face, well clear of the fixture's own walls."""
    import ifcopenshell

    dest = tmp_path / "nested.ifc"
    shutil.copy2(minimal_ifc4_path, dest)
    ifc = ifcopenshell.open(str(dest))
    storey = ifc.by_type("IfcBuildingStorey")[0]
    _add_wall(ifc, "Box", length=10.0, thickness=10.0, height=4.0, at=(20.0, 0.0, 0.0), storey=storey)
    _add_wall(ifc, "Inner", length=1.0, thickness=1.0, height=1.0, at=(24.0, 4.0, 1.0), storey=storey)
    _add_wall(ifc, "Crosser", length=2.0, thickness=0.3, height=0.3, at=(29.0, 5.0, 2.0), storey=storey)
    ifc.write(str(dest))
    return dest


@pytest.fixture
def unhealthy_model(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    """One file carrying a duplicate GlobalId, an uncontained element, and a
    wall five kilometres from everything else."""
    import ifcopenshell

    dest = tmp_path / "unhealthy.ifc"
    shutil.copy2(minimal_ifc4_path, dest)
    ifc = ifcopenshell.open(str(dest))
    storey = ifc.by_type("IfcBuildingStorey")[0]
    _add_wall(ifc, "Orphan", length=1.0, thickness=0.2, height=1.0, at=(1.0, 1.0, 0.0))
    _add_wall(
        ifc, "Far away", length=1.0, thickness=0.2, height=1.0, at=(5000.0, 0.0, 0.0), storey=storey
    )
    walls = {wall.Name: wall for wall in ifc.by_type("IfcWall")}
    walls["Wall-2"].GlobalId = walls["Wall-1"].GlobalId
    ifc.write(str(dest))
    return dest


# -- compare_models ---------------------------------------------------------


async def test_tools_are_registered_as_reads(ask_harness) -> None:
    names = set(await ask_harness.list_tools())
    assert {"compare_models", "query_spatial", "check_model_health"}.issubset(names)


async def test_diff_reports_added_removed_moved_and_edited(
    harness_factory, revision: tuple[Path, Path]
) -> None:
    before, after = revision
    h = await harness_factory(model=before)
    attached = await h.call("attach", path=str(after))
    other = attached["data"]["model_id"]

    out = await h.call("compare_models", other_model=other)

    assert out["ok"] is True
    data = out["data"]
    assert data["matcher"] == "global_id"
    assert data["models"] == {"before": h.core.session.model_id, "after": other}
    assert data["counts"]["added"] == 1
    assert data["counts"]["removed"] == 1
    assert data["counts"]["moved"] == 1
    assert data["counts"]["property_changed"] == 1
    by_id = {change["global_id"]: change for change in data["changes"]}
    moved = next(c for c in by_id.values() if "moved" in c["change"])
    assert moved["name"] == "Wall-1"
    assert moved["moved"]["by"] == pytest.approx(0.5, abs=1e-6)
    edited = next(c for c in by_id.values() if "property_changed" in c["change"])
    assert edited["name"] == "Wall-2"
    assert edited["property_changed"]["changed"][0]["name"] == "Pset_WallCommon.IsExternal"


async def test_change_ids_are_shaped_for_apply_color_theme(
    harness_factory, revision: tuple[Path, Path]
) -> None:
    """The coordination loop pipes these into the viewer, so each bucket must be
    a flat list of unique GlobalId strings."""
    before, after = revision
    h = await harness_factory(model=before)
    other = (await h.call("attach", path=str(after)))["data"]["model_id"]

    buckets = (await h.call("compare_models", other_model=other))["data"]["global_ids"]

    assert set(buckets) >= {"added", "removed", "moved", "property_changed"}
    for ids in buckets.values():
        assert all(isinstance(gid, str) and len(gid) == 22 for gid in ids)
        assert len(set(ids)) == len(ids)


async def test_move_tolerance_can_ignore_a_small_shift(
    harness_factory, revision: tuple[Path, Path]
) -> None:
    before, after = revision
    h = await harness_factory(model=before)
    other = (await h.call("attach", path=str(after)))["data"]["model_id"]

    out = await h.call("compare_models", other_model=other, move_tolerance=1.0)

    assert out["data"]["counts"]["moved"] == 0


async def test_regenerated_ids_fall_back_to_signature_matching(
    harness_factory, regenerated_ids: tuple[Path, Path]
) -> None:
    """Without this an export that renumbers GUIDs reads as a whole new model."""
    before, after = regenerated_ids
    h = await harness_factory(model=before)
    other = (await h.call("attach", path=str(after)))["data"]["model_id"]

    data = (await h.call("compare_models", other_model=other))["data"]

    assert data["matcher"] == "signature"
    assert data["note"]
    assert data["counts"]["added"] == 0
    assert data["counts"]["removed"] == 0
    assert data["totals"]["unchanged"] == data["matched_pairs"]


async def test_diff_against_the_same_model_is_a_clear_error(
    harness_factory, work_model: Path
) -> None:
    h = await harness_factory(model=work_model)
    out = await h.call("compare_models", other_model=h.core.session.model_id)
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "attach" in out["error"]["hint"]


async def test_diff_leaves_both_models_clean(
    harness_factory, revision: tuple[Path, Path]
) -> None:
    before, after = revision
    h = await harness_factory(model=before)
    other = (await h.call("attach", path=str(after)))["data"]["model_id"]

    out = await h.call("compare_models", other_model=other)

    assert out["ok"] is True
    assert out["meta"]["dirty"] is False


# -- query_spatial ----------------------------------------------------------


async def test_inside_separates_wholly_contained_from_poking_out(
    harness_factory, nested_model: Path
) -> None:
    import ifcopenshell

    ifc = ifcopenshell.open(str(nested_model))
    box = next(w for w in ifc.by_type("IfcWall") if w.Name == "Box")

    h = await harness_factory(model=nested_model)
    out = await h.call(
        "query_spatial", relation="inside", target_global_id=box.GlobalId, selector="IfcWall"
    )

    assert out["ok"] is True
    data = out["data"]
    assert data["method"] == "point_in_solid_sampled"
    assert data["target"]["watertight"] is True
    assert data["confidence"] == "high"
    status = {r["name"]: r["status"] for r in data["results"]}
    assert status["Inner"] == "fully_inside"
    assert status["Crosser"] == "partially_inside"


async def test_crosses_marks_what_only_passes_through(
    harness_factory, nested_model: Path
) -> None:
    import ifcopenshell

    ifc = ifcopenshell.open(str(nested_model))
    box = next(w for w in ifc.by_type("IfcWall") if w.Name == "Box")

    h = await harness_factory(model=nested_model)
    data = (
        await h.call(
            "query_spatial", relation="crosses", target_global_id=box.GlobalId, selector="IfcWall"
        )
    )["data"]

    by_name = {r["name"]: r for r in data["results"]}
    assert by_name["Crosser"]["enclosed"] is False
    assert by_name["Crosser"]["shared_volume"] > 0
    assert by_name["Inner"]["enclosed"] is True


async def test_above_finds_the_wall_directly_over_another(
    harness_factory, work_model: Path
) -> None:
    import ifcopenshell

    ifc = ifcopenshell.open(str(work_model))
    ground = next(w for w in ifc.by_type("IfcWall") if w.Name == "Wall-1")

    h = await harness_factory(model=work_model)
    data = (
        await h.call(
            "query_spatial", relation="above", target_global_id=ground.GlobalId, selector="IfcWall"
        )
    )["data"]

    assert [r["name"] for r in data["results"]] == ["Wall-3"]
    assert data["results"][0]["gap"] == pytest.approx(0.0, abs=1e-6)
    assert data["results"][0]["plan_overlap_area"] > 0


async def test_within_box_reports_containment_against_explicit_bounds(
    harness_factory, nested_model: Path
) -> None:
    h = await harness_factory(model=nested_model)
    data = (
        await h.call(
            "query_spatial",
            relation="within_box",
            box=[20.0, 0.0, 0.0, 30.0, 10.0, 4.0],
            selector="IfcWall",
        )
    )["data"]

    status = {r["name"]: r["status"] for r in data["results"]}
    assert status["Inner"] == "fully_inside"
    assert status["Crosser"] == "partially_inside"
    assert "Wall-1" not in status


async def test_within_distance_measures_real_surface_distance(
    harness_factory, work_model: Path
) -> None:
    import ifcopenshell

    ifc = ifcopenshell.open(str(work_model))
    ground = next(w for w in ifc.by_type("IfcWall") if w.Name == "Wall-1")

    h = await harness_factory(model=work_model)
    data = (
        await h.call(
            "query_spatial",
            relation="within_distance",
            target_global_id=ground.GlobalId,
            selector="IfcWall",
            distance=0.5,
        )
    )["data"]

    assert data["method"] == "closest_point_surface_distance"
    assert data["approximate"] is False
    assert {r["name"] for r in data["results"]} == {"Wall-2", "Wall-3"}


async def test_relation_without_a_target_is_a_clear_error(
    harness_factory, work_model: Path
) -> None:
    h = await harness_factory(model=work_model)
    out = await h.call("query_spatial", relation="inside", selector="IfcWall")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "target_global_id" in out["error"]["message"]


async def test_within_box_without_bounds_names_both_ways_to_give_them(
    harness_factory, work_model: Path
) -> None:
    h = await harness_factory(model=work_model)
    out = await h.call("query_spatial", relation="within_box", selector="IfcWall")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "target_global_id" in out["error"]["hint"]


async def test_a_box_on_a_solid_relation_is_refused_rather_than_ignored(
    harness_factory, work_model: Path
) -> None:
    h = await harness_factory(model=work_model)
    out = await h.call(
        "query_spatial", relation="inside", box=[0.0, 0.0, 0.0, 1.0, 1.0, 1.0], selector="IfcWall"
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "within_box" in out["error"]["message"]


# -- check_model_health -----------------------------------------------------


async def test_clean_model_reports_healthy(harness_factory, work_model: Path) -> None:
    h = await harness_factory(model=work_model)
    out = await h.call("check_model_health")

    assert out["ok"] is True
    data = out["data"]
    assert data["healthy"] is True
    assert data["findings"] == []
    assert set(data["checks"]) == set(
        (
            "duplicate_global_ids",
            "orphaned_elements",
            "degenerate_solids",
            "placement_outliers",
            "duplicate_placements",
            "model_extent",
            "empty_storeys",
            "unused_types",
        )
    )


async def test_finds_the_problems_the_schema_validator_misses(
    harness_factory, unhealthy_model: Path
) -> None:
    h = await harness_factory(model=unhealthy_model)
    schema = await h.call("validate_model")
    data = (await h.call("check_model_health"))["data"]

    # the schema validator sees the reused id and nothing else in this file
    assert schema["ok"] is True
    assert schema["data"]["issue_count"] == 1

    assert data["healthy"] is False
    found = {finding["check"]: finding for finding in data["findings"]}
    assert found["duplicate_global_ids"]["severity"] == "error"
    assert found["duplicate_global_ids"]["count"] == 1
    assert "Orphan" in [e["name"] for e in found["orphaned_elements"]["examples"]]
    assert "Far away" in [e["name"] for e in found["placement_outliers"]["examples"]]
    assert found["placement_outliers"]["global_ids"]


async def test_checks_can_be_restricted_to_the_cheap_ones(
    harness_factory, unhealthy_model: Path
) -> None:
    h = await harness_factory(model=unhealthy_model)
    data = (await h.call("check_model_health", checks=["duplicate_global_ids"]))["data"]

    assert set(data["checks"]) == {"duplicate_global_ids"}
    assert [f["check"] for f in data["findings"]] == ["duplicate_global_ids"]


async def test_unknown_check_lists_the_allowed_ones(
    harness_factory, work_model: Path
) -> None:
    h = await harness_factory(model=work_model)
    out = await h.call("check_model_health", checks=["nonsense"])
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "duplicate_global_ids" in out["error"]["hint"]


async def test_geometry_checks_are_skipped_with_a_stated_reason(
    harness_factory, unhealthy_model: Path
) -> None:
    """Silently sampling a big model would be worse than saying nothing."""
    h = await harness_factory(model=unhealthy_model)
    data = (await h.call("check_model_health", max_elements=1))["data"]

    skipped = {name for name, s in data["checks"].items() if s["status"] == "skipped"}
    assert skipped == {
        "degenerate_solids",
        "placement_outliers",
        "duplicate_placements",
        "model_extent",
    }
    assert "max_elements" in data["checks"]["placement_outliers"]["reason"]
    assert data["checks"]["duplicate_global_ids"]["status"] == "findings"
