"""Clash geometry: the solid-overlap test, the broad phase, and the caps."""

from __future__ import annotations

import numpy as np
import pytest

import ifc_console.ifc.clash as clash_module
from ifc_console.ifc.clash import (
    _BROAD_PHASE_COLUMNS,
    _INSIDE_POINT_ROWS,
    _INSIDE_TRIANGLE_ROWS,
    NON_PHYSICAL,
    _boxes,
    _candidates,
    _inside,
    _sample_grid,
    _solid_overlap,
    compare_sets,
    detect_clashes,
    prepare_set,
)
from ifc_console.mcp.envelope import ToolError

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
    ]
)


def box(low, high) -> np.ndarray:
    """Closed triangulated box as an (N, 3, 3) triangle array."""
    lo = np.asarray(low, dtype=float)
    hi = np.asarray(high, dtype=float)
    corners = np.array(
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
    return corners[_BOX_FACES]


def overlap_of(low_a, high_a, low_b, high_b, samples=512):
    low = np.maximum(low_a, low_b).astype(float)
    high = np.minimum(high_a, high_b).astype(float)
    return _solid_overlap(box(low_a, high_a), box(low_b, high_b), low, high, samples)


class TestSolidOverlap:
    @pytest.mark.parametrize(
        ("low_b", "high_b", "volume"),
        [
            ([0, 0, 0], [1, 1, 1], 1.0),
            ([0.5, 0, 0], [1.5, 1, 1], 0.5),
            ([0.9, 0, 0], [1.9, 1, 1], 0.1),
            ([0.5, 0.5, 0.5], [1.5, 1.5, 1.5], 0.125),
            ([0.25, 0.25, 0.25], [0.75, 0.75, 0.75], 0.125),
        ],
    )
    def test_reports_the_shared_volume(self, low_b, high_b, volume):
        hit, got = overlap_of([0, 0, 0], [1, 1, 1], low_b, high_b)
        assert hit
        assert got == pytest.approx(volume, rel=0.05)

    def test_axis_aligned_solids_are_not_missed(self):
        """Grid-aligned boxes only ever touch along shared faces, so a surface
        intersection test says no. The occupancy test must still say yes."""
        hit, volume = overlap_of([0, 0, 0], [1, 1, 1], [0.5, 0, 0], [1.5, 1, 1])
        assert hit
        assert volume > 0.4

    def test_touching_faces_do_not_overlap(self):
        low = np.array([1.0, 0.0, 0.0])
        high = np.array([1.0, 1.0, 1.0])
        hit, volume = _solid_overlap(
            box([0, 0, 0], [1, 1, 1]), box([1, 0, 0], [2, 1, 1]), low, high, 512
        )
        assert not hit
        assert volume == 0.0


class TestSampleGrid:
    def test_stays_inside_the_box(self):
        low = np.array([0.0, 0.0, 0.0])
        high = np.array([2.0, 1.0, 0.5])
        points = _sample_grid(low, high, 512)
        assert len(points)
        assert np.all(points >= low) and np.all(points <= high)

    def test_respects_the_budget(self):
        points = _sample_grid(np.zeros(3), np.array([3.0, 3.0, 3.0]), 64)
        assert len(points) <= 64

    def test_offsets_avoid_mesh_diagonals(self):
        """Centred samples land on the diagonal of an axis-aligned face, where a
        ray crosses two triangles and the odd-crossing test flips."""
        points = _sample_grid(np.zeros(3), np.ones(3), 512)
        on_diagonal = np.isclose(points[:, 1], points[:, 2])
        assert not on_diagonal.any()


class TestBroadPhase:
    def test_overlap_mode_needs_real_penetration(self):
        lo_a = np.array([[0.0, 0.0, 0.0]])
        hi_a = np.array([[1.0, 1.0, 1.0]])
        lo_b = np.array([[1.0, 0.0, 0.0]])
        hi_b = np.array([[2.0, 1.0, 1.0]])
        assert list(_candidates(lo_a, hi_a, lo_b, hi_b, "overlap", 0.01)) == []

    def test_clearance_mode_finds_near_neighbours(self):
        lo_a = np.array([[0.0, 0.0, 0.0]])
        hi_a = np.array([[1.0, 1.0, 1.0]])
        lo_b = np.array([[1.02, 0.0, 0.0]])
        hi_b = np.array([[2.0, 1.0, 1.0]])
        candidates = list(_candidates(lo_a, hi_a, lo_b, hi_b, "clearance", 0.05))
        assert len(candidates) == 1
        assert candidates[0][:2] == (0, 0)
        assert candidates[0][2] == pytest.approx(0.02)

    def test_candidates_are_chunked_and_row_major(self, monkeypatch):
        columns = _BROAD_PHASE_COLUMNS + 7
        lo_a = np.zeros((2, 3))
        hi_a = np.ones((2, 3))
        lo_b = np.zeros((columns, 3))
        hi_b = np.ones((columns, 3))
        original_maximum = np.maximum
        shapes: list[tuple[int, ...]] = []

        def tracking_maximum(left, right):
            result = original_maximum(left, right)
            shapes.append(result.shape)
            return result

        monkeypatch.setattr(clash_module.np, "maximum", tracking_maximum)
        candidates = list(_candidates(lo_a, hi_a, lo_b, hi_b, "overlap", 0.0))

        assert [(i, j) for i, j, _ in candidates[: columns + 1]] == [
            *((0, j) for j in range(columns)),
            (1, 0),
        ]
        assert shapes
        assert max(shape[0] for shape in shapes) <= _BROAD_PHASE_COLUMNS

    def test_self_check_can_stream_only_the_upper_triangle(self):
        lows = np.zeros((3, 3))
        highs = np.ones((3, 3))

        candidates = _candidates(
            lows,
            highs,
            lows,
            highs,
            "overlap",
            0.0,
            upper_triangle=True,
        )

        assert [(i, j) for i, j, _ in candidates] == [(0, 1), (0, 2), (1, 2)]

    def test_boxes_come_from_the_mesh(self):
        verts = np.array([[0.0, 0.0, 0.0], [2.0, 3.0, 4.0]])
        faces = np.array([[0, 1, 0]])
        low, high = _boxes({7: (verts, faces)}, [7])
        assert low[0].tolist() == [0.0, 0.0, 0.0]
        assert high[0].tolist() == [2.0, 3.0, 4.0]


class TestInsideMemory:
    def test_point_triangle_masks_are_bounded(self, monkeypatch):
        seed_points = np.array([[0.25, 0.24, 0.31], [1.25, 0.24, 0.31]])
        points = np.tile(seed_points, (_INSIDE_POINT_ROWS + 1, 1))
        triangles = np.tile(box([0, 0, 0], [1, 1, 1]), (_INSIDE_TRIANGLE_ROWS // 12 + 1, 1, 1))
        expected = np.tile([True, False], _INSIDE_POINT_ROWS + 1)
        original_nonzero = np.nonzero
        shapes: list[tuple[int, ...]] = []

        def tracking_nonzero(values):
            shapes.append(values.shape)
            return original_nonzero(values)

        monkeypatch.setattr(clash_module.np, "nonzero", tracking_nonzero)
        result = _inside(points, triangles)

        assert result.tolist() == expected.tolist()
        assert shapes
        assert all(
            rows <= _INSIDE_POINT_ROWS and columns <= _INSIDE_TRIANGLE_ROWS
            for rows, columns in shapes
        )


class TestAgainstAModel:
    def test_touching_walls_are_not_a_clash(self, ifc4):
        report = detect_clashes(ifc4, "IfcWall", mode="overlap")
        assert report["total"] == 0

    def test_a_duplicated_wall_clashes_at_its_full_volume(self, ifc4):
        import ifcopenshell
        import ifcopenshell.util.element as element_util

        original = ifc4.by_type("IfcWall")[0]
        copy = element_util.copy(ifc4, original)
        copy.GlobalId = ifcopenshell.guid.new()

        report = detect_clashes(ifc4, "IfcWall", mode="overlap")
        assert report["total"] == 1
        clash = report["clashes"][0]
        # the fixture wall is 5.0 x 0.2 x 3.0
        assert clash["volume"] == pytest.approx(3.0, rel=0.05)
        assert clash["type"] == "overlap"
        assert clash["a"]["global_id"] in report["global_ids"]
        assert clash["b"]["global_id"] in report["global_ids"]

    def test_clearance_finds_the_touching_walls(self, ifc4):
        report = detect_clashes(ifc4, "IfcWall", mode="clearance", tolerance=0.5)
        assert report["total"] > 0
        assert all(c["type"] == "clearance" for c in report["clashes"])
        assert report["clashes"][0]["gap"] == pytest.approx(0.0, abs=1e-6)

    def test_fast_precision_keeps_every_box_candidate(self, ifc4):
        import ifcopenshell
        import ifcopenshell.util.element as element_util

        copy = element_util.copy(ifc4, ifc4.by_type("IfcWall")[0])
        copy.GlobalId = ifcopenshell.guid.new()

        sampled = detect_clashes(ifc4, "IfcWall", mode="overlap", precision="sampled")
        fast = detect_clashes(ifc4, "IfcWall", mode="overlap", precision="fast")
        assert fast["total"] >= sampled["total"] >= 1
        # only the sampled path can estimate the overlap
        assert "volume" not in fast["clashes"][0]
        assert "volume" in sampled["clashes"][0]
        assert sampled["method"] == "sampled_solid_occupancy"
        assert sampled["approximate"] is True

    def test_exact_is_a_compatibility_alias_not_a_correctness_claim(self, ifc4):
        report = detect_clashes(ifc4, "IfcWall", mode="overlap", precision="exact")
        assert report["precision"] == "sampled"
        assert report["requested_precision"] == "exact"

    def test_non_physical_classes_are_excluded_by_default(self, ifc4):
        report = prepare_set(ifc4, "IfcProduct")
        classes = {info["class"] for info in report["info"].values()}
        assert not classes & set(NON_PHYSICAL)

    def test_non_physical_subtypes_are_excluded_too(self, ifc4):
        """IFC4 adds IfcOpeningStandardCase; an exact class-name test misses it."""
        from ifc_console.ifc.clash import _non_physical

        standard = ifc4.create_entity("IfcOpeningStandardCase")
        cache: dict[str, bool] = {}
        assert _non_physical(standard, cache)
        assert not _non_physical(ifc4.by_type("IfcWall")[0], cache)

    def test_element_cap_is_enforced(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            prepare_set(ifc4, "IfcWall", max_elements=1)
        assert excinfo.value.code == "TOO_MANY_ELEMENTS"

    def test_empty_selector_is_a_clear_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            prepare_set(ifc4, "IfcPile")
        assert excinfo.value.code == "NO_MATCH"

    def test_bad_selector_is_a_clear_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            prepare_set(ifc4, "this is not a selector((")
        assert excinfo.value.code == "INVALID_QUERY"

    def test_prepared_sets_carry_no_ifc_objects(self, ifc4):
        """Cross-model clash depends on this: the comparison must be safe to run
        off the worker that owns the model."""
        prep = prepare_set(ifc4, "IfcWall")
        assert set(prep["info"].values().__iter__().__next__()) == {
            "global_id",
            "class",
            "name",
        }
        for verts, faces in prep["meshes"].values():
            assert isinstance(verts, np.ndarray) and isinstance(faces, np.ndarray)

    def test_compare_rejects_a_bad_mode(self, ifc4):
        prep = prepare_set(ifc4, "IfcWall")
        with pytest.raises(ToolError) as excinfo:
            compare_sets(prep, prep, self_check=True, mode="sideways")
        assert excinfo.value.code == "INVALID_INPUT"

    def test_compare_rejects_a_negative_tolerance(self, ifc4):
        prep = prepare_set(ifc4, "IfcWall")
        with pytest.raises(ToolError) as excinfo:
            compare_sets(prep, prep, self_check=True, tolerance=-1.0)
        assert excinfo.value.code == "INVALID_INPUT"

    def test_results_are_capped_and_flagged(self, ifc4):
        prep = prepare_set(ifc4, "IfcWall")
        report = compare_sets(
            prep, prep, self_check=True, mode="clearance", tolerance=10.0, max_results=1
        )
        assert report["returned"] == 1
        assert report["truncated"] is (report["total"] > 1)

    def test_truncation_retains_stable_best_results_and_full_counts(self):
        count = 50

        def mesh(low, high):
            return np.array([low, high], dtype=float), np.array([[0, 1, 0]])

        prep_a = {
            "selector": "a",
            "ids": [1],
            "info": {1: {"global_id": "A", "class": "IfcWall", "name": "A"}},
            "meshes": {1: mesh([0, 0, 0], [1, 1, 1])},
            "without_geometry": 0,
            "dropped_non_physical": 0,
        }
        ids_b = list(range(100, 100 + count))
        prep_b = {
            "selector": "b",
            "ids": ids_b,
            "info": {
                element_id: {
                    "global_id": f"B{position}",
                    "class": "IfcWall",
                    "name": f"B{position}",
                }
                for position, element_id in enumerate(ids_b)
            },
            "meshes": {
                element_id: mesh(
                    [1 + (position // 5) * 0.01, 0, 0],
                    [2 + (position // 5) * 0.01, 1, 1],
                )
                for position, element_id in enumerate(ids_b)
            },
            "without_geometry": 0,
            "dropped_non_physical": 0,
        }

        report = compare_sets(
            prep_a,
            prep_b,
            same_model=False,
            mode="clearance",
            tolerance=1.0,
            max_results=3,
        )

        assert report["candidate_pairs"] == count
        assert report["total"] == count
        assert report["returned"] == 3
        assert report["truncated"] is True
        assert [item["b"]["global_id"] for item in report["clashes"]] == ["B0", "B1", "B2"]
        assert report["by_class_pair"] == [{"pair": "IfcWall / IfcWall", "count": count}]
