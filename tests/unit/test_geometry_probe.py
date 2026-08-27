"""The per-element geometry probe: extents, volume, footprint, distance."""

from __future__ import annotations

import numpy as np
import pytest

from ifc_console.ifc import geometry
from ifc_console.mcp.envelope import ToolError

_BOX_FACES = np.array(
    [
        [0, 2, 1], [0, 3, 2], [4, 5, 6], [4, 6, 7],
        [0, 1, 5], [0, 5, 4], [1, 2, 6], [1, 6, 5],
        [2, 3, 7], [2, 7, 6], [3, 0, 4], [3, 4, 7],
    ]
)


def box_mesh(low, high) -> tuple[np.ndarray, np.ndarray]:
    lo = np.asarray(low, dtype=float)
    hi = np.asarray(high, dtype=float)
    verts = np.array(
        [
            [lo[0], lo[1], lo[2]], [hi[0], lo[1], lo[2]],
            [hi[0], hi[1], lo[2]], [lo[0], hi[1], lo[2]],
            [lo[0], lo[1], hi[2]], [hi[0], lo[1], hi[2]],
            [hi[0], hi[1], hi[2]], [lo[0], hi[1], hi[2]],
        ]
    )
    return verts, _BOX_FACES


class TestMeshMath:
    def test_volume_of_a_unit_box(self):
        verts, faces = box_mesh([0, 0, 0], [1, 1, 1])
        assert geometry.mesh_volume(verts, faces) == pytest.approx(1.0)

    def test_volume_of_a_wall_shaped_box(self):
        verts, faces = box_mesh([0, 0, 0], [5.0, 0.2, 3.0])
        assert geometry.mesh_volume(verts, faces) == pytest.approx(3.0)

    def test_footprint_of_a_box_is_its_plan_area(self):
        verts, faces = box_mesh([0, 0, 0], [5.0, 0.2, 3.0])
        assert geometry.footprint_area(verts, faces) == pytest.approx(1.0)


class TestPointsToTriangles:
    def test_face_gap_is_exact(self):
        verts_a, faces_a = box_mesh([0, 0, 0], [1, 1, 1])
        verts_b, _ = box_mesh([1.5, 0, 0], [2.5, 1, 1])
        got = geometry.points_to_triangles_distance(verts_b, verts_a[faces_a])
        assert got == pytest.approx(0.5)

    def test_point_over_a_face_projects_onto_it(self):
        verts, faces = box_mesh([0, 0, 0], [1, 1, 1])
        point = np.array([[0.5, 0.5, 2.0]])
        assert geometry.points_to_triangles_distance(point, verts[faces]) == pytest.approx(1.0)

    def test_point_off_a_corner_measures_to_the_vertex(self):
        verts, faces = box_mesh([0, 0, 0], [1, 1, 1])
        point = np.array([[2.0, 2.0, 2.0]])
        expected = np.sqrt(3.0)
        assert geometry.points_to_triangles_distance(point, verts[faces]) == pytest.approx(expected)

    def test_touching_point_is_zero(self):
        verts, faces = box_mesh([0, 0, 0], [1, 1, 1])
        point = np.array([[1.0, 0.5, 0.5]])
        assert geometry.points_to_triangles_distance(point, verts[faces]) == pytest.approx(0.0)


