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
