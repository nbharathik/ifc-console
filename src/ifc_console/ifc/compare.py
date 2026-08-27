"""Revision diff between two IFC models.

Each side is reduced to a plain snapshot on the worker that owns its file, so
the comparison itself holds no IfcOpenShell state and can run on either worker.
That is what makes a diff across two attached models safe.

Positions and volumes come from world-space triangle meshes in SI metres, the
same source clash detection uses, because the stock ifcopenshell wheel has no
OpenCASCADE and therefore no BReps to compare.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc.geometry import (
    element_meshes,
    is_non_physical,
    mesh_volume,
    selected,
)
from ifc_console.ifc.units import unit_info

MAX_ELEMENTS = 20_000

# Buckets a matched element can fall into. `added` and `removed` are the two
# unmatched buckets and never combine with these.
CHANGE_KINDS = (
    "moved",
    "geometry_changed",
    "property_changed",
    "type_changed",
    "container_changed",
)

# Below this share of GlobalIds in common the two files almost certainly come
# from an exporter that regenerates ids, so id matching would report the whole
# model as replaced.
_ID_MATCH_FLOOR = 0.2

# Volume differences smaller than this are tessellation noise, not an edit.
_VOLUME_FLOOR = 1e-6

# Property changes kept per element; the count says how many were dropped.
_MAX_PROPERTY_CHANGES = 10


def _plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _flat_psets(element: Any) -> dict[str, Any]:
    """The element's own properties as `Pset_Name.Property` keys.

    Type-inherited values are excluded: a type swap is reported on its own, and
    inheriting would repeat every one of its properties as a change.
    """
    import ifcopenshell.util.element as element_util

    try:
        psets = element_util.get_psets(element, psets_only=True, should_inherit=False)
    except Exception:
        return {}
    flat: dict[str, Any] = {}
    for set_name, values in psets.items():
        if not isinstance(values, dict):
            continue
        for prop, value in values.items():
            if prop == "id":
                continue
            flat[f"{set_name}.{prop}"] = _plain(value)
    return flat


def _origin(element: Any, factor: float) -> list[float] | None:
    """Placement translation in SI metres, or None without a placement.

    The placement is authored in the file's length unit while meshes are metres,
    so the factor is what keeps a metre tolerance meaningful in a millimetre file.
    """
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        import ifcopenshell.util.placement as placement_util

        matrix = placement_util.get_local_placement(placement)
    except Exception:
        return None
    return [round(float(value) * factor, 6) for value in matrix[:3, 3]]


def _describe(element: Any) -> tuple[str | None, str | None]:
    import ifcopenshell.util.element as element_util

    try:
        type_object = element_util.get_type(element)
    except Exception:
        type_object = None
    try:
        container = element_util.get_container(element)
    except Exception:
        container = None
    return (
        getattr(type_object, "Name", None) if type_object is not None else None,
        getattr(container, "Name", None) if container is not None else None,
    )


def snapshot(
    ifc: Any,
    *,
    selector: str | None = None,
    physical_only: bool = True,
    max_elements: int = 5000,
    include_properties: bool = True,
    include_geometry: bool = True,
) -> dict[str, Any]:
    """Reduce one model to comparable plain records keyed by GlobalId."""
    elements = selected(ifc, selector) if selector else list(ifc.by_type("IfcElement"))
    if physical_only:
        verdict: dict[str, bool] = {}
        elements = [e for e in elements if not is_non_physical(e, verdict)]
    elements = [e for e in elements if getattr(e, "GlobalId", None)]
    if not elements:
        raise ToolError(
            "NO_MATCH",
            f"selector {selector!r} matched no elements" if selector else "the model has no elements",
            "Check the selector with query_elements, or omit it to diff every element.",
        )
    if len(elements) > max_elements:
        raise ToolError(
            "TOO_MANY_ELEMENTS",
            f"matched {len(elements)} elements, over the {max_elements} cap",
            "Pass a selector to diff one discipline at a time, or raise "
            "max_elements if you accept the cost.",
        )
    elements.sort(key=lambda e: e.id())

    units = unit_info(ifc)
    factor = float(units.get("to_si_factor") or 1.0)
    meshes = element_meshes(ifc, elements) if include_geometry else {}

    records: dict[str, dict[str, Any]] = {}
    without_geometry = 0
    for element in elements:
        type_name, container = _describe(element)
        record: dict[str, Any] = {
            "class": element.is_a(),
            "name": getattr(element, "Name", None),
            "type": type_name,
            "container": container,
            "origin": _origin(element, factor),
        }
        mesh = meshes.get(element.id())
        if mesh is None:
            if include_geometry:
                without_geometry += 1
        else:
            verts, faces = mesh
            low = verts.min(axis=0)
            high = verts.max(axis=0)
            record["centroid"] = [round(v, 6) for v in verts.mean(axis=0).tolist()]
            record["extents"] = [round(v, 6) for v in (high - low).tolist()]
            record["volume"] = round(mesh_volume(verts, faces), 6)
        if include_properties:
            record["props"] = _flat_psets(element)
        # a duplicate GlobalId is a broken file, not a diff input; keep the
        # first and let check_model_health be the tool that reports it
        records.setdefault(element.GlobalId, record)
    return {
        "selector": selector,
        "units": {**units, "values": "SI metres"},
        "elements": records,
        "count": len(records),
        "without_geometry": without_geometry,
        "has_geometry": include_geometry,
        "has_properties": include_properties,
    }


def _position(record: dict[str, Any]) -> tuple[list[float] | None, str]:
    centroid = record.get("centroid")
    if centroid is not None:
        return centroid, "centroid"
    return record.get("origin"), "placement"


def _signature_pairs(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[tuple[str, str]]:
    """Pair elements by identity rather than GlobalId, for regenerated exports.

    Duplicates inside one (class, type, name) group are paired in a stable
    spatial order, so a move stays a move instead of an add plus a remove.
    """

    def grouped(records: dict[str, dict[str, Any]]) -> dict[tuple, list[str]]:
        groups: dict[tuple, list[str]] = {}
        for gid, record in records.items():
            key = (record["class"], record.get("type"), record.get("name"))
            groups.setdefault(key, []).append(gid)
        for gids in groups.values():
            gids.sort(key=lambda g: tuple(_position(records[g])[0] or (0.0, 0.0, 0.0)))
        return groups

    groups_a = grouped(before)
    groups_b = grouped(after)
    pairs: list[tuple[str, str]] = []
    for key, gids_a in groups_a.items():
        for gid_a, gid_b in zip(gids_a, groups_b.get(key, []), strict=False):
            pairs.append((gid_a, gid_b))
    return pairs


def _property_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    changed = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, "__absent__")
        new = after.get(name, "__absent__")
        if old == new:
            continue
        entry: dict[str, Any] = {"name": name}
        if old != "__absent__":
            entry["before"] = old
        if new != "__absent__":
            entry["after"] = new
        changed.append(entry)
    if not changed:
        return None
    kept = changed[:_MAX_PROPERTY_CHANGES]
    diff: dict[str, Any] = {"total": len(changed), "changed": kept}
    if len(changed) > len(kept):
        diff["truncated"] = True
    return diff


def _compare_pair(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    move_tolerance: float,
    volume_tolerance: float,
) -> dict[str, Any]:
    """Every difference between one matched pair, as change kind to detail."""
    found: dict[str, Any] = {}

    pos_a, basis = _position(before)
    pos_b, basis_b = _position(after)
    if pos_a is not None and pos_b is not None and basis == basis_b:
        delta = np.asarray(pos_b, dtype=np.float64) - np.asarray(pos_a, dtype=np.float64)
        distance = float(np.linalg.norm(delta))
        if distance > move_tolerance:
            found["moved"] = {
                "by": round(distance, 6),
                "delta": [round(v, 6) for v in delta.tolist()],
                "basis": basis,
            }

    volume_a = before.get("volume")
    volume_b = after.get("volume")
    geometry: dict[str, Any] = {}
    if volume_a is not None and volume_b is not None:
        limit = max(volume_tolerance * max(abs(volume_a), abs(volume_b)), _VOLUME_FLOOR)
        if abs(volume_b - volume_a) > limit:
            geometry["volume_before"] = volume_a
            geometry["volume_after"] = volume_b
            geometry["volume_delta"] = round(volume_b - volume_a, 6)
    extents_a = before.get("extents")
    extents_b = after.get("extents")
    if extents_a is not None and extents_b is not None:
        spread = np.abs(np.asarray(extents_b) - np.asarray(extents_a))
        if float(spread.max()) > move_tolerance:
            geometry["extents_before"] = extents_a
            geometry["extents_after"] = extents_b
    if geometry:
        found["geometry_changed"] = geometry

    if before.get("type") != after.get("type"):
        found["type_changed"] = {"before": before.get("type"), "after": after.get("type")}
    if before.get("container") != after.get("container"):
        found["container_changed"] = {
            "before": before.get("container"),
            "after": after.get("container"),
        }

    props_a = before.get("props")
    props_b = after.get("props")
    if props_a is not None and props_b is not None:
        diff = _property_diff(props_a, props_b)
        if diff is not None:
            found["property_changed"] = diff
    return found


def compare_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    move_tolerance: float = 0.01,
    volume_tolerance: float = 0.01,
    max_changes: int = 100,
) -> dict[str, Any]:
    """Diff two snapshots. Pure Python: touches no IfcOpenShell state."""
    if move_tolerance < 0 or volume_tolerance < 0:
        raise ToolError(
            "INVALID_INPUT",
            "tolerances must not be negative",
            "move_tolerance is metres, volume_tolerance is a fraction, e.g. 0.01.",
        )
    records_a: dict[str, dict[str, Any]] = before["elements"]
    records_b: dict[str, dict[str, Any]] = after["elements"]
    ids_a = set(records_a)
    ids_b = set(records_b)
    shared = ids_a & ids_b
    smaller = min(len(ids_a), len(ids_b))

    if smaller and len(shared) < max(1, int(_ID_MATCH_FLOOR * smaller)):
        pairs = _signature_pairs(records_a, records_b)
        matcher = "signature"
    else:
        pairs = [(gid, gid) for gid in sorted(shared)]
        matcher = "global_id"

    matched_a = {gid_a for gid_a, _ in pairs}
    matched_b = {gid_b for _, gid_b in pairs}
    removed = sorted(ids_a - matched_a)
    added = sorted(ids_b - matched_b)

    counts = dict.fromkeys(("added", "removed", *CHANGE_KINDS), 0)
    counts["added"] = len(added)
    counts["removed"] = len(removed)
    by_class: dict[str, dict[str, int]] = {}
    buckets: dict[str, list[str]] = {name: [] for name in ("added", "removed", *CHANGE_KINDS)}
    changes: list[dict[str, Any]] = []
    unchanged = 0

    def bump(ifc_class: str, kind: str) -> None:
        entry = by_class.setdefault(ifc_class, {"added": 0, "removed": 0, "changed": 0})
        entry[kind] += 1

    for gid in removed:
        record = records_a[gid]
        bump(record["class"], "removed")
        buckets["removed"].append(gid)
        changes.append(
            {
                "global_id": gid,
                "class": record["class"],
                "name": record.get("name"),
                "change": ["removed"],
            }
        )
    for gid in added:
        record = records_b[gid]
        bump(record["class"], "added")
        buckets["added"].append(gid)
        changes.append(
            {
                "global_id": gid,
                "class": record["class"],
                "name": record.get("name"),
                "change": ["added"],
            }
        )

    for gid_a, gid_b in pairs:
        record_a = records_a[gid_a]
        record_b = records_b[gid_b]
        found = _compare_pair(
            record_a,
            record_b,
            move_tolerance=move_tolerance,
            volume_tolerance=volume_tolerance,
        )
        if not found:
            unchanged += 1
            continue
        kinds = [kind for kind in CHANGE_KINDS if kind in found]
        for kind in kinds:
            counts[kind] += 1
            buckets[kind].append(gid_b)
        bump(record_b["class"], "changed")
        entry: dict[str, Any] = {
            "global_id": gid_b,
            "class": record_b["class"],
            "name": record_b.get("name"),
            "change": kinds,
        }
        if matcher == "signature" and gid_a != gid_b:
            entry["previous_global_id"] = gid_a
        entry.update({kind: found[kind] for kind in kinds})
        changes.append(entry)

    # edits first, then removals, then additions: a clipped list should still
    # show the modifications rather than a run of new elements
    order = {"removed": 1, "added": 2}
    changes.sort(key=lambda c: (order.get(c["change"][0], 0), c["class"], c.get("name") or ""))
    kept = changes[:max_changes]

    signature_note = {
        "note": (
            "GlobalIds barely overlap, so elements were paired by class, type and "
            "name instead; ids in `changes` come from the second model"
        )
    }
    return {
        "matcher": matcher,
        **(signature_note if matcher == "signature" else {}),
        "matched_pairs": len(pairs),
        "selector": before.get("selector") or after.get("selector"),
        "tolerances": {"move_metres": move_tolerance, "volume_fraction": volume_tolerance},
        "units": before.get("units"),
        "totals": {"before": len(records_a), "after": len(records_b), "unchanged": unchanged},
        "counts": counts,
        "compared": {
            "geometry": bool(before.get("has_geometry") and after.get("has_geometry")),
            "properties": bool(before.get("has_properties") and after.get("has_properties")),
            "without_geometry": before.get("without_geometry", 0)
            + after.get("without_geometry", 0),
        },
        "by_class": by_class,
        "global_ids": {name: ids[:max_changes] for name, ids in buckets.items() if ids},
        "total": len(changes),
        "returned": len(kept),
        "truncated": len(changes) > len(kept),
        "changes": kept,
    }


def compare_models(
    ifc_before: Any,
    ifc_after: Any,
    *,
    selector: str | None = None,
    physical_only: bool = True,
    max_elements: int = 5000,
    include_properties: bool = True,
    include_geometry: bool = True,
    **compare_kwargs: Any,
) -> dict[str, Any]:
    """Snapshot both sides and diff them, for callers holding one worker."""
    options = {
        "selector": selector,
        "physical_only": physical_only,
        "max_elements": max_elements,
        "include_properties": include_properties,
        "include_geometry": include_geometry,
    }
    return compare_snapshots(
        snapshot(ifc_before, **options), snapshot(ifc_after, **options), **compare_kwargs
    )


__all__ = [
    "CHANGE_KINDS",
    "MAX_ELEMENTS",
    "compare_models",
    "compare_snapshots",
    "snapshot",
]
