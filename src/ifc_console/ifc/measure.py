"""Per-element measurements by explicit, auditable methods.

Every value is reported twice: in the file's project units (what stored
properties use) and in SI metres (what meshes use). The method is always named
in the result, so a measurement can be traced and reproduced.
"""

from __future__ import annotations

import fnmatch
from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc import geometry
from ifc_console.ifc.units import file_to_si, si_to_file, unit_info

METHODS = ("stored_qto", "layer_sum", "geometry_extent")
AXES = ("local_x", "local_y", "local_z", "world_x", "world_y", "world_z")
# The same probe carries mesh areas, so geometry_extent names them through the
# axis argument rather than growing a second selector.
AREA_AXES = ("surface_area", "top_area", "bottom_area", "side_area_x", "side_area_y")
GEOMETRY_AXES = AXES + AREA_AXES

# Read in the element's own frame; without a placement they come out world aligned.
_FRAME_DEPENDENT = frozenset(
    {"local_x", "local_y", "local_z", "top_area", "bottom_area", "side_area_x", "side_area_y"}
)

# quantity entity class -> power of the length unit
_QUANTITY_POWERS = {
    "IfcQuantityLength": 1,
    "IfcQuantityArea": 2,
    "IfcQuantityVolume": 3,
    "IfcQuantityCount": 0,
}

_SI_LABELS = {0: None, 1: "METRE", 2: "METRE^2", 3: "METRE^3"}


def _unit_label(length_unit: str | None, power: int) -> str | None:
    if power == 0 or length_unit is None:
        return None if power == 0 else length_unit
    return length_unit if power == 1 else f"{length_unit}^{power}"


def _describe(element: Any) -> dict[str, Any]:
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
    }


def _quantity_power(ifc: Any, qset_id: Any, quantity: str) -> int | None:
    """The length-unit power of a named quantity, from its entity class."""
    try:
        entity = ifc.by_id(int(qset_id))
    except Exception:
        return None
    for item in getattr(entity, "Quantities", None) or ():
        if getattr(item, "Name", None) == quantity:
            return _QUANTITY_POWERS.get(item.is_a())
    return None


def _stored_qto(
    ifc: Any, element: Any, *, qto_set: str | None, quantity: str, length_unit: str | None
) -> dict[str, Any]:
    import ifcopenshell.util.element as element_util

    try:
        qtos = element_util.get_psets(element, qtos_only=True)
    except Exception:
        qtos = {}
    for set_name, values in sorted(qtos.items()):
        if qto_set and set_name != qto_set:
            continue
        value = values.get(quantity)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        power = _quantity_power(ifc, values.get("id"), quantity)
        return {
            "value": round(float(value), 6),
            "unit": _unit_label(length_unit, power) if power is not None else None,
            "power": power,
            "inputs": {"qto_set": set_name, "quantity": quantity},
            "flags": [],
        }
    flag = "missing_quantity" if qtos else "no_quantity_sets"
    return {
        "value": None,
        "unit": None,
        "power": None,
        "inputs": {"qto_set": qto_set, "quantity": quantity},
        "flags": [flag],
    }


def _layer_set(element: Any) -> Any:
    import ifcopenshell.util.element as element_util

    try:
        material = element_util.get_material(element)
    except Exception:
        material = None
    if material is None:
        return None
    if material.is_a("IfcMaterialLayerSetUsage"):
        return material.ForLayerSet
    if material.is_a("IfcMaterialLayerSet"):
        return material
    return None


def _layer_included(names: list[str], include: list[str], exclude: list[str]) -> bool:
    def matches(patterns: list[str]) -> bool:
        return any(
            fnmatch.fnmatch(name.casefold(), pattern.casefold())
            for name in names
            for pattern in patterns
        )

    if include and not matches(include):
        return False
    return not (exclude and matches(exclude))


def _layer_sum(
    element: Any,
    *,
    include: list[str],
    exclude: list[str],
    length_unit: str | None,
) -> dict[str, Any]:
    layer_set = _layer_set(element)
    if layer_set is None:
        return {
            "value": None,
            "unit": None,
            "power": 1,
            "inputs": {"layers": []},
            "flags": ["no_layer_set"],
        }
    total = 0.0
    layers = []
    for layer in getattr(layer_set, "MaterialLayers", None) or ():
        thickness = getattr(layer, "LayerThickness", None)
        material_name = getattr(getattr(layer, "Material", None), "Name", None)
        layer_name = getattr(layer, "Name", None)
        names = [n for n in (layer_name, material_name) if n]
        included = _layer_included(names or [""], include, exclude)
        if included and isinstance(thickness, (int, float)):
            total += float(thickness)
        layers.append(
            {
                "name": layer_name,
                "material": material_name,
                "thickness": round(float(thickness), 6)
                if isinstance(thickness, (int, float))
                else None,
                "included": included,
            }
        )
    flags = [] if any(item["included"] for item in layers) else ["all_layers_excluded"]
    return {
        "value": round(total, 6),
        "unit": length_unit,
        "power": 1,
        "inputs": {"layer_set": getattr(layer_set, "LayerSetName", None), "layers": layers},
        "flags": flags,
    }


