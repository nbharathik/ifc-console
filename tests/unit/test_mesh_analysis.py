"""Raw mesh health and evidence-backed directional measurements."""

from __future__ import annotations

import numpy as np
import pytest

from ifc_console.core.results import ToolError
from ifc_console.ifc.mesh_analysis import (
    directional_extent,
    mesh_hash,
    mesh_health,
    ray_intervals,
    slice_mesh,
    to_trimesh,
)

_BOX_FACES = np.array(
    [
        [0, 2, 1],
        [0, 3, 2],
        [4, 5, 6],
        [4, 6, 7],
        [0, 1, 5],
        [0, 5, 4],
        [1, 2, 6],
        [1, 6, 5],
        [2, 3, 7],
        [2, 7, 6],
        [3, 0, 4],
        [3, 4, 7],
    ],
    dtype=np.int64,
)


def _box(low=(-1.0, -1.0, -1.0), high=(1.0, 1.0, 1.0)):
    lo, hi = np.asarray(low, dtype=float), np.asarray(high, dtype=float)
    vertices = np.array(
        [
            [lo[0], lo[1], lo[2]],
            [hi[0], lo[1], lo[2]],
            [hi[0], hi[1], lo[2]],
            [lo[0], hi[1], lo[2]],
            [lo[0], lo[1], hi[2]],
            [hi[0], lo[1], hi[2]],
            [hi[0], hi[1], hi[2]],
            [lo[0], hi[1], hi[2]],
        ]
    )
    return vertices, _BOX_FACES.copy()


def _hollow_box():
    outer, outer_faces = _box()
    inner, inner_faces = _box((-0.8, -0.8, -0.8), (0.8, 0.8, 0.8))
    # A cavity shell is wound towards the void, opposite the outer boundary.
    return np.vstack([outer, inner]), np.vstack([outer_faces, inner_faces[:, ::-1] + 8])


class TestMeshHealth:
    def test_closed_box_is_a_valid_volume(self):
        vertices, faces = _box()
        health = mesh_health(vertices, faces, backend="builtin")
        assert health["watertight"] is True
        assert health["winding_consistent"] is True
        assert health["valid_volume"] is True
        assert health["connected_components"] == 1
        assert health["boundary_edges"] == 0
        assert health["euler_characteristic"] == 2
        assert health["volume_si"] == pytest.approx(8.0)

    def test_open_box_keeps_winding_but_refuses_volume(self):
        vertices, faces = _box()
        health = mesh_health(vertices, faces[:-2], backend="builtin")
        assert health["watertight"] is False
        assert health["winding_consistent"] is True
        assert health["valid_volume"] is False
        assert health["boundary_edges"] == 4
        assert "volume_unreliable" in health["flags"]

    def test_inward_box_is_not_a_valid_outward_volume(self):
        vertices, faces = _box()
        health = mesh_health(vertices, faces[:, ::-1], backend="builtin")
        assert health["watertight"] is True
        assert health["winding_consistent"] is True
        assert health["valid_volume"] is False
        assert "inward_winding" in health["flags"]

    def test_duplicate_and_degenerate_faces_are_counted(self):
        vertices, faces = _box()
        bad = np.vstack([faces, faces[0], [0, 0, 1]])
        health = mesh_health(vertices, bad, backend="builtin")
        assert health["duplicate_faces"] == 1
        assert health["degenerate_faces"] == 1
        assert health["valid_volume"] is False

    def test_disconnected_boxes_are_reported(self):
        first, faces = _box()
        second, _ = _box((3, -1, -1), (5, 1, 1))
        health = mesh_health(
            np.vstack([first, second]), np.vstack([faces, faces + 8]), backend="builtin"
        )
        assert health["connected_components"] == 2
        assert health["valid_volume"] is True

    def test_filtered_preview_never_mutates_raw_arrays(self):
        vertices, faces = _box()
        vertices_before, faces_before = vertices.tobytes(), faces.tobytes()
        result = mesh_health(
            vertices, np.vstack([faces, faces[0]]), backend="builtin", include_filtered_preview=True
        )
        assert vertices.tobytes() == vertices_before
        assert faces.tobytes() == faces_before
        assert result["filtered_copy_preview"]["used_for_measurements"] is False
        assert result["filtered_copy_preview"]["health"]["duplicate_faces"] == 0

    def test_trimesh_adapter_owns_independent_arrays(self):
        pytest.importorskip("trimesh")
        vertices, faces = _box()
        adapted = to_trimesh(vertices, faces)
        assert not np.shares_memory(adapted.vertices, vertices)
        assert not np.shares_memory(adapted.faces, faces)
        adapted.vertices[0] = 99.0
        assert not np.all(vertices[0] == 99.0)


class TestMeshHash:
    def test_hash_is_stable_and_sensitive_to_geometry(self):
        vertices, faces = _box()
        first = mesh_hash(vertices, faces)
        assert first == mesh_hash(vertices.copy(), faces.copy())
        changed = vertices.copy()
        changed[0, 0] += 0.001
        assert mesh_hash(changed, faces) != first
        assert mesh_hash(vertices, faces[:, ::-1]) != first


