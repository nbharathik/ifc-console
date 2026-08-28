"""Clash detection between two selector-defined element sets.

IfcOpenShell ships `tree.clash_*_many`, but those need an OpenCASCADE build
(`ifcopenshell.geom.has_occ`) and the stock wheel has none, so overlaps are
computed here from world-space triangle meshes: an axis-aligned broad phase,
then a sampled solid-occupancy test. Surface-intersection tests were tried and
rejected: grid-aligned BIM solids overlap while their triangles only touch.
"""

from __future__ import annotations

import heapq
from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc.geometry import NON_PHYSICAL
from ifc_console.ifc.geometry import element_meshes as _meshes
from ifc_console.ifc.geometry import is_non_physical as _non_physical
from ifc_console.ifc.geometry import mesh_boxes as _boxes
from ifc_console.ifc.geometry import selected as _selected

MODES = ("overlap", "clearance")
PRECISIONS = ("sampled", "fast", "exact")

MAX_ELEMENTS = 5000
MAX_RESULTS = 1000

# Work limits keep peak memory independent of the total pair count.
_BROAD_PHASE_COLUMNS = 1024
_INSIDE_POINT_ROWS = 64
_INSIDE_TRIANGLE_ROWS = 512

_EPS = 1e-9


def _candidates(
    lo_a,
    hi_a,
    lo_b,
    hi_b,
    mode: str,
    tolerance: float,
    *,
    upper_triangle: bool = False,
):
    """Yield box candidates in deterministic row-major order."""
    # gap > 0 means separated on that axis; gap < 0 means they interpenetrate.
    for i in range(len(lo_a)):
        first = i + 1 if upper_triangle else 0
        for start in range(first, len(lo_b), _BROAD_PHASE_COLUMNS):
            stop = min(start + _BROAD_PHASE_COLUMNS, len(lo_b))
            gap = np.maximum(lo_a[i] - hi_b[start:stop], lo_b[start:stop] - hi_a[i])
            worst = gap.max(axis=1)
            hits = (
                worst < -tolerance
                if mode == "overlap"
                else (worst >= -tolerance) & (worst <= tolerance)
            )
            for local_j in np.flatnonzero(hits):
                yield i, start + int(local_j), float(worst[local_j])


def _overlap_box(lo_a, hi_a, lo_b, hi_b):
    low = np.maximum(lo_a, lo_b)
    high = np.minimum(hi_a, hi_b)
    return low, high