def _geometry_extent(
    element: Any,
    mesh: tuple[np.ndarray, np.ndarray] | None,
    *,
    axis: str,
    length_unit: str | None,
    factor: float,
) -> dict[str, Any]:
    power = 2 if axis in AREA_AXES else 1
    if mesh is None:
        return {
            "value": None,
            "unit": _unit_label(length_unit, power),
            "power": power,
            "inputs": {"axis": axis},
            "flags": ["no_geometry"],
        }
    probe = geometry.probe_element(element, mesh)
    if axis in AREA_AXES:
        value_si = probe[axis]
    elif axis.startswith("world_"):
        index = "xyz".index(axis[-1])
        low = probe["aabb"]["min"][index]
        high = probe["aabb"]["max"][index]
        value_si = high - low
    else:
        value_si = probe["local_extents"][axis[-1]]
    flags = []
    if axis in _FRAME_DEPENDENT and not probe["placement_aligned"]:
        flags.append("no_placement")
    # the prismatic check judges extents as dimensions; a mesh area is measured
    if axis in AXES and probe["confidence"] != "high":
        flags.append("low_confidence")
    return {
        "value": round(si_to_file(float(value_si), factor, power), 6),
        "unit": _unit_label(length_unit, power),
        "power": power,
        "inputs": {
            "axis": axis,
            "local_extents_si": probe["local_extents"],
            "prismatic_ratio": probe["prismatic_ratio"],
            "confidence": probe["confidence"],
        },
        "flags": flags,
    }


def measure_elements(
    ifc: Any,
    *,
    selector: str | None = None,
    global_ids: list[str] | None = None,
    method: str,
    metric: str | None = None,
    qto_set: str | None = None,
    quantity: str | None = None,
    include_layers: list[str] | None = None,
    exclude_layers: list[str] | None = None,
    axis: str = "local_y",
    physical_only: bool = True,
    max_elements: int = 500,
) -> dict[str, Any]:
    """Measure one metric for a set of elements with one named method."""
    if method not in METHODS:
        raise ToolError(
            "INVALID_INPUT",
            f"method must be one of {', '.join(METHODS)}",
            "Use stored_qto for quantity sets, layer_sum for material layers, "
            "or geometry_extent for mesh dimensions and areas.",
        )
    if method == "stored_qto" and not quantity:
        raise ToolError(
            "INVALID_INPUT",
            "method stored_qto needs a quantity name",
            "Pass quantity='Width' (get_psets shows the stored names).",
        )
    if axis not in GEOMETRY_AXES:
        raise ToolError(
            "INVALID_INPUT",
            f"axis must be one of {', '.join(GEOMETRY_AXES)}",
            "local_y is the thickness axis of a placement-aligned wall; "
            "surface_area and the top/bottom/side buckets report areas.",
        )

    elements = geometry.resolve_targets(
        ifc,
        selector=selector,
        global_ids=global_ids,
        physical_only=physical_only,
        max_elements=max_elements,
    )
    units = unit_info(ifc)
    length_unit = units["length_unit"]
    factor = units["to_si_factor"]

    meshes: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    if method == "geometry_extent":
        meshes = geometry.element_meshes(ifc, elements)

    records = []
    values = []
    for element in elements:
        if method == "stored_qto":
            assert quantity is not None
            measured = _stored_qto(
                ifc, element, qto_set=qto_set, quantity=quantity, length_unit=length_unit
            )
        elif method == "layer_sum":
            measured = _layer_sum(
                element,
                include=include_layers or [],
                exclude=exclude_layers or [],
                length_unit=length_unit,
            )
        else:
            measured = _geometry_extent(
                element,
                meshes.get(element.id()),
                axis=axis,
                length_unit=length_unit,
                factor=factor,
            )
        power = measured.pop("power")
        value = measured["value"]
        record = {**_describe(element), "metric": metric or method, "method": method, **measured}
        if value is not None and power:
            record["value_si"] = round(file_to_si(value, factor, power), 9)
            record["si_unit"] = _SI_LABELS[power]
        else:
            record["value_si"] = value if power == 0 else None
            record["si_unit"] = _SI_LABELS.get(power or 0)
        records.append(record)
        if value is not None:
            values.append(value)

    summary: dict[str, Any] = {
        "count": len(records),
        "measured": len(values),
        "missing": len(records) - len(values),
    }
    if values:
        summary.update(
            {
                "min": round(min(values), 6),
                "max": round(max(values), 6),
                "mean": round(sum(values) / len(values), 6),
            }
        )
    return {
        "metric": metric or method,
        "method": method,
        "units": units,
        "summary": summary,
        "elements": records,
    }


