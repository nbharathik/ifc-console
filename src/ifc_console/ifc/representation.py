"""Bounded, serializable evidence from IFC geometric representations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

MAX_DEPTH = 8
MAX_NODES = 64

_SWEEP_CLASSES = (
    "IfcExtrudedAreaSolid",
    "IfcExtrudedAreaSolidTapered",
    "IfcRevolvedAreaSolid",
    "IfcRevolvedAreaSolidTapered",
    "IfcSweptDiskSolid",
    "IfcSweptDiskSolidPolygonal",
    "IfcFixedReferenceSweptAreaSolid",
    "IfcSurfaceCurveSweptAreaSolid",
)

_PROFILE_FAMILIES = {
    "IfcRectangleProfileDef": "rectangle",
    "IfcRectangleHollowProfileDef": "rectangle_hollow",
    "IfcRoundedRectangleProfileDef": "rounded_rectangle",
    "IfcCircleProfileDef": "circle",
    "IfcCircleHollowProfileDef": "circle_hollow",
    "IfcEllipseProfileDef": "ellipse",
    "IfcIShapeProfileDef": "i_shape",
    "IfcAsymmetricIShapeProfileDef": "i_shape_asymmetric",
    "IfcTShapeProfileDef": "t_shape",
    "IfcUShapeProfileDef": "u_shape",
    "IfcCShapeProfileDef": "c_shape",
    "IfcZShapeProfileDef": "z_shape",
    "IfcLShapeProfileDef": "l_shape",
    "IfcTrapeziumProfileDef": "trapezium",
    "IfcArbitraryClosedProfileDef": "arbitrary_closed",
    "IfcArbitraryProfileDefWithVoids": "arbitrary_with_voids",
    "IfcCenterLineProfileDef": "centerline",
    "IfcCompositeProfileDef": "composite",
    "IfcDerivedProfileDef": "derived",
}


@dataclass(frozen=True)
class SolidSource:
    """An internal IFC solid plus the transforms and modifiers that affect it."""

    entity: Any
    transform: np.ndarray
    provenance: tuple[dict[str, Any], ...]
    boolean_role: str | None = None

    @property
    def scale(self) -> tuple[float, float, float]:
        return _linear_scales(self.transform)

    @property
    def uniform_scale(self) -> float | None:
        values = self.scale
        if max(values) - min(values) <= max(max(values), 1.0) * 1e-6:
            return float(sum(values) / 3.0)
        return None


def profile_family(profile: Any | None) -> str | None:
    if profile is None:
        return None
    cls = profile.is_a()
    if cls == "IfcArbitraryProfileDefWithVoids":
        return "arbitrary_with_voids"
    return _PROFILE_FAMILIES.get(cls, "unsupported")


def _round(value: float, digits: int = 9) -> float:
    return round(float(value), digits)


def _linear_scales(matrix: np.ndarray) -> tuple[float, float, float]:
    singular = np.linalg.svd(np.asarray(matrix, dtype=np.float64)[:3, :3], compute_uv=False)
    return tuple(float(value) for value in sorted(singular, reverse=True))


def _mapped_matrix(item: Any) -> np.ndarray:
    try:
        import ifcopenshell.util.placement as placement_util

        matrix = np.asarray(placement_util.get_mappeditem_transformation(item), dtype=np.float64)
        if matrix.shape == (4, 4) and np.all(np.isfinite(matrix)):
            return matrix
    except Exception:
        pass
    return np.eye(4, dtype=np.float64)


def _transform_summary(matrix: np.ndarray, factor: float) -> dict[str, Any]:
    scales = _linear_scales(matrix)
    uniform = max(scales) - min(scales) <= max(max(scales), 1.0) * 1e-6
    linear = np.asarray(matrix, dtype=np.float64)[:3, :3]
    determinant = float(np.linalg.det(linear))
    result: dict[str, Any] = {
        "translation_si": [_round(value * factor) for value in matrix[:3, 3]],
        "scales": [_round(value) for value in scales],
        "uniform_scale": _round(sum(scales) / 3.0) if uniform else None,
        "nonuniform": not uniform,
        "mirrored": determinant < 0.0,
    }
    return result


def _direction(direction: Any | None) -> list[float] | None:
    ratios = getattr(direction, "DirectionRatios", None)
    if not ratios:
        return None
    value = np.asarray(ratios, dtype=np.float64)
    if len(value) == 2:
        value = np.append(value, 0.0)
    length = float(np.linalg.norm(value))
    if value.shape != (3,) or not np.isfinite(length) or length <= 1e-12:
        return None
    return [_round(component / length) for component in value]


def _curve_summary(curve: Any | None) -> dict[str, Any] | None:
    if curve is None:
        return None
    cls = curve.is_a()
    result: dict[str, Any] = {"class": cls}
    if cls == "IfcPolyline":
        result["points"] = len(curve.Points or ())
    elif cls == "IfcIndexedPolyCurve":
        coords = getattr(curve.Points, "CoordList", None) or ()
        segments = curve.Segments or ()
        result.update(
            points=len(coords),
            segments=len(segments),
            has_arcs=any(segment.is_a("IfcArcIndex") for segment in segments),
        )
    elif cls in {"IfcCircle", "IfcEllipse"}:
        for name in ("Radius", "SemiAxis1", "SemiAxis2"):
            value = getattr(curve, name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result[name] = _round(value, 6)
    elif cls == "IfcCompositeCurve":
        result["segments"] = len(curve.Segments or ())
    elif cls == "IfcTrimmedCurve":
        result["basis_curve"] = getattr(curve.BasisCurve, "is_a", lambda: None)()
    else:
        result["supported"] = False
    return result


def _profile_summary(profile: Any | None) -> dict[str, Any] | None:
    if profile is None:
        return None
    result: dict[str, Any] = {
        "class": profile.is_a(),
        "family": profile_family(profile),
        "name": getattr(profile, "ProfileName", None),
    }
    if profile.is_a("IfcParameterizedProfileDef"):
        result["parameter_names"] = sorted(
            key
            for key, value in profile.get_info().items()
            if isinstance(value, (int, float))
            and not isinstance(value, bool)
            and key not in {"id", "type"}
        )
    curve = getattr(profile, "OuterCurve", None) or getattr(profile, "Curve", None)
    if curve is not None:
        result["curve"] = _curve_summary(curve)
    if profile.is_a("IfcArbitraryProfileDefWithVoids"):
        result["inner_curves"] = len(profile.InnerCurves or ())
    if profile.is_a("IfcCompositeProfileDef"):
        result["profiles"] = [_profile_summary(child) for child in (profile.Profiles or ())[:6]]
    if profile.is_a("IfcDerivedProfileDef"):
        result["parent"] = _profile_summary(profile.ParentProfile)
    return result


def _solid_summary(solid: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"class": solid.is_a()}
    profile = getattr(solid, "SweptArea", None)
    if profile is not None:
        result["profile"] = _profile_summary(profile)
    for name in ("Depth", "Radius", "InnerRadius", "StartParam", "EndParam"):
        value = getattr(solid, name, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[name] = _round(value, 6)
    extrusion = _direction(getattr(solid, "ExtrudedDirection", None))
    if extrusion is not None:
        result["extruded_direction"] = extrusion
    axis = getattr(getattr(solid, "Axis", None), "Axis", None)
    revolution = _direction(axis)
    if revolution is not None:
        result["revolution_axis"] = revolution
    directrix = getattr(solid, "Directrix", None)
    if directrix is not None:
        result["directrix"] = _curve_summary(directrix)
    end_profile = getattr(solid, "EndSweptArea", None)
    if end_profile is not None:
        result["end_profile"] = _profile_summary(end_profile)
    return result


def _representation_roots(element: Any) -> tuple[list[tuple[str, Any]], bool]:
    roots: list[tuple[str, Any]] = []
    shape = getattr(element, "Representation", None)
    for index, representation in enumerate(getattr(shape, "Representations", None) or ()):
        identifier = getattr(representation, "RepresentationIdentifier", None) or f"rep_{index}"
        roots.append((str(identifier), representation))
    if roots:
        return roots, False
    try:
        import ifcopenshell.util.element as element_util

        element_type = element_util.get_type(element)
    except Exception:
        element_type = None
    for index, rep_map in enumerate(getattr(element_type, "RepresentationMaps", None) or ()):
        roots.append((f"type_map_{index}", rep_map.MappedRepresentation))
    return roots, bool(roots)


def _walk(
    item: Any,
    *,
    transform: np.ndarray,
    provenance: tuple[dict[str, Any], ...],
    boolean_role: str | None,
    factor: float,
    depth: int,
    budget: dict[str, int],
    solids: list[SolidSource],
    flags: list[str],
) -> dict[str, Any] | None:
    if depth > MAX_DEPTH:
        budget["skipped"] += 1
        flags.append("representation_depth_capped")
        return None
    if budget["used"] >= budget["limit"]:
        budget["skipped"] += 1
        flags.append("representation_count_capped")
        return None
    budget["used"] += 1
    cls = item.is_a()
    node: dict[str, Any] = {"class": cls}

    if any(item.is_a(name) for name in _SWEEP_CLASSES):
        solids.append(SolidSource(item, transform.copy(), provenance, boolean_role))
        node.update(_solid_summary(item))
        if boolean_role is not None:
            node["boolean_role"] = boolean_role
        return node

    if item.is_a("IfcMappedItem"):
        local = _mapped_matrix(item)
        summary = _transform_summary(local, factor)
        node["transform"] = summary
        node["mapping_source_id"] = getattr(item.MappingSource, "id", lambda: None)()
        if summary["nonuniform"]:
            flags.append("mapped_nonuniform_scale_unsupported")
        mapped = item.MappingSource.MappedRepresentation
        children = []
        mapped_provenance = provenance + (
            {
                "kind": "mapped_item",
                "source_id": getattr(item.MappingSource, "id", lambda: None)(),
                "transform": summary,
            },
        )
        for child in getattr(mapped, "Items", None) or ():
            child_node = _walk(
                child,
                transform=transform @ local,
                provenance=mapped_provenance,
                boolean_role=boolean_role,
                factor=factor,
                depth=depth + 1,
                budget=budget,
                solids=solids,
                flags=flags,
            )
            if child_node is not None:
                children.append(child_node)
        node["children"] = children
        return node

    if item.is_a("IfcBooleanResult"):
        operator = str(getattr(item, "Operator", "UNKNOWN"))
        node["operator"] = operator
        children = []
        for role, operand in (
            ("base", item.FirstOperand),
            ("modifier", item.SecondOperand),
        ):
            context = provenance + ({"kind": "boolean", "operator": operator, "role": role},)
            child_node = _walk(
                operand,
                transform=transform,
                provenance=context,
                boolean_role=role,
                factor=factor,
                depth=depth + 1,
                budget=budget,
                solids=solids,
                flags=flags,
            )
            if child_node is not None:
                children.append(child_node)
        node["children"] = children
        flags.append("boolean_modified_geometry")
        return node

    if item.is_a("IfcCsgSolid"):
        child = _walk(
            item.TreeRootExpression,
            transform=transform,
            provenance=provenance + ({"kind": "csg_tree"},),
            boolean_role=boolean_role,
            factor=factor,
            depth=depth + 1,
            budget=budget,
            solids=solids,
            flags=flags,
        )
        node["children"] = [child] if child is not None else []
        flags.append("csg_mesh_fallback")
        return node

    if item.is_a("IfcGeometricSet") or item.is_a("IfcGeometricCurveSet"):
        children = []
        for child in getattr(item, "Elements", None) or ():
            child_node = _walk(
                child,
                transform=transform,
                provenance=provenance,
                boolean_role=boolean_role,
                factor=factor,
                depth=depth + 1,
                budget=budget,
                solids=solids,
                flags=flags,
            )
            if child_node is not None:
                children.append(child_node)
        node["children"] = children
        return node

    if item.is_a("IfcManifoldSolidBrep") or item.is_a("IfcShellBasedSurfaceModel"):
        node["analysis"] = "mesh_fallback"
        flags.append("brep_or_surface_mesh_fallback")
        return node

    node["supported"] = False
    flags.append(f"unsupported_representation:{cls}")
    return node


def _collect(
    element: Any, factor: float, max_nodes: int
) -> tuple[dict[str, Any], list[SolidSource]]:
    roots, from_type = _representation_roots(element)
    flags: list[str] = []
    solids: list[SolidSource] = []
    budget = {"used": 0, "limit": max_nodes, "skipped": 0}
    trees = []
    representation_types: set[str] = set()
    for identifier, representation in roots:
        representation_types.add(str(getattr(representation, "RepresentationType", None) or ""))
        children = []
        for item in getattr(representation, "Items", None) or ():
            child = _walk(
                item,
                transform=np.eye(4, dtype=np.float64),
                provenance=({"kind": "representation", "identifier": identifier},),
                boolean_role=None,
                factor=factor,
                depth=0,
                budget=budget,
                solids=solids,
                flags=flags,
            )
            if child is not None:
                children.append(child)
        trees.append(
            {
                "identifier": identifier,
                "representation_type": getattr(representation, "RepresentationType", None),
                "items": children,
            }
        )
    classes: dict[str, int] = {}
    mapped_sources: set[int] = set()
    mapped_source_occurrences = 0
    unsupported_nodes = 0
    for tree in trees:
        stack = list(tree["items"])
        while stack:
            node = stack.pop()
            cls = node["class"]
            classes[cls] = classes.get(cls, 0) + 1
            source_id = node.get("mapping_source_id")
            if isinstance(source_id, int):
                mapped_sources.add(source_id)
                mapped_source_occurrences += 1
            if node.get("supported") is False:
                unsupported_nodes += 1
            stack.extend(node.get("children") or ())
    inventory = {
        "source": "type_representation_map" if from_type else "occurrence_representation",
        "representations": len(roots),
        "nodes": budget["used"],
        "nodes_skipped": budget["skipped"],
        "node_limit": max_nodes,
        "unique_mapped_sources": len(mapped_sources),
        "mapped_source_ids": sorted(mapped_sources),
        "mapped_source_occurrences": mapped_source_occurrences,
        "unsupported_nodes": unsupported_nodes,
        "representation_types": sorted(value for value in representation_types if value),
        "classes": dict(sorted(classes.items())),
        "tree": trees,
        "flags": list(dict.fromkeys(flags)),
    }
    return inventory, solids


def representation_inventory(
    element: Any,
    *,
    factor: float = 1.0,
    max_nodes: int = MAX_NODES,
    include_tree: bool = True,
) -> dict[str, Any]:
    """Return a bounded representation tree without raw IFC entities."""
    if max_nodes < 1:
        raise ValueError("max_nodes must be positive")
    inventory, _ = _collect(element, factor, max_nodes)
    if not include_tree:
        inventory.pop("tree", None)
    return inventory


def solid_sources(element: Any, *, factor: float = 1.0) -> list[SolidSource]:
    """Return internal solid sources for exact measurement extraction."""
    _, solids = _collect(element, factor, MAX_NODES)
    return solids


def geometry_family(inventory: dict[str, Any]) -> str:
    classes = set(inventory.get("classes") or {})
    if "IfcExtrudedAreaSolidTapered" in classes:
        return "tapered_profile_extrusion"
    if "IfcExtrudedAreaSolid" in classes and not any(
        name.startswith("IfcBoolean") for name in classes
    ):
        return "constant_profile_extrusion"
    if any(name.startswith("IfcRevolvedAreaSolid") for name in classes):
        return "revolved_profile"
    if any(name.startswith("IfcSweptDiskSolid") for name in classes):
        return "swept_disk"
    if any("SweptAreaSolid" in name for name in classes):
        return "directrix_sweep"
    if any(name.startswith("IfcBoolean") or name == "IfcCsgSolid" for name in classes):
        return "csg_or_boolean"
    if any("Brep" in name or "SurfaceModel" in name for name in classes):
        return "surface_or_brep"
    return "mesh_only"


__all__ = [
    "MAX_DEPTH",
    "MAX_NODES",
    "SolidSource",
    "geometry_family",
    "profile_family",
    "representation_inventory",
    "solid_sources",
]