def _triangles(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    return verts[faces]


def _inside(points: np.ndarray, tris: np.ndarray) -> np.ndarray:
    """Point-in-solid by odd crossings of a +X ray (Moller-Trumbore)."""
    if not len(tris) or not len(points):
        return np.zeros(len(points), dtype=bool)

    counts = np.zeros(len(points), dtype=np.int64)
    tri_lo = tris.min(axis=1)
    tri_hi = tris.max(axis=1)
    for point_start in range(0, len(points), _INSIDE_POINT_ROWS):
        point_stop = min(point_start + _INSIDE_POINT_ROWS, len(points))
        point_block = points[point_start:point_stop]
        block_counts = np.zeros(len(point_block), dtype=np.int64)
        for tri_start in range(0, len(tris), _INSIDE_TRIANGLE_ROWS):
            tri_stop = min(tri_start + _INSIDE_TRIANGLE_ROWS, len(tris))
            lo = tri_lo[tri_start:tri_stop]
            hi = tri_hi[tri_start:tri_stop]
            # Only triangles spanning the point in Y and Z, and not fully behind it.
            mask = (
                (point_block[:, None, 1] >= lo[None, :, 1])
                & (point_block[:, None, 1] <= hi[None, :, 1])
                & (point_block[:, None, 2] >= lo[None, :, 2])
                & (point_block[:, None, 2] <= hi[None, :, 2])
                & (point_block[:, None, 0] <= hi[None, :, 0])
            )
            point_indices, triangle_indices = np.nonzero(mask)
            if not len(point_indices):
                continue

            p = point_block[point_indices]
            selected_tris = tris[tri_start + triangle_indices]
            a = selected_tris[:, 0, :]
            e1 = selected_tris[:, 1, :] - a
            e2 = selected_tris[:, 2, :] - a
            # h = d x e2 with d = (1, 0, 0)
            h = np.stack([np.zeros(len(e2)), -e2[:, 2], e2[:, 1]], axis=1)
            det = np.einsum("ij,ij->i", e1, h)
            ok = np.abs(det) > _EPS
            inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
            sv = p - a
            u = inv * np.einsum("ij,ij->i", sv, h)
            q = np.cross(sv, e1)
            v = inv * q[:, 0]
            t_hit = inv * np.einsum("ij,ij->i", e2, q)
            hit = ok & (u >= 0.0) & (u <= 1.0) & (v >= 0.0) & (u + v <= 1.0) & (t_hit > _EPS)
            block_counts += np.bincount(point_indices[hit], minlength=len(point_block))
        counts[point_start:point_stop] = block_counts
    return (counts % 2) == 1


def _sample_grid(low: np.ndarray, high: np.ndarray, budget: int) -> np.ndarray:
    """Cell centres of a near-cubic grid filling the overlap box."""
    extent = np.maximum(high - low, _EPS)
    step = float(np.cbrt(float(np.prod(extent)) / max(budget, 1)))
    if step <= 0:
        step = float(extent.max())
    counts = np.clip(np.ceil(extent / max(step, _EPS)).astype(int), 1, 64)
    while int(np.prod(counts)) > budget and counts.max() > 1:
        counts[int(np.argmax(counts))] -= 1
    # Offsets are deliberately unequal: centred samples land on the shared
    # diagonals of axis-aligned meshes, where a ray crosses two triangles at
    # once and the odd-crossing test flips the wrong way.
    offsets = (0.5, 0.4931, 0.5137)
    axes = [
        low[k] + (np.arange(counts[k]) + offsets[k]) * (extent[k] / counts[k]) for k in range(3)
    ]
    grid = np.meshgrid(*axes, indexing="ij")
    return np.stack([g.ravel() for g in grid], axis=1)


def _solid_overlap(
    tris_a: np.ndarray, tris_b: np.ndarray, low: np.ndarray, high: np.ndarray, budget: int
) -> tuple[bool, float]:
    """Sampled test for shared volume, with a volume estimate in cubic metres.

    Surface intersection is not usable here: grid-aligned BIM solids overlap
    while their triangles only meet along shared faces and edges.
    """
    points = _sample_grid(low, high, budget)
    if not len(points):
        return False, 0.0
    in_a = _inside(points, tris_a)
    if not in_a.any():
        return False, 0.0
    in_b = np.zeros(len(points), dtype=bool)
    in_b[in_a] = _inside(points[in_a], tris_b)
    hits = int(in_b.sum())
    if not hits:
        return False, 0.0
    box_volume = float(np.prod(np.maximum(high - low, 0.0)))
    return True, box_volume * hits / len(points)


def _describe(element: Any) -> dict[str, Any]:
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
    }


def prepare_set(
    ifc: Any,
    selector: str,
    *,
    physical_only: bool = True,
    max_elements: int = 1000,
) -> dict[str, Any]:
    """Resolve a selector to meshes and plain descriptors.

    Returns no IfcOpenShell objects, so the comparison can run off the worker
    that owns this model. That is what makes cross-model clash safe.
    """
    elements = _selected(ifc, selector)
    matched = len(elements)
    if physical_only:
        # is_a(cls) matches subtypes; an exact name test misses IFC4 cases like
        # IfcOpeningStandardCase. Verdicts are cached per class name.
        verdict: dict[str, bool] = {}
        elements = [e for e in elements if not _non_physical(e, verdict)]
    dropped = matched - len(elements)

    if len(elements) > max_elements:
        raise ToolError(
            "TOO_MANY_ELEMENTS",
            f"selector matched {len(elements)} elements, over the {max_elements} cap",
            "Narrow the selector, or raise max_elements if you accept the cost.",
        )
    if not elements:
        raise ToolError(
            "NO_MATCH",
            f"selector {selector!r} matched no clashable elements",
            "Check it with query_elements; voids and spaces are excluded unless "
            "physical_only=false.",
        )

    meshes = _meshes(ifc, elements)
    info = {e.id(): _describe(e) for e in elements if e.id() in meshes}
    ids = sorted(info)
    if not ids:
        raise ToolError(
            "NO_GEOMETRY",
            "none of the matched elements have usable geometry",
            "Clash needs solid geometry; annotations and empty spaces cannot clash.",
        )
    return {
        "selector": selector,
        "ids": ids,
        "info": info,
        "meshes": meshes,
        "matched": matched,
        "dropped_non_physical": dropped,
        "without_geometry": len(elements) - len(ids),
    }