def _pair_gap(lo_a, hi_a, lo_b, hi_b) -> float:
    return float(np.maximum(lo_a - hi_b, lo_b - hi_a).max())


def measure_distance(
    ifc: Any,
    *,
    set_a: str | None = None,
    global_ids_a: list[str] | None = None,
    set_b: str | None = None,
    global_ids_b: list[str] | None = None,
    physical_only: bool = True,
    max_elements: int = 200,
    surface_samples: int = 5000,
) -> dict[str, Any]:
    """Closest approach between two element sets, three ways.

    Reports the closest pair by bounding-box gap, with centroid distance,
    the axis-aligned box gap, and a closest-point surface distance.
    """
    elements_a = geometry.resolve_targets(
        ifc,
        selector=set_a,
        global_ids=global_ids_a,
        physical_only=physical_only,
        max_elements=max_elements,
    )
    elements_b = geometry.resolve_targets(
        ifc,
        selector=set_b,
        global_ids=global_ids_b,
        physical_only=physical_only,
        max_elements=max_elements,
    )
    units = unit_info(ifc)
    factor = units["to_si_factor"]

    meshes = geometry.element_meshes(ifc, list({e.id(): e for e in elements_a + elements_b}.values()))
    ids_a = [e.id() for e in elements_a if e.id() in meshes]
    ids_b = [e.id() for e in elements_b if e.id() in meshes]
    if not ids_a or not ids_b:
        raise ToolError(
            "NO_GEOMETRY",
            "one of the sets has no usable geometry",
            "Distance needs solid geometry; check the sets with query_elements.",
        )
    by_id = {e.id(): e for e in elements_a + elements_b}

    lo_a, hi_a = geometry.mesh_boxes(meshes, ids_a)
    lo_b, hi_b = geometry.mesh_boxes(meshes, ids_b)

    pairs = []
    for i, id_a in enumerate(ids_a):
        for j, id_b in enumerate(ids_b):
            if id_a == id_b:
                continue
            pairs.append((_pair_gap(lo_a[i], hi_a[i], lo_b[j], hi_b[j]), id_a, id_b))
    if not pairs:
        raise ToolError(
            "INVALID_INPUT",
            "the two sets resolve to the same single element",
            "Pick two different elements or sets.",
        )
    pairs.sort(key=lambda item: item[0])
    gap, best_a, best_b = pairs[0]

    verts_a, faces_a = meshes[best_a]
    verts_b, faces_b = meshes[best_b]
    centroid = float(np.linalg.norm(verts_a.mean(axis=0) - verts_b.mean(axis=0)))
    overlapping = gap < 0

    if overlapping:
        surface = 0.0
    else:
        rng = np.random.default_rng(0)

        def sample(verts: np.ndarray) -> np.ndarray:
            if len(verts) <= surface_samples:
                return verts
            return verts[rng.choice(len(verts), surface_samples, replace=False)]

        surface = min(
            geometry.points_to_triangles_distance(sample(verts_a), verts_b[faces_b]),
            geometry.points_to_triangles_distance(sample(verts_b), verts_a[faces_a]),
        )

    def both(si_value: float) -> dict[str, float]:
        return {"si": round(si_value, 6), "file": round(si_to_file(si_value, factor), 6)}

    return {
        "a": _describe(by_id[best_a]),
        "b": _describe(by_id[best_b]),
        "overlapping": overlapping,
        "centroid_distance": both(centroid),
        "aabb_gap": both(max(gap, 0.0)),
        "surface_distance": both(float(surface)),
        "units": units,
        "closest_pairs": [
            {
                "a": by_id[ia].GlobalId,
                "b": by_id[ib].GlobalId,
                "aabb_gap_si": round(max(g, 0.0), 6),
            }
            for g, ia, ib in pairs[:10]
        ],
    }


__all__ = [
    "AREA_AXES",
    "AXES",
    "GEOMETRY_AXES",
    "METHODS",
    "measure_distance",
    "measure_elements",
]