class TestResolveTargets:
    def test_selector_and_ids_together_is_an_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            geometry.resolve_targets(ifc4, selector="IfcWall", global_ids=["x"])
        assert excinfo.value.code == "INVALID_INPUT"

    def test_neither_is_an_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            geometry.resolve_targets(ifc4)
        assert excinfo.value.code == "INVALID_INPUT"

    def test_unknown_global_id_is_a_clear_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            geometry.resolve_targets(ifc4, global_ids=["0000000000000000000000"])
        assert excinfo.value.code == "NOT_FOUND"

    def test_global_ids_resolve_without_physical_filter(self, ifc4):
        space = ifc4.by_type("IfcSpace")[0]
        got = geometry.resolve_targets(ifc4, global_ids=[space.GlobalId])
        assert got[0].GlobalId == space.GlobalId

    def test_selector_results_are_sorted_and_capped(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            geometry.resolve_targets(ifc4, selector="IfcWall", max_elements=1)
        assert excinfo.value.code == "TOO_MANY_ELEMENTS"

    def test_offset_skips_the_head_of_the_sorted_match(self, ifc4):
        every = geometry.resolve_targets(ifc4, selector="IfcWall")
        page = geometry.resolve_targets(ifc4, selector="IfcWall", offset=1)
        assert [e.id() for e in page] == [e.id() for e in every[1:]]

    def test_offset_past_the_match_names_the_offset(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            geometry.resolve_targets(ifc4, selector="IfcWall", offset=99)
        assert excinfo.value.code == "NO_MATCH"
        assert "offset 99" in excinfo.value.message

    def test_negative_offset_is_an_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            geometry.resolve_targets(ifc4, selector="IfcWall", offset=-1)
        assert excinfo.value.code == "INVALID_INPUT"


class _Element:
    """Enough of an ifcopenshell element for the iterator arguments."""

    def __init__(self, element_id: int) -> None:
        self._id = element_id

    def id(self) -> int:
        return self._id


def _iterator_spy(monkeypatch) -> list[int]:
    """Record the worker count element_meshes asks the iterator for."""
    import ifcopenshell.geom as geom

    seen: list[int] = []

    class _Stub:
        def initialize(self) -> bool:
            return False

    def fake_iterator(settings, ifc, cpus, **kwargs):
        seen.append(cpus)
        return _Stub()

    monkeypatch.setattr(geom, "iterator", fake_iterator)
    return seen


class TestTessellation:
    def test_small_sets_run_on_one_thread(self, ifc4, monkeypatch):
        seen = _iterator_spy(monkeypatch)
        geometry.element_meshes(ifc4, [_Element(i) for i in range(31)])
        assert seen == [1]

    def test_big_sets_still_use_the_pool(self, ifc4, monkeypatch):
        import multiprocessing

        seen = _iterator_spy(monkeypatch)
        geometry.element_meshes(ifc4, [_Element(i) for i in range(32)])
        assert seen == [max(1, min(multiprocessing.cpu_count() - 1, 8))]


class TestMeshProvider:
    def test_probe_reads_its_meshes_from_the_provider(self, ifc4):
        wall = ifc4.by_type("IfcWall")[0]
        seen = []

        def provider(ifc, elements):
            seen.append([e.id() for e in elements])
            return geometry.element_meshes(ifc, elements)

        with geometry.mesh_provider(provider):
            report = geometry.probe_elements(ifc4, global_ids=[wall.GlobalId])
        assert seen == [[wall.id()]]
        assert report["elements"][0]["volume"] == pytest.approx(3.0, rel=0.02)

    def test_provider_is_not_re_entered_by_its_own_tessellation(self, ifc4, monkeypatch):
        """A cache fills its misses through element_meshes; that must not loop."""
        walls = ifc4.by_type("IfcWall")
        entries = []

        def provider(ifc, elements):
            entries.append(len(elements))
            return geometry.element_meshes(ifc, elements)

        with geometry.mesh_provider(provider):
            meshes = geometry.element_meshes(ifc4, walls)
        assert entries == [len(walls)]
        assert set(meshes) == {w.id() for w in walls}
        # the slot is restored for the next call inside the block
        with geometry.mesh_provider(provider):
            geometry.element_meshes(ifc4, walls[:1])
        assert entries == [len(walls), 1]

    def test_the_provider_is_cleared_when_the_block_ends(self, ifc4):
        def provider(ifc, elements):
            raise AssertionError("provider outlived its block")

        with geometry.mesh_provider(provider):
            pass
        assert geometry.element_meshes(ifc4, ifc4.by_type("IfcWall")[:1])


class TestProbe:
    def test_wall_extents_volume_and_confidence(self, ifc4):
        report = geometry.probe_elements(ifc4, selector="IfcWall, Name=Wall-1")
        record = report["elements"][0]
        extents = record["local_extents"]
        assert extents["x"] == pytest.approx(5.0, rel=0.01)
        assert extents["y"] == pytest.approx(0.2, rel=0.01)
        assert extents["z"] == pytest.approx(3.0, rel=0.01)
        assert record["volume"] == pytest.approx(3.0, rel=0.02)
        assert record["footprint_area"] == pytest.approx(1.0, rel=0.02)
        assert record["confidence"] == "high"
        assert record["placement_aligned"] is True

    def test_rotated_wall_keeps_its_local_dimensions(self, ifc4):
        """Wall-2 is rotated 90 degrees; world extents swap but local ones hold."""
        report = geometry.probe_elements(ifc4, selector="IfcWall, Name=Wall-2")
        record = report["elements"][0]
        extents = record["local_extents"]
        assert extents["x"] == pytest.approx(4.0, rel=0.01)
        assert extents["y"] == pytest.approx(0.2, rel=0.01)
        aabb = record["aabb"]
        world_x = aabb["max"][0] - aabb["min"][0]
        assert world_x == pytest.approx(0.2, rel=0.05)

    def test_units_block_names_both_worlds(self, ifc4):
        report = geometry.probe_elements(ifc4, selector="IfcWall")
        assert report["units"]["length_unit"] == "MILLIMETRE"
        assert report["units"]["to_si_factor"] == pytest.approx(0.001)
        assert report["matched"] == 3
        assert report["returned"] == 3

    def test_offset_pages_the_probe(self, ifc4):
        every = geometry.probe_elements(ifc4, selector="IfcWall")
        page = geometry.probe_elements(ifc4, selector="IfcWall", offset=2)
        assert page["returned"] == 1
        assert page["elements"][0]["global_id"] == every["elements"][2]["global_id"]

    def test_elements_without_geometry_are_reported(self, ifc4):
        door = ifc4.by_type("IfcDoor")[0]
        wall = ifc4.by_type("IfcWall")[0]
        report = geometry.probe_elements(ifc4, global_ids=[wall.GlobalId, door.GlobalId])
        assert door.GlobalId in report["without_geometry"]

    def test_all_without_geometry_is_a_clear_error(self, ifc4):
        door = ifc4.by_type("IfcDoor")[0]
        with pytest.raises(ToolError) as excinfo:
            geometry.probe_elements(ifc4, global_ids=[door.GlobalId])
        assert excinfo.value.code == "NO_GEOMETRY"
