"""Quantity takeoff from stored Qto_* sets, plus georeferencing facts.

Stored quantities only: trustworthy numbers straight from the authoring tool.
A geometry fallback for missing values is planned as a separate opt-in step.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ifc_console.ifc.info import _units
from ifc_console.mcp.envelope import ToolError

AGGREGATE_BY = ("class", "type", "storey", "material", "none")

_MAX_ELEMENTS = 10_000


def _storey_name(element: Any) -> str:
    import ifcopenshell.util.element as element_util

    item = element
    while item is not None:
        container = element_util.get_container(item)
        if container is None:
            item = element_util.get_aggregate(item)
            continue
        if container.is_a("IfcBuildingStorey"):
            return container.Name or "(unnamed storey)"
        item = container
    return "(no storey)"


def _material_name(element: Any) -> str:
    import ifcopenshell.util.element as element_util

    try:
        material = element_util.get_material(element)
    except Exception:
        material = None
    if material is None:
        return "(no material)"
    name = getattr(material, "Name", None)
    return name or f"({material.is_a()})"


def _group_key(element: Any, aggregate_by: str) -> str:
    import ifcopenshell.util.element as element_util

    if aggregate_by == "class":
        return element.is_a()
    if aggregate_by == "type":
        try:
            type_entity = element_util.get_type(element)
        except Exception:
            type_entity = None
        if type_entity is None:
            return "(no type)"
        return getattr(type_entity, "Name", None) or type_entity.is_a()
    if aggregate_by == "storey":
        return _storey_name(element)
    if aggregate_by == "material":
        return _material_name(element)
    return "all"


def compute_quantities(
    ifc: Any,
    selector: str,
    *,
    aggregate_by: str = "class",
    quantities: tuple[str, ...] | None = None,
    max_elements: int = _MAX_ELEMENTS,
) -> dict[str, Any]:
    """Sum stored quantity-set values for a selector-defined element set."""
    import ifcopenshell.util.element as element_util
    import ifcopenshell.util.selector as selector_util

    try:
        elements = list(selector_util.filter_elements(ifc, selector))
    except Exception as exc:
        raise ToolError(
            "INVALID_QUERY",
            f"selector failed: {exc}",
            "Use query_elements selector syntax, e.g. `IfcWall` or "
            "`IfcSlab, material=concrete`.",
        ) from exc

    matched = len(elements)
    skipped = max(0, matched - max_elements)
    elements = elements[:max_elements]

    groups: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, int] = defaultdict(int)
    without_quantities = 0
    totals: dict[str, float] = defaultdict(float)

    for element in elements:
        key = _group_key(element, aggregate_by)
        counts[key] += 1
        try:
            qtos = element_util.get_psets(element, qtos_only=True)
        except Exception:
            qtos = {}
        found_any = False
        for qset in qtos.values():
            for name, value in qset.items():
                if name == "id" or isinstance(value, bool):
                    continue
                if not isinstance(value, (int, float)):
                    continue
                if quantities and name not in quantities:
                    continue
                groups[key][name] += float(value)
                totals[name] += float(value)
                found_any = True
        if not found_any:
            without_quantities += 1

    group_rows = [
        {
            "group": key,
            "count": counts[key],
            "quantities": {n: round(v, 6) for n, v in sorted(values.items())},
        }
        for key, values in sorted(groups.items())
    ]
    # groups with matches but no numeric quantities still deserve a row
    for key, count in sorted(counts.items()):
        if key not in groups:
            group_rows.append({"group": key, "count": count, "quantities": {}})
    group_rows.sort(key=lambda row: row["group"])

    result: dict[str, Any] = {
        "selector": selector,
        "aggregate_by": aggregate_by,
        "source": "stored",
        "units": _units(ifc),
        "matched": matched,
        "aggregated": len(elements),
        "elements_without_quantities": without_quantities,
        "groups": group_rows,
        "totals": {n: round(v, 6) for n, v in sorted(totals.items())},
    }
    if skipped:
        result["skipped"] = skipped
        result["note"] = f"only the first {max_elements} matches were aggregated"
    elif without_quantities:
        result["note"] = (
            "elements without stored quantity sets contribute nothing; "
            "a geometry fallback is not implemented yet"
        )
    return result


def build_georeferencing(ifc: Any) -> dict[str, Any]:
    """Coordinate reference system, map conversion, and north directions."""
    import ifcopenshell.util.geolocation as geo

    def _safe(fn: Any) -> Any:
        try:
            return fn(ifc)
        except Exception:
            return None

    crs = _safe(geo.get_crs)
    crs_info = None
    if crs is not None:
        crs_info = {
            "name": getattr(crs, "Name", None),
            "description": getattr(crs, "Description", None),
            "geodetic_datum": getattr(crs, "GeodeticDatum", None),
            "map_projection": getattr(crs, "MapProjection", None),
            "map_zone": getattr(crs, "MapZone", None),
        }
        unit = getattr(crs, "MapUnit", None)
        if unit is not None:
            crs_info["map_unit"] = getattr(unit, "Name", None)

    helmert = _safe(geo.get_helmert_transformation_parameters)
    conversion = None
    if helmert is not None:
        conversion = {
            "eastings": getattr(helmert, "e", None),
            "northings": getattr(helmert, "n", None),
            "orthogonal_height": getattr(helmert, "h", None),
            "x_axis_abscissa": getattr(helmert, "xaa", None),
            "x_axis_ordinate": getattr(helmert, "xao", None),
            "scale": getattr(helmert, "scale", None),
        }

    return {
        "georeferenced": crs is not None,
        "crs": crs_info,
        "map_conversion": conversion,
        "true_north_degrees": _safe(geo.get_true_north),
        "grid_north_degrees": _safe(geo.get_grid_north),
    }