class TestDirectionalExtent:
    def test_diagonal_extent_returns_support_points(self):
        vertices, faces = _box((0, 0, 0), (1, 1, 1))
        result = directional_extent(vertices, faces, [1, 1, 0])
        assert result["extent_si"] == pytest.approx(np.sqrt(2.0))
        assert result["definition"] == "outside_to_outside_extent"
        assert result["support_points"]["min"][:2] == [0.0, 0.0]
        assert result["support_points"]["max"][:2] == [1.0, 1.0]

    def test_local_direction_is_transformed_to_world(self):
        vertices, faces = _box((0, 0, 0), (2, 1, 1))
        rotation = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
        result = directional_extent(
            vertices, faces, [1, 0, 0], frame="local", local_rotation=rotation
        )
        assert result["direction"]["world_direction"] == [0.0, 1.0, 0.0]
        assert result["extent_si"] == pytest.approx(1.0)

    def test_unreferenced_outlier_does_not_change_extent(self):
        vertices, faces = _box((0, 0, 0), (1, 1, 1))
        vertices = np.vstack([vertices, [999.0, 999.0, 999.0]])
        result = directional_extent(vertices, faces, [1, 0, 0])
        assert result["extent_si"] == pytest.approx(1.0)
        assert "unreferenced_or_invalid_vertices_ignored" in result["flags"]

    @pytest.mark.parametrize("direction", ([0, 0, 0], [float("nan"), 0, 0]))
    def test_invalid_direction_is_refused(self, direction):
        vertices, faces = _box()
        with pytest.raises(ToolError) as caught:
            directional_extent(vertices, faces, direction)
        assert caught.value.code == "INVALID_INPUT"


class TestRayIntervals:
    def test_solid_box_returns_one_material_interval(self):
        vertices, faces = _box()
        result = ray_intervals(
            vertices, faces, [0, 0, 0], [2, 0, 0], backend="builtin"
        )
        assert len(result["intersections"]) == 2
        assert result["material_intervals"][0]["thickness_si"] == pytest.approx(2.0)
        assert result["overall_width_si"] == pytest.approx(2.0)
        assert result["origin_classification"] == "material"
        assert result["refusal"] is None

    def test_hollow_box_preserves_two_walls_and_the_void(self):
        vertices, faces = _hollow_box()
        result = ray_intervals(
            vertices, faces, [0, 0, 0], [1, 0, 0], backend="builtin"
        )
        assert [hit["distance_si"] for hit in result["intersections"]] == pytest.approx(
            [-1.0, -0.8, 0.8, 1.0]
        )
        assert [part["thickness_si"] for part in result["material_intervals"]] == pytest.approx(
            [0.2, 0.2]
        )
        assert result["clear_internal_width_si"] == pytest.approx(1.6)
        assert result["origin_classification"] == "internal_void_or_gap"

    def test_open_mesh_returns_hits_but_refuses_pairing(self):
        vertices, faces = _box()
        result = ray_intervals(
            vertices, faces[:-2], [0, 0, 0], [1, 0, 0], backend="builtin"
        )
        assert result["intersections"]
        assert result["material_intervals"] == []
        assert result["confidence"] == "low"
        assert result["refusal"]
        assert "mesh_not_a_valid_volume" in result["flags"]

    def test_overlapping_components_are_swept_as_material_union(self):
        first, faces = _box((0, 0, 0), (2, 1, 1))
        second, _ = _box((1, 0, 0), (3, 1, 1))
        result = ray_intervals(
            np.vstack([first, second]),
            np.vstack([faces, faces + 8]),
            [0, 0.5, 0.5],
            [1, 0, 0],
            backend="builtin",
        )
        assert len(result["material_intervals"]) == 1
        assert result["material_intervals"][0]["start_distance_si"] == pytest.approx(0.0)
        assert result["material_intervals"][0]["end_distance_si"] == pytest.approx(3.0)
        assert result["non_material_intervals"] == []

    def test_line_that_misses_reports_no_width(self):
        vertices, faces = _box()
        result = ray_intervals(
            vertices, faces, [0, 5, 0], [1, 0, 0], backend="builtin"
        )
        assert result["intersections"] == []
        assert result["overall_width_si"] is None
        assert "line_misses_mesh" in result["flags"]


class TestSliceMesh:
    def test_section_has_a_reconstructable_world_frame(self):
        vertices, faces = _box()
        result = slice_mesh(
            vertices,
            faces,
            [0, 0, 1],
            origin=[0, 0, 0],
            backend="builtin",
            include_outline=True,
        )
        assert result["intersects"] is True
        assert result["section"]["closed"] is True
        assert result["section"]["area"] == pytest.approx(4.0)
        frame = result["section"]["outline_frame"]
        assert frame["origin"] == pytest.approx([0, 0, 0])
        assert len(result["section"]["outline"][0]) >= 4

    def test_missing_plane_is_explicit(self):
        vertices, faces = _box()
        result = slice_mesh(
            vertices,
            faces,
            [0, 0, 1],
            origin=[0, 0, 5],
            backend="builtin",
        )
        assert result["intersects"] is False
        assert result["section"] is None
        assert "plane_misses_mesh" in result["flags"]
