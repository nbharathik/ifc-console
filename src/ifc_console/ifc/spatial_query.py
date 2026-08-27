"""Geometric spatial relations: inside, above, below, crosses, near, in a box.

The selector's `location=` facet only walks IfcRelContainedInSpatialStructure,
and most exporters contain elements in the storey rather than the space, so
"which sprinklers are in Office 101" cannot be answered from relationships.
These relations answer it from geometry instead.

Everything here works on world-space triangle meshes in SI metres: the stock
ifcopenshell wheel has no OpenCASCADE, so there are no booleans or BReps. The
point-in-solid and occupancy primitives are the ones clash detection uses, and
they are unreliable on meshes that are not closed, so every result reports the
method it used and a confidence derived from the target's watertightness.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ifc_console.core.results import ToolError

# The tested primitives behind detect_clashes; a solid relation is the same
# question asked of one element instead of two sets.
from ifc_console.ifc.clash import _inside as point_in_solid
from ifc_console.ifc.clash import _solid_overlap as solid_overlap
from ifc_console.ifc.geometry import (
    element_meshes,
    is_non_physical,
    mesh_boxes,
    points_to_triangles_distance,
    resolve_targets,
    selected,
)
from ifc_console.ifc.units import unit_info

RELATIONS = ("inside", "above", "below", "crosses", "within_distance", "within_box")

MAX_ELEMENTS = 5000

# Occupancy samples per candidate for `crosses`, matching detect_clashes.
_SAMPLES = 512

# Corners are pulled a hair towards the centroid: a candidate sharing a face
# with its container puts sample points exactly on the target surface, where an
# odd-crossing ray test has no defined answer.
_CORNER_INSET = 0.999

_EPS = 1e-9


def _watertight(faces: np.ndarray) -> bool:
    """True when every edge is shared by exactly two triangles."""
    edges = np.concatenate([faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]])
    edges = np.sort(edges, axis=1)
    _, counts = np.unique(edges, axis=0, return_counts=True)
    return bool(np.all(counts == 2))


def _sample_points(low: np.ndarray, high: np.ndarray) -> np.ndarray:
    """Centroid plus the eight bounding-box corners of one candidate."""
    centre = (low + high) / 2.0
    corners = np.array(
        [[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])]
    )
    corners = centre + (corners - centre) * _CORNER_INSET
    return np.vstack([centre[None, :], corners])


def _plan_overlap(lo_a, hi_a, lo_b, hi_b, tolerance: float) -> float | None:
    """Shared plan area of two boxes in square metres, or None when disjoint.

    tolerance only decides whether they count as overlapping; the reported area
    is the real one, so a slack of a centimetre cannot inflate it.
    """
    low = np.maximum(lo_a[:2], lo_b[:2])
    high = np.minimum(hi_a[:2], hi_b[:2])
    span = high - low
    if float(span.min()) < -tolerance:
        return None
    return float(max(float(span[0]), 0.0) * max(float(span[1]), 0.0))


def _box_overlap_fraction(lo_a, hi_a, lo_b, hi_b) -> float:
    """Share of box A's volume that lies inside box B."""
    span = np.maximum(np.minimum(hi_a, hi_b) - np.maximum(lo_a, lo_b), 0.0)
    own = float(np.prod(np.maximum(hi_a - lo_a, _EPS)))
    return float(np.prod(span)) / own if own > _EPS else 0.0


def _describe(element: Any) -> dict[str, Any]:
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
    }


def _candidate_set(
    ifc: Any,
    selector: str,
    *,
    physical_only: bool,
    max_elements: int,
    exclude_id: int | None,
) -> list[Any]:
    elements = [e for e in selected(ifc, selector) if e.id() != exclude_id]
    if physical_only:
        verdict: dict[str, bool] = {}
        elements = [e for e in elements if not is_non_physical(e, verdict)]
    if not elements:
        raise ToolError(
            "NO_MATCH",
            f"selector {selector!r} matched no candidate elements",
            "Check it with query_elements; spaces and voids are excluded unless "
            "physical_only=false.",
        )
    if len(elements) > max_elements:
        raise ToolError(
            "TOO_MANY_ELEMENTS",
            f"selector matched {len(elements)} elements, over the {max_elements} cap",
            "Every candidate has to be tessellated, so this tool needs a scoped "
            "selector on any real model: name the class you actually mean, e.g. "
            "`IfcSprinkler` instead of `IfcElement`. query_elements with the same "
            "selector tells you how many it matches before you pay for it.",
        )
    elements.sort(key=lambda e: e.id())
    return elements


