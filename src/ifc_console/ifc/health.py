"""Model health checks: the "is this file any good" question.

validate_model answers a different one. It runs ifcopenshell.validate: attribute
types, cardinality, uniqueness and where-rules. A file can satisfy every rule in
the schema and still have elements in no spatial container, representations with
no solid, a beam forty kilometres from site, the same wall modelled twice, and an
extent that contradicts its declared unit. These checks look at the data the way
a BIM manager does on day one.

Geometry checks read world-space triangle meshes in SI metres and are the
expensive half, so they are skipped with a stated reason on a model larger than
max_elements rather than silently sampling it.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc.geometry import element_meshes, is_non_measurable, mesh_volume
from ifc_console.ifc.units import unit_info

CHECKS = (
    "duplicate_global_ids",
    "orphaned_elements",
    "degenerate_solids",
    "placement_outliers",
    "duplicate_placements",
    "model_extent",
    "empty_storeys",
    "unused_types",
)

GEOMETRY_CHECKS = frozenset(
    {"degenerate_solids", "placement_outliers", "duplicate_placements", "model_extent"}
)

MAX_ELEMENTS = 20_000

# Below this a solid has no measurable content at all: a millimetre cube is
# 1e-9 cubic metres, so anything smaller is a modelling accident.
_ZERO_VOLUME = 1e-9

# A centroid this many robust spreads from the model median is not a design
# decision, it is an import that landed in the wrong place.
_OUTLIER_FACTOR = 20.0

# Buildings are not this big and not this small. Either bound means the file's
# length unit and its coordinates disagree.
_EXTENT_MAX = 10_000.0
_EXTENT_MIN = 0.1

# Two elements whose centroids agree to a millimetre are the same thing twice.
_PLACEMENT_DECIMALS = 3


def _severity_counts(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"error": 0, "warning": 0, "info": 0}
    for finding in findings:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    return counts


def _finding(
    check: str,
    severity: str,
    title: str,
    examples: list[dict[str, Any]],
    total: int,
    *,
    limit: int,
    detail: str | None = None,
) -> dict[str, Any]:
    kept = examples[:limit]
    finding: dict[str, Any] = {
        "check": check,
        "severity": severity,
        "title": title,
        "count": total,
        "returned": len(kept),
    }
    if detail:
        finding["detail"] = detail
    finding["examples"] = kept
    ids = [e["global_id"] for e in kept if e.get("global_id")]
    if ids:
        finding["global_ids"] = ids
    return finding


def _duplicate_global_ids(ifc: Any, limit: int) -> dict[str, Any] | None:
    classes: dict[str, Counter] = {}
    for entity in ifc.by_type("IfcRoot"):
        gid = getattr(entity, "GlobalId", None)
        if gid:
            classes.setdefault(gid, Counter())[entity.is_a()] += 1
    duplicates = [gid for gid, seen in classes.items() if sum(seen.values()) > 1]
    if not duplicates:
        return None
    examples = [
        {
            "global_id": gid,
            "occurrences": sum(classes[gid].values()),
            "classes": sorted(classes[gid]),
        }
        for gid in sorted(duplicates)[:limit]
    ]
    return _finding(
        "duplicate_global_ids",
        "error",
        "GlobalIds are reused by more than one entity",
        examples,
        len(duplicates),
        limit=limit,
        detail="A GlobalId must be unique in the file; every tool that looks an "
        "element up by id, including the viewer, resolves to the wrong one. "
        "validate_model reports the same violation one instance at a time.",
    )


def _orphaned_elements(ifc: Any, limit: int) -> dict[str, Any] | None:
    import ifcopenshell.util.element as element_util

    verdict: dict[str, bool] = {}
    orphans = []
    for element in ifc.by_type("IfcElement"):
        if is_non_measurable(element, verdict):
            continue
        # a void or a filling is placed by the element it belongs to, not by a
        # spatial container
        if getattr(element, "VoidsElements", None) or getattr(element, "FillsVoids", None):
            continue
        try:
            if element_util.get_container(element) is not None:
                continue
            if element_util.get_aggregate(element) is not None:
                continue
        except Exception:
            continue
        orphans.append(
            {
                "global_id": getattr(element, "GlobalId", None),
                "class": element.is_a(),
                "name": getattr(element, "Name", None),
            }
        )
    if not orphans:
        return None
    return _finding(
        "orphaned_elements",
        "warning",
        "elements sit in no spatial container",
        orphans,
        len(orphans),
        limit=limit,
        detail="Nothing places these on a storey, so they are invisible to "
        "storey takeoffs, to `location=` selectors and to most viewers' trees.",
    )


def _degenerate_solids(
    ifc: Any, meshes: dict[int, Any], elements: list[Any], limit: int
) -> dict[str, Any] | None:
    bad = []
    for element in elements:
        if getattr(element, "Representation", None) is None:
            continue
        mesh = meshes.get(element.id())
        if mesh is None:
            reason = "no_mesh"
        elif mesh_volume(mesh[0], mesh[1]) < _ZERO_VOLUME:
            reason = "zero_volume"
        else:
            continue
        bad.append(
            {
                "global_id": getattr(element, "GlobalId", None),
                "class": element.is_a(),
                "name": getattr(element, "Name", None),
                "reason": reason,
            }
        )
    if not bad:
        return None
    return _finding(
        "degenerate_solids",
        "warning",
        "elements carry a representation but no usable solid",
        bad,
        len(bad),
        limit=limit,
        detail="These contribute nothing to a derived takeoff or a clash run. "
        "`no_mesh` also covers meshes too dense to tessellate here, so confirm "
        "one with get_element_geometry before reporting it as broken.",
    )


def _centroids(meshes: dict[int, Any], elements: list[Any]) -> tuple[list[Any], np.ndarray]:
    present = [e for e in elements if e.id() in meshes]
    if not present:
        return [], np.zeros((0, 3))
    points = np.array([meshes[e.id()][0].mean(axis=0) for e in present], dtype=np.float64)
    return present, points


def _placement_outliers(
    present: list[Any], points: np.ndarray, limit: int, factor: float
) -> dict[str, Any] | None:
    if len(points) < 4:
        return None
    median = np.median(points, axis=0)
    # median absolute deviation, not the interquartile range: with a handful of
    # elements one stray import moves a quartile far enough to hide itself
    spread = np.median(np.abs(points - median), axis=0)
    # a flat or single-storey model has zero spread on some axis; one metre is
    # the floor so the test cannot divide the model into outliers
    reference = max(float(spread.max()), 1.0)
    offsets = np.abs(points - median).max(axis=1)
    flagged = np.nonzero(offsets > factor * reference)[0]
    if not len(flagged):
        return None
    order = flagged[np.argsort(-offsets[flagged])]
    examples = [
        {
            "global_id": getattr(present[i], "GlobalId", None),
            "class": present[i].is_a(),
            "name": getattr(present[i], "Name", None),
            "distance_from_median": round(float(offsets[i]), 3),
        }
        for i in order[:limit]
    ]
    return _finding(
        "placement_outliers",
        "error",
        "elements sit far outside the rest of the model",
        examples,
        int(len(flagged)),
        limit=limit,
        detail=f"Flagged beyond {factor:g}x the median absolute deviation "
        f"({round(reference, 3)} m) from the model median. Usually a linked file "
        "imported at the wrong origin; check get_georeferencing too.",
    )


def _duplicate_placements(
    present: list[Any], points: np.ndarray, meshes: dict[int, Any], limit: int
) -> dict[str, Any] | None:
    groups: dict[tuple, list[Any]] = {}
    for element, centroid in zip(present, points, strict=True):
        mesh = meshes[element.id()]
        key = (
            element.is_a(),
            tuple(np.round(centroid, _PLACEMENT_DECIMALS).tolist()),
            round(mesh_volume(mesh[0], mesh[1]), 6),
        )
        groups.setdefault(key, []).append(element)
    stacked = [members for members in groups.values() if len(members) > 1]
    if not stacked:
        return None
    examples = []
    for members in sorted(stacked, key=lambda m: -len(m))[:limit]:
        examples.append(
            {
                "global_id": getattr(members[0], "GlobalId", None),
                "class": members[0].is_a(),
                "name": getattr(members[0], "Name", None),
                "copies": len(members),
                "also": [getattr(m, "GlobalId", None) for m in members[1:limit]],
            }
        )
    return _finding(
        "duplicate_placements",
        "warning",
        "identical solids share a position",
        examples,
        len(stacked),
        limit=limit,
        detail="Same class, same centroid to the millimetre, same volume. Double "
        "modelling inflates every quantity takeoff; detect_clashes confirms it.",
    )


def _model_extent(ifc: Any, meshes: dict[int, Any]) -> dict[str, Any] | None:
    if not meshes:
        return None
    lows = np.array([mesh[0].min(axis=0) for mesh in meshes.values()])
    highs = np.array([mesh[0].max(axis=0) for mesh in meshes.values()])
    span = highs.max(axis=0) - lows.min(axis=0)
    diagonal = float(np.linalg.norm(span))
    if _EXTENT_MIN <= diagonal <= _EXTENT_MAX:
        return None
    units = unit_info(ifc)
    return _finding(
        "model_extent",
        "warning",
        "the model's physical size does not look like a building",
        [
            {
                "diagonal_metres": round(diagonal, 3),
                "extent_metres": [round(v, 3) for v in span.tolist()],
                "declared_length_unit": units.get("length_unit"),
            }
        ],
        1,
        limit=1,
        detail="Geometry is always read in SI metres. A span this far off usually "
        "means the coordinates were authored in a different unit than the file "
        "declares, or a stray element is dragging the bounds.",
    )


def _empty_storeys(ifc: Any, limit: int) -> dict[str, Any] | None:
    empty = []
    for storey in ifc.by_type("IfcBuildingStorey"):
        contained = any(
            rel.RelatedElements for rel in getattr(storey, "ContainsElements", None) or ()
        )
        aggregated = any(
            rel.RelatedObjects for rel in getattr(storey, "IsDecomposedBy", None) or ()
        )
        if contained or aggregated:
            continue
        empty.append(
            {
                "global_id": getattr(storey, "GlobalId", None),
                "class": storey.is_a(),
                "name": getattr(storey, "Name", None),
            }
        )
    if not empty:
        return None
    return _finding(
        "empty_storeys",
        "info",
        "storeys hold nothing",
        empty,
        len(empty),
        limit=limit,
        detail="Either a level was never modelled, or its elements are contained "
        "somewhere else; a storey takeoff will report zero for it.",
    )


def _unused_types(ifc: Any, limit: int) -> dict[str, Any] | None:
    unused = []
    for type_object in ifc.by_type("IfcTypeObject"):
        # IFC4 names the inverse Types, IFC2X3 names it ObjectTypeOf
        relations = getattr(type_object, "Types", None) or getattr(
            type_object, "ObjectTypeOf", None
        )
        if any(getattr(rel, "RelatedObjects", None) for rel in relations or ()):
            continue
        unused.append(
            {
                "global_id": getattr(type_object, "GlobalId", None),
                "class": type_object.is_a(),
                "name": getattr(type_object, "Name", None),
            }
        )
    if not unused:
        return None
    return _finding(
        "unused_types",
        "info",
        "type objects have no occurrences",
        unused,
        len(unused),
        limit=limit,
        detail="Harmless, but they inflate the type list an author picks from.",
    )


def check_model_health(
    ifc: Any,
    *,
    checks: list[str] | None = None,
    max_findings: int = 20,
    max_elements: int = 5000,
    outlier_factor: float = _OUTLIER_FACTOR,
) -> dict[str, Any]:
    """Run the requested checks and group the findings by check."""
    wanted = list(checks) if checks else list(CHECKS)
    unknown = [name for name in wanted if name not in CHECKS]
    if unknown:
        raise ToolError(
            "INVALID_INPUT",
            f"unknown check(s): {unknown}",
            f"Allowed: {list(CHECKS)}.",
        )
    # keep the declared order whatever order the caller asked in
    wanted = [name for name in CHECKS if name in set(wanted)]

    status: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    elements = list(ifc.by_type("IfcElement"))
    needs_geometry = [name for name in wanted if name in GEOMETRY_CHECKS]
    meshes: dict[int, Any] = {}
    present: list[Any] = []
    points = np.zeros((0, 3))
    skip_reason: str | None = None

    if needs_geometry:
        if len(elements) > max_elements:
            skip_reason = (
                f"the model has {len(elements)} elements, over the {max_elements} "
                "geometry cap; raise max_elements to run these, or pass checks=[...] "
                "to run only the cheap ones"
            )
        else:
            meshes = element_meshes(ifc, elements)
            present, points = _centroids(meshes, elements)

    for name in wanted:
        if name in GEOMETRY_CHECKS and skip_reason:
            status[name] = {"status": "skipped", "reason": skip_reason}
            continue
        if name == "duplicate_global_ids":
            found = _duplicate_global_ids(ifc, max_findings)
        elif name == "orphaned_elements":
            found = _orphaned_elements(ifc, max_findings)
        elif name == "degenerate_solids":
            found = _degenerate_solids(ifc, meshes, elements, max_findings)
        elif name == "placement_outliers":
            found = _placement_outliers(present, points, max_findings, outlier_factor)
        elif name == "duplicate_placements":
            found = _duplicate_placements(present, points, meshes, max_findings)
        elif name == "model_extent":
            found = _model_extent(ifc, meshes)
        elif name == "empty_storeys":
            found = _empty_storeys(ifc, max_findings)
        else:
            found = _unused_types(ifc, max_findings)
        if found is None:
            status[name] = {"status": "ok"}
        else:
            status[name] = {
                "status": "findings",
                "severity": found["severity"],
                "count": found["count"],
            }
            findings.append(found)

    severities = _severity_counts(findings)
    return {
        "schema": ifc.schema,
        "units": {**unit_info(ifc), "geometry_values": "SI metres"},
        "elements": len(elements),
        "healthy": severities["error"] == 0 and severities["warning"] == 0,
        "summary": {
            **severities,
            "checks_run": sum(1 for s in status.values() if s["status"] != "skipped"),
            "checks_skipped": sum(1 for s in status.values() if s["status"] == "skipped"),
        },
        "checks": status,
        "findings": findings,
    }


__all__ = ["CHECKS", "GEOMETRY_CHECKS", "MAX_ELEMENTS", "check_model_health"]
