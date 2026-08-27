"""Quantity takeoff from stored Qto_* sets, plus georeferencing facts.

Stored quantities are the default: trustworthy numbers straight from the
authoring tool. source="derived" adds a mesh-based fallback for elements that
carry no stored values, marked as such in the result.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from ifc_console.core.results import ToolError
from ifc_console.ifc.info import _units

AGGREGATE_BY = ("class", "type", "storey", "material", "none")
SOURCES = ("stored", "derived")

# Derived quantity names mirror the Qto_*BaseQuantities vocabulary, with the
# power of the length unit each one carries.
_DERIVED_QUANTITIES = {
    "Length": 1,
    "Width": 1,
    "Height": 1,
    "GrossFootprintArea": 2,
    "GrossFloorArea": 2,
    "GrossSideArea": 2,
    "GrossTopArea": 2,
    "GrossSurfaceArea": 2,
    "GrossVolume": 3,
}

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
    return _resolve_material_name(material) or f"({material.is_a()})"


def _resolve_material_name(material: Any, depth: int = 0) -> str | None:
    """A usable group label for any of the material set flavours.

    get_material hands back the usage object for layered elements, whose Name
    is empty; grouping on that collapses every wall into one row.
    """
    if material is None or depth > 3:
        return None
    name = getattr(material, "Name", None)
    if name:
        return name
    for attr in ("ForLayerSet", "ForProfileSet"):
        target = getattr(material, attr, None)
        if target is not None:
            resolved = _resolve_material_name(target, depth + 1)
            if resolved:
                return resolved
    layer_set_name = getattr(material, "LayerSetName", None)
    if layer_set_name:
        return layer_set_name
    for attr in ("MaterialLayers", "MaterialProfiles", "MaterialConstituents"):
        parts = getattr(material, attr, None) or ()
        names = [getattr(getattr(p, "Material", None), "Name", None) for p in parts]
        joined = ", ".join(n for n in names if n)
        if joined:
            return joined
    return None


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


def _is_space(element: Any) -> bool:
    return bool(element.is_a("IfcSpace") or element.is_a("IfcSpatialZone"))


def _derived_values(
    probe: dict[str, Any], factor: float, *, space: bool = False
) -> dict[str, float]:
    """Mesh-derived quantities converted from SI metres to file units.

    Spaces take the Qto_SpaceBaseQuantities names; everything else takes the
    element vocabulary.
    """
    extents = probe["local_extents"]
    if space:
        si_values = {
            "Height": extents["z"],
            "GrossFloorArea": probe["footprint_area"],
            "GrossVolume": probe["volume"],
        }
    else:
        si_values = {
            "Length": extents["x"],
            "Width": extents["y"],
            "Height": extents["z"],
            "GrossFootprintArea": probe["footprint_area"],
            # opposing faces come in pairs, so half the larger pair is the
            # elevation area a takeoff means by GrossSideArea
            "GrossSideArea": max(probe["side_area_x"], probe["side_area_y"]) / 2.0,
            "GrossTopArea": probe["top_area"],
            "GrossSurfaceArea": probe["surface_area"],
            "GrossVolume": probe["volume"],
        }
    scale = factor if factor > 0 else 1.0
    return {
        name: float(value) / (scale ** _DERIVED_QUANTITIES[name])
        for name, value in si_values.items()
    }


def compute_quantities(
    ifc: Any,
    selector: str,
    *,
    aggregate_by: str = "class",
    quantities: tuple[str, ...] | None = None,
    max_elements: int = _MAX_ELEMENTS,
    source: str = "stored",
) -> dict[str, Any]:
    """Sum stored quantity-set values for a selector-defined element set."""
    import ifcopenshell.util.element as element_util
    import ifcopenshell.util.selector as selector_util

    if source not in SOURCES:
        raise ToolError(
            "INVALID_INPUT",
            f"source must be one of {', '.join(SOURCES)}",
            "Use source='derived' to fill missing stored values from geometry.",
        )

    try:
        elements = list(selector_util.filter_elements(ifc, selector))
    except Exception as exc:
        raise ToolError(
            "INVALID_QUERY",
            f"selector failed: {exc}",
            "Use query_elements selector syntax, e.g. `IfcWall` or `IfcSlab, material=concrete`.",
        ) from exc

    matched = len(elements)
    skipped = max(0, matched - max_elements)
    # filter_elements returns a set, so an unsorted slice picks a different
    # subset every session; takeoffs have to be reproducible.
    elements.sort(key=lambda e: e.id())
    elements = elements[:max_elements]

    groups: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[str, int] = defaultdict(int)
    without_quantities = 0
    totals: dict[str, float] = defaultdict(float)
    fallback: list[Any] = []

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
            fallback.append(element)

    derived_elements = 0
    derived_without_geometry = 0
    if source == "derived" and fallback:
        from ifc_console.ifc import geometry
        from ifc_console.ifc.units import unit_info

        factor = unit_info(ifc)["to_si_factor"]
        verdict: dict[str, bool] = {}
        # spaces reach here only when the selector asked for them, and a room
        # takeoff is exactly what they are for; openings and grids stay out
        candidates = [e for e in fallback if not geometry.is_non_measurable(e, verdict)]
        meshes = geometry.element_meshes(ifc, candidates)
        for element in candidates:
            mesh = meshes.get(element.id())
            if mesh is None:
                derived_without_geometry += 1
                continue
            probe = geometry.probe_element(element, mesh)
            key = _group_key(element, aggregate_by)
            derived_elements += 1
            for name, value in _derived_values(probe, factor, space=_is_space(element)).items():
                if quantities and name not in quantities:
                    continue
                groups[key][name] += value
                totals[name] += value
        derived_without_geometry += len(fallback) - len(candidates)

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
        "source": "stored+derived" if derived_elements else "stored",
        "units": _units(ifc),
        "matched": matched,
        "aggregated": len(elements),
        "elements_without_quantities": without_quantities,
        "groups": group_rows,
        "totals": {n: round(v, 6) for n, v in sorted(totals.items())},
    }
    if source == "derived":
        result["derived_elements"] = derived_elements
        result["derived_without_geometry"] = derived_without_geometry
        if derived_elements:
            result["note"] = (
                "elements without stored values received mesh-derived "
                "dimensions, gross areas and GrossVolume (spaces get the "
                "Qto_SpaceBaseQuantities names); get_element_geometry shows "
                "the per-element confidence"
            )
    if skipped:
        result["skipped"] = skipped
        result["note"] = f"only the first {max_elements} matches were aggregated"
    elif without_quantities and source == "stored":
        result["note"] = (
            "elements without stored quantity sets contribute nothing; "
            "pass source='derived' for a mesh-based fallback"
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