def _validated_box(box: list[float] | None) -> tuple[np.ndarray, np.ndarray]:
    if not box or len(box) != 6:
        raise ToolError(
            "INVALID_INPUT",
            "relation='within_box' needs box=[min_x, min_y, min_z, max_x, max_y, max_z]",
            "Pass six SI-metre values, or pass target_global_id to use that "
            "element's own bounding box.",
        )
    low = np.asarray(box[:3], dtype=np.float64)
    high = np.asarray(box[3:], dtype=np.float64)
    if np.any(high < low):
        low, high = np.minimum(low, high), np.maximum(low, high)
    return low, high


def query_spatial(
    ifc: Any,
    *,
    relation: str,
    target_global_id: str | None = None,
    box: list[float] | None = None,
    selector: str = "IfcElement",
    distance: float = 1.0,
    tolerance: float = 0.01,
    physical_only: bool = True,
    max_elements: int = 1000,
    max_results: int = 100,
) -> dict[str, Any]:
    """Elements standing in one geometric relation to a target or a box."""
    if relation not in RELATIONS:
        raise ToolError(
            "INVALID_INPUT",
            f"unknown relation {relation!r}",
            f"Allowed: {list(RELATIONS)}.",
        )
    if tolerance < 0 or distance < 0:
        raise ToolError(
            "INVALID_INPUT",
            "distance and tolerance must not be negative",
            "Both are metres; tolerance is the slack on a boundary, distance the reach.",
        )
    if box and relation != "within_box":
        raise ToolError(
            "INVALID_INPUT",
            f"box only applies to relation='within_box', not {relation!r}",
            "Pass target_global_id for the element the relation is about.",
        )
    if box and target_global_id:
        raise ToolError(
            "INVALID_INPUT",
            "pass either target_global_id or box, not both",
            "target_global_id uses that element's own bounding box; box gives "
            "explicit bounds instead.",
        )

    target = None
    target_mesh = None
    if target_global_id:
        # physical_only=False on purpose: a space is the most useful target of
        # all, and resolve_targets allows it when asked for by GlobalId.
        target = resolve_targets(
            ifc, global_ids=[target_global_id], physical_only=False, max_elements=1
        )[0]
        target_mesh = element_meshes(ifc, [target]).get(target.id())
        if target_mesh is None:
            raise ToolError(
                "NO_GEOMETRY",
                f"{target_global_id} has no usable geometry",
                "Spatial relations need a solid; pick an element that has a body "
                "representation, or use relation='within_box' with explicit bounds.",
            )
        target_low = target_mesh[0].min(axis=0)
        target_high = target_mesh[0].max(axis=0)
    elif relation == "within_box":
        target_low, target_high = _validated_box(box)
    else:
        raise ToolError(
            "INVALID_INPUT",
            f"relation={relation!r} needs target_global_id",
            "Pass the GlobalId of the space, wall or duct the relation is about; "
            "search_elements and get_viewer_selection return them.",
        )

    solid_relation = relation in ("inside", "crosses")
    elements = _candidate_set(
        ifc,
        selector,
        physical_only=physical_only,
        max_elements=max_elements,
        exclude_id=target.id() if target is not None else None,
    )
    meshes = element_meshes(ifc, elements)
    ids = [e.id() for e in elements if e.id() in meshes]
    if not ids:
        raise ToolError(
            "NO_GEOMETRY",
            "none of the candidate elements have usable geometry",
            "Spatial relations need solids; annotations and empty spaces have none.",
        )
    by_id = {e.id(): e for e in elements}
    lows, highs = mesh_boxes(meshes, ids)

    watertight = _watertight(target_mesh[1]) if solid_relation else None
    target_tris = target_mesh[0][target_mesh[1]] if target_mesh is not None else None

    results: list[dict[str, Any]] = []
    for index, element_id in enumerate(ids):
        low, high = lows[index], highs[index]
        entry: dict[str, Any] | None = None

        if relation == "inside":
            if _box_overlap_fraction(low, high, target_low, target_high) <= 0.0:
                continue
            hits = point_in_solid(_sample_points(low, high), target_tris)
            count = int(hits.sum())
            if not count:
                continue
            entry = {
                "status": "fully_inside" if count == len(hits) else "partially_inside",
                "containment": round(count / len(hits), 3),
                "centroid_inside": bool(hits[0]),
            }
        elif relation == "crosses":
            overlap_low = np.maximum(low, target_low)
            overlap_high = np.minimum(high, target_high)
            if float((overlap_high - overlap_low).min()) <= tolerance:
                continue
            verts, faces = meshes[element_id]
            shares, volume = solid_overlap(
                target_tris, verts[faces], overlap_low, overlap_high, _SAMPLES
            )
            if not shares:
                continue
            # a duct that ends inside the target does not pass through it
            enclosed = _box_overlap_fraction(low, high, target_low, target_high) >= 1.0 - 1e-6
            entry = {"shared_volume": round(volume, 6), "enclosed": enclosed}
        elif relation in ("above", "below"):
            area = _plan_overlap(low, high, target_low, target_high, tolerance)
            if area is None:
                continue
            gap = (
                float(low[2] - target_high[2])
                if relation == "above"
                else float(target_low[2] - high[2])
            )
            if gap < -tolerance or gap > distance:
                continue
            entry = {"gap": round(max(gap, 0.0), 6), "plan_overlap_area": round(area, 6)}
        elif relation == "within_distance":
            span = np.maximum(
                np.maximum(low - target_high, target_low - high).max(),
                0.0,
            )
            if float(span) > distance:
                continue
            verts, faces = meshes[element_id]
            surface = min(
                points_to_triangles_distance(verts, target_tris),
                points_to_triangles_distance(target_mesh[0], verts[faces]),
            )
            if surface > distance:
                continue
            entry = {"distance": round(surface, 6), "box_gap": round(float(span), 6)}
        else:  # within_box
            fraction = _box_overlap_fraction(low, high, target_low, target_high)
            if fraction <= 0.0:
                continue
            entry = {
                "status": "fully_inside" if fraction >= 1.0 - 1e-6 else "partially_inside",
                "containment": round(fraction, 3),
            }

        if entry is not None:
            results.append({**_describe(by_id[element_id]), **entry})

    if relation in ("above", "below"):
        results.sort(key=lambda r: r["gap"])
    elif relation == "within_distance":
        results.sort(key=lambda r: r["distance"])
    elif relation == "crosses":
        results.sort(key=lambda r: -r["shared_volume"])
    else:
        results.sort(key=lambda r: -r["containment"])

    by_class: dict[str, int] = {}
    for record in results:
        by_class[record["class"]] = by_class.get(record["class"], 0) + 1
    kept = results[:max_results]

    method = {
        "inside": "point_in_solid_sampled",
        "crosses": "sampled_solid_occupancy",
        "above": "axis_aligned_plan_overlap",
        "below": "axis_aligned_plan_overlap",
        "within_distance": "closest_point_surface_distance",
        "within_box": "axis_aligned_bounding_box",
    }[relation]
    if solid_relation:
        confidence = "high" if watertight else "low"
    elif relation == "within_distance":
        confidence = "high"
    else:
        confidence = "medium"

    payload: dict[str, Any] = {
        "relation": relation,
        "method": method,
        "approximate": relation != "within_distance",
        "confidence": confidence,
        "selector": selector,
        "units": {**unit_info(ifc), "values": "SI metres"},
        "candidates": len(ids),
        "total": len(results),
        "returned": len(kept),
        "truncated": len(results) > len(kept),
        "by_class": by_class,
        "global_ids": [r["global_id"] for r in kept if r["global_id"]],
        "results": kept,
    }
    if target is not None:
        payload["target"] = {
            **_describe(target),
            "aabb": {
                "min": [round(v, 6) for v in target_low.tolist()],
                "max": [round(v, 6) for v in target_high.tolist()],
            },
        }
        if solid_relation:
            payload["target"]["watertight"] = watertight
            if not watertight:
                payload["note"] = (
                    "the target mesh is not closed, so point-in-solid answers near "
                    "its boundary are unreliable; treat partially_inside as a hint"
                )
    else:
        payload["box"] = {
            "min": [round(v, 6) for v in target_low.tolist()],
            "max": [round(v, 6) for v in target_high.tolist()],
        }
    if relation in ("above", "below", "within_distance"):
        payload["distance_limit"] = distance
    return payload


__all__ = ["MAX_ELEMENTS", "RELATIONS", "query_spatial"]