def compare_sets(
    prep_a: dict[str, Any],
    prep_b: dict[str, Any],
    *,
    self_check: bool = False,
    same_model: bool = True,
    mode: str = "overlap",
    tolerance: float = 0.01,
    precision: str = "sampled",
    samples: int = 512,
    max_results: int = 200,
) -> dict[str, Any]:
    """Pair two prepared sets. Pure numpy: touches no IfcOpenShell state."""
    if mode not in MODES:
        raise ToolError(
            "INVALID_INPUT",
            f"mode must be one of {', '.join(MODES)}",
            "Use mode='overlap' for interpenetration or mode='clearance' for gaps.",
        )
    if precision not in PRECISIONS:
        raise ToolError(
            "INVALID_INPUT",
            f"precision must be one of {', '.join(PRECISIONS)}",
            "Use precision='sampled' for occupancy sampling or 'fast' for boxes only.",
        )
    if tolerance < 0:
        raise ToolError(
            "INVALID_INPUT",
            "tolerance must not be negative",
            "Pass a tolerance in metres, e.g. 0.01.",
        )

    # `exact` was the pre-release name for this sampled implementation. Keep
    # accepting it so stored calls do not break, but never report it as exact.
    effective_precision = "sampled" if precision == "exact" else precision
    meshes_a = prep_a["meshes"]
    meshes_b = prep_b["meshes"]
    by_id_a = prep_a["info"]
    by_id_b = prep_b["info"]
    ids_a = prep_a["ids"]
    ids_b = prep_b["ids"]
    skipped = prep_a["without_geometry"] + (0 if self_check else prep_b["without_geometry"])
    dropped = prep_a["dropped_non_physical"] + (0 if self_check else prep_b["dropped_non_physical"])

    lo_a, hi_a = _boxes(meshes_a, ids_a)
    lo_b, hi_b = _boxes(meshes_b, ids_b)
    index_a = {element_id: i for i, element_id in enumerate(ids_a)}
    index_b = {element_id: i for i, element_id in enumerate(ids_b)}
    retained: list[tuple[float, int, dict[str, Any]]] = []
    counts: dict[str, int] = {}
    checked = 0
    total = 0

    candidates = _candidates(
        lo_a,
        hi_a,
        lo_b,
        hi_b,
        mode,
        tolerance,
        upper_triangle=self_check and same_model,
    )
    for i, j, worst in candidates:
        id_a = ids_a[i]
        id_b = ids_b[j]
        # Express ids only identify an element within its own file: two
        # revisions of the same model share them, so an id match across
        # models is not a self-pair.
        if same_model:
            if id_a == id_b:
                continue
            reverse_i = index_a.get(id_b)
            reverse_j = index_b.get(id_a)
            if reverse_i is not None and reverse_j is not None and (reverse_i, reverse_j) < (i, j):
                reverse_gap = np.maximum(
                    lo_a[reverse_i] - hi_b[reverse_j],
                    lo_b[reverse_j] - hi_a[reverse_i],
                )
                reverse_worst = float(reverse_gap.max())
                reverse_hit = (
                    reverse_worst < -tolerance
                    if mode == "overlap"
                    else -tolerance <= reverse_worst <= tolerance
                )
                if reverse_hit:
                    continue
        checked += 1

        low, high = _overlap_box(lo_a[i], hi_a[i], lo_b[j], hi_b[j])
        if mode == "overlap":
            volume = None
            if effective_precision == "sampled":
                tris_a = _triangles(*meshes_a[id_a])
                tris_b = _triangles(*meshes_b[id_b])
                real, volume = _solid_overlap(tris_a, tris_b, low, high, samples)
                if not real:
                    continue
            depth = round(float(np.min(high - low)), 6)
            rounded_volume = round(volume, 6) if volume is not None else None
            sort_key = -(rounded_volume or depth)
        else:
            gap = round(max(worst, 0.0), 6)
            sort_key = gap

        pair = " / ".join(sorted((by_id_a[id_a]["class"], by_id_b[id_b]["class"])))
        counts[pair] = counts.get(pair, 0) + 1
        ordinal = total
        total += 1

        # The transformed rank makes the worst retained result the heap root.
        rank = (-sort_key, -ordinal)
        if max_results > 0 and (len(retained) < max_results or rank > retained[0][:2]):
            if mode == "overlap":
                entry = {
                    "type": "overlap",
                    "depth": depth,
                    "point": [round(v, 6) for v in ((low + high) / 2.0).tolist()],
                    "overlap_box": {
                        "min": [round(v, 6) for v in low.tolist()],
                        "max": [round(v, 6) for v in high.tolist()],
                    },
                }
                if rounded_volume is not None:
                    entry["volume"] = rounded_volume
            else:
                centre_a = (lo_a[i] + hi_a[i]) / 2.0
                centre_b = (lo_b[j] + hi_b[j]) / 2.0
                entry = {
                    "type": "clearance",
                    "gap": gap,
                    "point": [round(v, 6) for v in ((centre_a + centre_b) / 2.0).tolist()],
                    # clearance measures box to box; precision='exact' has no effect
                    "basis": "bounding_box",
                }
            entry["a"] = by_id_a[id_a]
            entry["b"] = by_id_b[id_b]
            item = (*rank, entry)
            if len(retained) < max_results:
                heapq.heappush(retained, item)
            else:
                heapq.heapreplace(retained, item)

    by_class_pair = [
        {"pair": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    ]

    kept = [item[2] for item in sorted(retained, key=lambda item: (-item[0], -item[1]))]
    global_ids: list[str] = []
    for c in kept:
        for side in ("a", "b"):
            gid = c[side]["global_id"]
            if gid and gid not in global_ids:
                global_ids.append(gid)

    return {
        "mode": mode,
        # Reporting the precision actually used, not the one requested: the
        # clearance pass is bounding-box only.
        "precision": effective_precision if mode == "overlap" else "bounding_box",
        "requested_precision": precision,
        "method": (
            "sampled_solid_occupancy"
            if mode == "overlap" and effective_precision == "sampled"
            else "axis_aligned_bounding_box"
        ),
        "approximate": True,
        "sample_budget": samples
        if mode == "overlap" and effective_precision == "sampled"
        else None,
        "tolerance": tolerance,
        "set_a": {"selector": prep_a["selector"], "elements": len(ids_a)},
        "set_b": {
            "selector": prep_b["selector"],
            "elements": len(ids_b),
            "self_check": self_check,
        },
        "candidate_pairs": checked,
        "total": total,
        "returned": len(kept),
        "truncated": total > len(kept),
        "elements_without_geometry": skipped,
        "non_physical_excluded": dropped,
        "by_class_pair": by_class_pair,
        "clashes": kept,
        "global_ids": global_ids,
    }


def detect_clashes(
    ifc_a: Any,
    selector_a: str,
    *,
    ifc_b: Any = None,
    selector_b: str | None = None,
    physical_only: bool = True,
    max_elements: int = 1000,
    **compare_kwargs: Any,
) -> dict[str, Any]:
    """Prepare both sets and compare them, for callers holding one worker."""
    same_model = ifc_b is None or ifc_b is ifc_a
    self_check = selector_b is None and same_model
    prep_a = prepare_set(ifc_a, selector_a, physical_only=physical_only, max_elements=max_elements)
    if self_check:
        prep_b = prep_a
    else:
        prep_b = prepare_set(
            ifc_a if ifc_b is None else ifc_b,
            selector_b or selector_a,
            physical_only=physical_only,
            max_elements=max_elements,
        )
    return compare_sets(
        prep_a, prep_b, self_check=self_check, same_model=same_model, **compare_kwargs
    )


__all__ = [
    "MAX_ELEMENTS",
    "MAX_RESULTS",
    "MODES",
    "NON_PHYSICAL",
    "PRECISIONS",
    "compare_sets",
    "detect_clashes",
    "prepare_set",
]
