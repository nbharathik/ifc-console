"""Clash detection between two selector-defined element sets.

IfcOpenShell ships `tree.clash_*_many`, but those need an OpenCASCADE build
(`ifcopenshell.geom.has_occ`) and the stock wheel has none, so overlaps are
computed here from world-space triangle meshes: an axis-aligned broad phase,
then a sampled solid-occupancy test. Surface-intersection tests were tried and
rejected: grid-aligned BIM solids overlap while their triangles only touch.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ifc_console.core.results import ToolError

MODES = ("overlap", "clearance")
PRECISIONS = ("exact", "fast")

# Voids, zones and drafting aids share space with real elements by design, so
# they are dropped unless the caller asks for them.
NON_PHYSICAL = (
    "IfcOpeningElement",
    "IfcSpace",
    "IfcAnnotation",
    "IfcGrid",
    "IfcVirtualElement",
    "IfcSpatialZone",
)

MAX_ELEMENTS = 5000
MAX_RESULTS = 1000

# Rows of the broad-phase pair matrix built at a time.
_BROAD_PHASE_ROWS = 256


def _non_physical(element, cache: dict[str, bool]) -> bool:
    name = element.is_a()
    hit = cache.get(name)
    if hit is None:
        hit = any(element.is_a(cls) for cls in NON_PHYSICAL)
        cache[name] = hit
    return hit


# Triangles per element above this are dropped: an element that dense is a
# rendering mesh, not something a coordinator clashes by hand.
_MAX_TRIANGLES = 20_000
_EPS = 1e-9


def _selected(ifc: Any, selector: str) -> list[Any]:
    import ifcopenshell.util.selector as selector_util

    try:
        return list(selector_util.filter_elements(ifc, selector))
    except Exception as exc:
        raise ToolError(
            "INVALID_QUERY",
            f"selector failed: {exc}",
            "Use query_elements selector syntax, e.g. `IfcWall` or "
            "`IfcDuctSegment, material=steel`.",
        ) from exc


def _meshes(ifc: Any, elements: list[Any]) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """World-space (vertices, triangle indices) per element id, geometry only."""
    import multiprocessing

    import ifcopenshell.geom as geom

    wanted = {e.id() for e in elements}
    if not wanted:
        return {}

    settings = geom.settings()
    settings.set("use-world-coords", True)
    out: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    try:
        cpus = max(1, min(multiprocessing.cpu_count() - 1, 8))
    except NotImplementedError:
        cpus = 1

    iterator = geom.iterator(settings, ifc, cpus, include=elements)
    if not iterator.initialize():
        return out
    while True:
        shape = iterator.get()
        if shape is not None and shape.id in wanted:
            verts = np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
            faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
            if len(verts) and len(faces) and len(faces) <= _MAX_TRIANGLES:
                out[shape.id] = (verts, faces)
        if not iterator.next():
            break
    return out


def _boxes(meshes: dict[int, tuple[np.ndarray, np.ndarray]], ids: list[int]):
    lows = np.zeros((len(ids), 3))
    highs = np.zeros((len(ids), 3))
    for i, eid in enumerate(ids):
        verts = meshes[eid][0]
        lows[i] = verts.min(axis=0)
        highs[i] = verts.max(axis=0)
    return lows, highs


def _candidates(lo_a, hi_a, lo_b, hi_b, mode: str, tolerance: float):
    """Broad phase: index pairs whose boxes overlap, or sit within tolerance."""
    # gap > 0 means separated on that axis; gap < 0 means they interpenetrate.
    # Built in row blocks: the full (n_a, n_b, 3) temporary is ~1.8 GB at the
    # 5000-element cap, while the result itself is only (n_a, n_b).
    worst = np.empty((len(lo_a), len(lo_b)))
    for start in range(0, len(lo_a), _BROAD_PHASE_ROWS):
        stop = min(start + _BROAD_PHASE_ROWS, len(lo_a))
        block = np.maximum(
            lo_a[start:stop, None, :] - hi_b[None, :, :],
            lo_b[None, :, :] - hi_a[start:stop, None, :],
        )
        worst[start:stop] = block.max(axis=2)
    hits = worst < -tolerance if mode == "overlap" else (worst >= -tolerance) & (worst <= tolerance)
    return np.argwhere(hits), worst


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

    tri_lo = tris.min(axis=1)
    tri_hi = tris.max(axis=1)
    # Only triangles spanning the point in Y and Z, and not fully behind it.
    mask = (
        (points[:, None, 1] >= tri_lo[None, :, 1])
        & (points[:, None, 1] <= tri_hi[None, :, 1])
        & (points[:, None, 2] >= tri_lo[None, :, 2])
        & (points[:, None, 2] <= tri_hi[None, :, 2])
        & (points[:, None, 0] <= tri_hi[None, :, 0])
    )
    pi, ti = np.nonzero(mask)
    counts = np.zeros(len(points), dtype=np.int64)

    step = 200_000
    for start in range(0, len(pi), step):
        pidx = pi[start : start + step]
        tidx = ti[start : start + step]
        p = points[pidx]
        a = tris[tidx, 0, :]
        e1 = tris[tidx, 1, :] - a
        e2 = tris[tidx, 2, :] - a
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
        np.add.at(counts, pidx[hit], 1)
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
    precision: str = "exact",
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
            "Use precision='exact' for a real solid test, 'fast' for boxes only.",
        )
    if tolerance < 0:
        raise ToolError(
            "INVALID_INPUT",
            "tolerance must not be negative",
            "Pass a tolerance in metres, e.g. 0.01.",
        )

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
    pairs, worst = _candidates(lo_a, hi_a, lo_b, hi_b, mode, tolerance)

    seen: set[tuple[int, int]] = set()
    clashes: list[dict[str, Any]] = []
    checked = 0

    for i, j in pairs:
        id_a = ids_a[i]
        id_b = ids_b[j]
        # Express ids only identify an element within its own file: two
        # revisions of the same model share them, so an id match across
        # models is not a self-pair.
        if same_model:
            if id_a == id_b:
                continue
            key = (id_a, id_b) if id_a < id_b else (id_b, id_a)
            if key in seen:
                continue
            seen.add(key)
        checked += 1

        low, high = _overlap_box(lo_a[i], hi_a[i], lo_b[j], hi_b[j])
        if mode == "overlap":
            volume = None
            if precision == "exact":
                tris_a = _triangles(*meshes_a[id_a])
                tris_b = _triangles(*meshes_b[id_b])
                real, volume = _solid_overlap(tris_a, tris_b, low, high, samples)
                if not real:
                    continue
            depth = float(np.min(high - low))
            point = ((low + high) / 2.0).tolist()
            entry = {
                "type": "overlap",
                "depth": round(depth, 6),
                "point": [round(v, 6) for v in point],
                "overlap_box": {
                    "min": [round(v, 6) for v in low.tolist()],
                    "max": [round(v, 6) for v in high.tolist()],
                },
            }
            if volume is not None:
                entry["volume"] = round(volume, 6)
        else:
            gap = float(max(worst[i, j], 0.0))
            centre_a = (lo_a[i] + hi_a[i]) / 2.0
            centre_b = (lo_b[j] + hi_b[j]) / 2.0
            entry = {
                "type": "clearance",
                "gap": round(gap, 6),
                "point": [round(v, 6) for v in ((centre_a + centre_b) / 2.0).tolist()],
                # clearance measures box to box; precision='exact' has no effect
                "basis": "bounding_box",
            }

        entry["a"] = by_id_a[id_a]
        entry["b"] = by_id_b[id_b]
        clashes.append(entry)

    if mode == "overlap":
        clashes.sort(key=lambda c: -(c.get("volume") or c["depth"]))
    else:
        clashes.sort(key=lambda c: c["gap"])

    counts: dict[str, int] = {}
    for c in clashes:
        pair = " / ".join(sorted((c["a"]["class"], c["b"]["class"])))
        counts[pair] = counts.get(pair, 0) + 1
    by_class_pair = [
        {"pair": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])
    ]

    total = len(clashes)
    kept = clashes[:max_results]
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
        "precision": precision if mode == "overlap" else "bounding_box",
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
