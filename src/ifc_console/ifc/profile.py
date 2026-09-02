"""Full per-element measurement probe: parametric profiles plus mesh sections.

Two independent sources feed one answer. The parametric side reads the IFC
representation (extruded profiles carry exact widths, depths and plate
thicknesses in file units). The mesh side slices the tessellated solid across
its long axis and measures the cut. Both are reported, the best value per
dimension is merged into `dimensions`, and disagreement raises a flag instead
of being hidden.
"""

from __future__ import annotations

import copy
import re
import time
from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc import geometry, representation, section
from ifc_console.ifc.elements import _material_info
from ifc_console.ifc.mesh_analysis import (
    mesh_source,
    principal_frame,
    scale_aware_tolerance,
    surface_principal_frame,
    topology_summary,
)
from ifc_console.ifc.similarity import build_geometry_signature
from ifc_console.ifc.units import file_to_si, si_to_file, unit_info

_MAX_SOLIDS = 12
_MAX_DEPTH = 8
STATIONS = (0.3, 0.5, 0.7)
# relative disagreement between parametric and measured before flagging
_MISMATCH = 0.05
ANALYSIS_VERSION = "2.0"
DETAIL_LEVELS = ("compact", "standard", "full")
MEASUREMENT_SETS = ("standard", "profile", "envelope", "fabrication")
FRAME_CHOICES = ("semantic", "placement", "principal", "world")
STATION_STRATEGIES = ("auto", "fixed", "none")
PRECISION_LEVELS = ("standard", "high")
_PRECISION_BUDGETS = {
    "standard": {"stations": 17, "thickness_rays": 2000},
    "high": {"stations": 33, "thickness_rays": 4000},
}
_APPROXIMATE_PROFILE_FLAGS = {
    "analytic_curve_sampled",
    "arcs_approximated",
    "centerline_bounds_approximate",
    "curve_position_unsupported",
    "derived",
    "indexed_segments_approximated",
    "trimmed_curve_approximated",
}

# parameterized profile class -> ((dimension, attribute, multiplier), ...)
_PROFILE_DIMS: dict[str, tuple[tuple[str, str, float], ...]] = {
    "IfcRectangleProfileDef": (("width", "XDim", 1.0), ("height", "YDim", 1.0)),
    "IfcRectangleHollowProfileDef": (
        ("width", "XDim", 1.0),
        ("height", "YDim", 1.0),
        ("wall_thickness", "WallThickness", 1.0),
    ),
    "IfcCircleProfileDef": (("width", "Radius", 2.0), ("height", "Radius", 2.0)),
    "IfcCircleHollowProfileDef": (
        ("width", "Radius", 2.0),
        ("height", "Radius", 2.0),
        ("wall_thickness", "WallThickness", 1.0),
    ),
    "IfcIShapeProfileDef": (
        ("width", "OverallWidth", 1.0),
        ("height", "OverallDepth", 1.0),
        ("web_thickness", "WebThickness", 1.0),
        ("flange_thickness", "FlangeThickness", 1.0),
    ),
    "IfcAsymmetricIShapeProfileDef": (
        ("width", "BottomFlangeWidth", 1.0),
        ("height", "OverallDepth", 1.0),
        ("web_thickness", "WebThickness", 1.0),
        ("flange_thickness", "BottomFlangeThickness", 1.0),
    ),
    "IfcUShapeProfileDef": (
        ("width", "FlangeWidth", 1.0),
        ("height", "Depth", 1.0),
        ("web_thickness", "WebThickness", 1.0),
        ("flange_thickness", "FlangeThickness", 1.0),
    ),
    "IfcZShapeProfileDef": (
        ("width", "FlangeWidth", 1.0),
        ("height", "Depth", 1.0),
        ("web_thickness", "WebThickness", 1.0),
        ("flange_thickness", "FlangeThickness", 1.0),
    ),
    "IfcTShapeProfileDef": (
        ("width", "FlangeWidth", 1.0),
        ("height", "Depth", 1.0),
        ("web_thickness", "WebThickness", 1.0),
        ("flange_thickness", "FlangeThickness", 1.0),
    ),
    "IfcLShapeProfileDef": (
        ("width", "Width", 1.0),
        ("height", "Depth", 1.0),
        ("wall_thickness", "Thickness", 1.0),
    ),
    "IfcCShapeProfileDef": (
        ("width", "Width", 1.0),
        ("height", "Depth", 1.0),
        ("wall_thickness", "WallThickness", 1.0),
    ),
    "IfcCenterLineProfileDef": (("wall_thickness", "Thickness", 1.0),),
}

_SKIP_PROFILE_ATTRS = {"id", "type", "ProfileType", "ProfileName", "Position", "Curve"}

# classes whose width/height parameters are the profile's bounding box, so a
# measured section may be compared against them; a Z or U profile's
# FlangeWidth is one flange, not the overall envelope
_BBOX_TRUE = frozenset(
    {
        "IfcRectangleProfileDef",
        "IfcRectangleHollowProfileDef",
        "IfcCircleProfileDef",
        "IfcCircleHollowProfileDef",
        "IfcIShapeProfileDef",
        "IfcAsymmetricIShapeProfileDef",
    }
)


def _describe(element: Any) -> dict[str, Any]:
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
    }


def _curve_points(curve: Any, flags: list[str], depth: int = 0) -> np.ndarray | None:
    """2D points of a profile curve in file units, or None when unsupported."""
    if curve is None or depth > _MAX_DEPTH:
        return None
    cls = curve.is_a()
    if cls == "IfcPolyline":
        pts = [tuple(p.Coordinates[:2]) for p in curve.Points or ()]
        return np.asarray(pts, dtype=np.float64) if len(pts) >= 2 else None
    if cls == "IfcIndexedPolyCurve":
        coords = getattr(curve.Points, "CoordList", None)
        if not coords:
            return None
        segments = curve.Segments or ()
        if segments:
            flags.append("indexed_segments_approximated")
        if any(seg.is_a("IfcArcIndex") for seg in segments):
            flags.append("arcs_approximated")
        return np.asarray([tuple(c[:2]) for c in coords], dtype=np.float64)
    if cls in {"IfcCircle", "IfcEllipse"}:
        radius_x = getattr(curve, "Radius", None) or getattr(curve, "SemiAxis1", None)
        radius_y = getattr(curve, "Radius", None) or getattr(curve, "SemiAxis2", None)
        if not isinstance(radius_x, (int, float)) or not isinstance(radius_y, (int, float)):
            return None
        angles = np.linspace(0.0, 2.0 * np.pi, 97)
        points = np.stack([radius_x * np.cos(angles), radius_y * np.sin(angles)], axis=1)
        position = getattr(curve, "Position", None)
        if position is not None:
            try:
                import ifcopenshell.util.placement as placement_util

                matrix = np.asarray(placement_util.get_axis2placement(position), dtype=np.float64)
                homogeneous = np.column_stack([points, np.zeros(len(points)), np.ones(len(points))])
                points = (homogeneous @ matrix.T)[:, :2]
            except Exception:
                flags.append("curve_position_unsupported")
        flags.append("analytic_curve_sampled")
        return points
    if cls == "IfcTrimmedCurve":
        points = _curve_points(getattr(curve, "BasisCurve", None), flags, depth + 1)
        if points is not None:
            flags.append("trimmed_curve_approximated")
        return points
    if cls == "IfcCompositeCurve":
        parts = []
        for seg in curve.Segments or ():
            pts = _curve_points(getattr(seg, "ParentCurve", None), flags, depth + 1)
            if pts is None:
                flags.append("curve_unsupported")
                return None
            parts.append(pts)
        return np.concatenate(parts) if parts else None
    flags.append("curve_unsupported")
    return None


def _ring_segments(points: np.ndarray, closed: bool) -> np.ndarray:
    """Segments (n, 2, 2) along a point chain, closing the loop when asked."""
    if len(points) >= 2 and np.allclose(points[0], points[-1]):
        points = points[:-1]
    nxt = np.roll(points, -1, axis=0)
    segs = np.stack([points, nxt], axis=1)
    return segs if closed else segs[:-1]


def _polygon_area(points: np.ndarray) -> float:
    x, y = points[:, 0], points[:, 1]
    return abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))) / 2.0


def _curve_metrics(
    outer: np.ndarray, inners: list[np.ndarray], factor: float, closed: bool
) -> dict[str, Any]:
    """Bounds, area, perimeter and wall thickness of a profile outline.

    Points are file units; the thickness estimator works in SI, so the
    outline is scaled before sampling and the answer scaled back.
    """
    low = outer.min(axis=0)
    high = outer.max(axis=0)
    segs = [_ring_segments(outer, closed)]
    area = _polygon_area(outer) if closed else None
    perimeter = float(np.linalg.norm(segs[0][:, 1] - segs[0][:, 0], axis=1).sum())
    for inner in inners:
        ring = _ring_segments(inner, True)
        segs.append(ring)
        perimeter += float(np.linalg.norm(ring[:, 1] - ring[:, 0], axis=1).sum())
        if area is not None:
            area -= _polygon_area(inner)
    out: dict[str, Any] = {
        "width": round(float(high[0] - low[0]), 6),
        "height": round(float(high[1] - low[1]), 6),
        "perimeter": round(perimeter, 6),
        "area": round(area, 9) if area is not None else None,
        "points": int(sum(len(s) for s in segs)),
    }
    samples, weights = section.thickness_samples(
        np.concatenate(segs) * factor,
        tolerance=max(factor * 1e-6, 1e-9),
    )
    if len(samples):
        median = section.weighted_percentile(samples, weights, 50)
        out["thickness_median"] = round(si_to_file(median, factor), 6)
        pair = section.split_thickness(samples, weights)
        if pair:
            out["thickness_pair"] = {
                key: {
                    "value": round(si_to_file(entry["value"], factor), 6),
                    "share": entry["share"],
                }
                for key, entry in pair.items()
            }
    return out


def _profile_record(
    profile: Any,
    factor: float,
    depth: int = 0,
    *,
    reuse: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if reuse is None:
        return _profile_record_uncached(profile, factor, depth, reuse=None)
    entity_id = getattr(profile, "id", lambda: id(profile))()
    key = (entity_id, float(factor), _MAX_DEPTH - depth)
    profiles = reuse.setdefault("profiles", {})
    cached = profiles.get(key)
    if cached is not None:
        reuse["profile_hits"] = reuse.get("profile_hits", 0) + 1
        return copy.deepcopy(cached)
    reuse["profile_misses"] = reuse.get("profile_misses", 0) + 1
    record = _profile_record_uncached(profile, factor, depth, reuse=reuse)
    profiles[key] = copy.deepcopy(record)
    return record


def _profile_record_uncached(
    profile: Any,
    factor: float,
    depth: int,
    *,
    reuse: dict[str, Any] | None,
) -> dict[str, Any]:
    flags: list[str] = []
    record: dict[str, Any] = {
        "class": profile.is_a(),
        "family": representation.profile_family(profile),
        "name": getattr(profile, "ProfileName", None),
    }
    if depth > _MAX_DEPTH:
        return record
    if profile.is_a("IfcDerivedProfileDef"):
        flags.append("derived")
        parent = _profile_record(profile.ParentProfile, factor, depth + 1, reuse=reuse)
        record["parent"] = parent
        record["flags"] = flags + parent.pop("flags", [])
        return record
    if profile.is_a("IfcCompositeProfileDef"):
        flags.append("composite")
        record["profiles"] = [
            _profile_record(p, factor, depth + 1, reuse=reuse) for p in (profile.Profiles or ())[:6]
        ]
        record["flags"] = flags
        return record
    if profile.is_a("IfcParameterizedProfileDef"):
        params = {}
        for key, value in profile.get_info().items():
            if key in _SKIP_PROFILE_ATTRS or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                params[key] = round(float(value), 6)
        record["parameters"] = params
    elif profile.is_a("IfcCenterLineProfileDef"):
        thickness = getattr(profile, "Thickness", None)
        points = _curve_points(getattr(profile, "Curve", None), flags)
        record["parameters"] = {"Thickness": thickness}
        if points is not None and len(points) >= 2:
            low, high = points.min(axis=0), points.max(axis=0)
            pad = float(thickness or 0.0)
            record["curve"] = {
                "width": round(float(high[0] - low[0]) + pad, 6),
                "height": round(float(high[1] - low[1]) + pad, 6),
                "points": int(len(points)),
            }
            flags.append("centerline_bounds_approximate")
    elif profile.is_a("IfcArbitraryClosedProfileDef"):
        outer = _curve_points(profile.OuterCurve, flags)
        inners = []
        if profile.is_a("IfcArbitraryProfileDefWithVoids"):
            for inner_curve in profile.InnerCurves or ():
                inner = _curve_points(inner_curve, flags)
                if inner is not None and len(inner) >= 3:
                    inners.append(inner)
        if outer is not None and len(outer) >= 3:
            record["curve"] = _curve_metrics(outer, inners, factor, closed=True)
    else:
        flags.append("profile_unsupported")
    if flags:
        record["flags"] = flags
    return record


def _collect_solids(items: Any, out: list[Any], depth: int = 0) -> None:
    if depth > _MAX_DEPTH or len(out) >= _MAX_SOLIDS:
        return
    for item in items or ():
        if len(out) >= _MAX_SOLIDS:
            return
        try:
            if item.is_a("IfcSweptAreaSolid"):
                out.append(item)
            elif item.is_a("IfcMappedItem"):
                mapped = item.MappingSource.MappedRepresentation
                _collect_solids(mapped.Items, out, depth + 1)
            elif item.is_a("IfcBooleanResult"):
                _collect_solids([item.FirstOperand], out, depth + 1)
            elif item.is_a("IfcCsgSolid"):
                _collect_solids([item.TreeRootExpression], out, depth + 1)
        except Exception:
            continue


def _body_solids(element: Any) -> list[Any]:
    shape = getattr(element, "Representation", None)
    if shape is None:
        return []
    reps = list(getattr(shape, "Representations", None) or ())
    body = [
        r for r in reps if (getattr(r, "RepresentationIdentifier", None) or "").lower() == "body"
    ]
    out: list[Any] = []
    for rep in body or reps:
        _collect_solids(getattr(rep, "Items", None), out)
    return out


def _swept_records(
    element: Any, factor: float, *, reuse: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    records = []
    for source in representation.solid_sources(element, factor=factor):
        solid = source.entity
        scales = source.scale
        uniform_scale = source.uniform_scale
        if reuse is None:
            record = _intrinsic_swept_record(solid, factor, reuse=None)
        else:
            entity_id = solid.id() if hasattr(solid, "id") else id(solid)
            key = (entity_id, float(factor))
            solids = reuse.setdefault("solids", {})
            cached = solids.get(key)
            if cached is None:
                reuse["solid_misses"] = reuse.get("solid_misses", 0) + 1
                cached = _intrinsic_swept_record(solid, factor, reuse=reuse)
                solids[key] = copy.deepcopy(cached)
            else:
                reuse["solid_hits"] = reuse.get("solid_hits", 0) + 1
            record = copy.deepcopy(cached)
        record.update(
            {
                "transform": {
                    "scales": [round(value, 9) for value in scales],
                    "uniform_scale": round(uniform_scale, 9) if uniform_scale is not None else None,
                    "nonuniform": uniform_scale is None,
                },
                "provenance": list(source.provenance),
            }
        )
        if source.boolean_role is not None:
            record["boolean_role"] = source.boolean_role
            record["exactness"] = "pre_boolean"
        elif uniform_scale is None:
            record["exactness"] = "unsafe_nonuniform_transform"
        else:
            record["exactness"] = "direct_or_uniform_transform"
        depth = getattr(solid, "Depth", None)
        if isinstance(depth, (int, float)) and uniform_scale is not None:
            record["effective_depth"] = round(float(depth) * uniform_scale, 6)
        direction = getattr(solid, "ExtrudedDirection", None)
        ratios = getattr(direction, "DirectionRatios", None)
        if ratios:
            local = np.asarray(ratios, dtype=np.float64)
            if len(local) == 2:
                local = np.append(local, 0.0)
            try:
                import ifcopenshell.util.placement as placement_util

                position = getattr(solid, "Position", None)
                placement = (
                    np.asarray(placement_util.get_axis2placement(position), dtype=np.float64)
                    if position is not None
                    else np.eye(4)
                )
            except Exception:
                placement = np.eye(4)
            transformed = source.transform[:3, :3] @ placement[:3, :3] @ local
            length = float(np.linalg.norm(transformed))
            if length > 1e-12:
                record["sweep_axis_element"] = [
                    round(float(value / length), 9) for value in transformed
                ]
        records.append(record)
    return records


def _intrinsic_swept_record(
    solid: Any,
    factor: float,
    *,
    reuse: dict[str, Any] | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"class": solid.is_a()}
    profile = getattr(solid, "SweptArea", None)
    if profile is not None:
        record["profile"] = _profile_record(profile, factor, reuse=reuse)
    end_profile = getattr(solid, "EndSweptArea", None)
    if end_profile is not None:
        record["end_profile"] = _profile_record(end_profile, factor, reuse=reuse)
    depth = getattr(solid, "Depth", None)
    if isinstance(depth, (int, float)):
        record["depth"] = round(float(depth), 6)
    direction = getattr(solid, "ExtrudedDirection", None)
    ratios = getattr(direction, "DirectionRatios", None)
    if ratios:
        values = list(ratios)
        if len(values) == 2:
            values.append(0.0)
        record["direction"] = [round(float(value), 6) for value in values]
    return record


def _material_profiles(
    element: Any, factor: float, *, reuse: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    import ifcopenshell.util.element as element_util

    try:
        material = element_util.get_material(element)
    except Exception:
        return []
    if material is None:
        return []
    if material.is_a("IfcMaterialProfileSetUsage"):
        material = material.ForProfileSet
    if not material.is_a("IfcMaterialProfileSet"):
        return []
    records = []
    for item in (material.MaterialProfiles or ())[:6]:
        profile = getattr(item, "Profile", None)
        records.append(
            {
                "material": getattr(getattr(item, "Material", None), "Name", None),
                "profile": _profile_record(profile, factor, reuse=reuse)
                if profile is not None
                else None,
            }
        )
    return records


def _material_layers(element: Any, factor: float) -> list[dict[str, Any]]:
    import ifcopenshell.util.element as element_util

    try:
        material = element_util.get_material(element)
    except Exception:
        return []
    if material is None:
        return []
    usage = material if material.is_a("IfcMaterialLayerSetUsage") else None
    if usage is not None:
        material = usage.ForLayerSet
    if not material.is_a("IfcMaterialLayerSet"):
        return []
    records = []
    for index, layer in enumerate((material.MaterialLayers or ())[:24]):
        thickness = getattr(layer, "LayerThickness", None)
        records.append(
            {
                "index": index,
                "material": getattr(getattr(layer, "Material", None), "Name", None),
                "thickness_file": round(float(thickness), 6)
                if isinstance(thickness, (int, float))
                else None,
                "thickness_si": round(file_to_si(float(thickness), factor), 9)
                if isinstance(thickness, (int, float))
                else None,
                "is_ventilated": getattr(layer, "IsVentilated", None),
            }
        )
    if usage is not None:
        offset = getattr(usage, "OffsetFromReferenceLine", None)
        for record in records:
            record["usage"] = {
                "direction_sense": str(getattr(usage, "DirectionSense", None)),
                "layer_set_direction": str(getattr(usage, "LayerSetDirection", None)),
                "offset_si": round(file_to_si(float(offset), factor), 9)
                if isinstance(offset, (int, float))
                else None,
            }
    return records


def _principal_axis(verts: np.ndarray) -> tuple[np.ndarray, float, np.ndarray]:
    """Longest principal direction, its extent, and projections along it."""
    centered = verts - verts.mean(axis=0)
    _, vecs = np.linalg.eigh(centered.T @ centered)
    axis = vecs[:, -1]
    proj = centered @ axis
    return axis, float(proj.max() - proj.min()), proj


def _sections(
    verts: np.ndarray,
    faces: np.ndarray,
    axis: np.ndarray,
    proj: np.ndarray,
    stations: tuple[float, ...],
    include_outline: bool,
    *,
    tolerance: float = 1e-6,
    max_thickness_rays: int = 2000,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    center = verts.mean(axis=0)
    lo, hi = float(proj.min()), float(proj.max())
    cuts = []
    for at in stations:
        origin = center + axis * (lo + at * (hi - lo))
        metrics = section.section_metrics(
            verts,
            faces,
            origin,
            axis,
            tolerance=tolerance,
            max_thickness_rays=max_thickness_rays,
        )
        if metrics is not None:
            cuts.append((at, origin, metrics))
    if not cuts:
        return [], None
    stations_out = [
        {
            "at": at,
            "width": m["width"],
            "height": m["height"],
            "perimeter": m["perimeter"],
            "area": m["area"],
        }
        for at, _, m in cuts
    ]
    perims = [m["perimeter"] for _, _, m in cuts]
    pick = int(np.argsort(perims)[len(perims) // 2])
    at, origin, best = cuts[pick]
    if include_outline:
        best = (
            section.section_metrics(
                verts,
                faces,
                origin,
                axis,
                include_outline=True,
                tolerance=tolerance,
                max_thickness_rays=max_thickness_rays,
            )
            or best
        )
    return stations_out, {"at": at, **best}


def _dim(si: float | None = None, file_value: float | None = None, *, factor: float, source: str):
    if si is None and file_value is None:
        return None
    if si is None:
        si = file_to_si(float(file_value), factor)
    if file_value is None:
        file_value = si_to_file(float(si), factor)
    return {"si": round(float(si), 6), "file": round(float(file_value), 6), "source": source}


def _profile_evidence_flags(record: dict[str, Any]) -> list[str]:
    flags = list(record.get("flags") or ())
    for child in record.get("profiles") or ():
        flags.extend(_profile_evidence_flags(child))
    if record.get("parent"):
        flags.extend(_profile_evidence_flags(record["parent"]))
    return list(dict.fromkeys(flags))


def _profile_dims(
    record: dict[str, Any],
    factor: float,
    dims: dict[str, Any],
    comparable: set[str],
    *,
    scale: float = 1.0,
    inherited_flags: tuple[str, ...] = (),
) -> None:
    """Fill headline dimensions from one profile record, file units in."""
    cls = record.get("class", "")
    evidence_flags = list(dict.fromkeys([*inherited_flags, *_profile_evidence_flags(record)]))
    approximate = any(flag in _APPROXIMATE_PROFILE_FLAGS for flag in evidence_flags)

    def annotate(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and approximate:
            value["approximate"] = True
            value["flags"] = evidence_flags
        return value

    mapping = _PROFILE_DIMS.get(cls)
    params = record.get("parameters") or {}
    if mapping:
        for name, attr, multiplier in mapping:
            value = params.get(attr)
            if isinstance(value, (int, float)) and name not in dims:
                dims[name] = annotate(
                    _dim(
                        file_value=float(value) * multiplier * scale,
                        factor=factor,
                        source="profile_parameter",
                    )
                )
                if cls in _BBOX_TRUE:
                    comparable.add(name)
    curve = record.get("curve") or {}
    for name in ("width", "height"):
        if name not in dims and isinstance(curve.get(name), (int, float)):
            dims[name] = annotate(
                _dim(
                    file_value=curve[name] * scale,
                    factor=factor,
                    source="profile_curve",
                )
            )
            comparable.add(name)
    if "wall_thickness" not in dims and isinstance(curve.get("thickness_median"), (int, float)):
        dims["wall_thickness"] = annotate(
            _dim(
                file_value=curve["thickness_median"] * scale,
                factor=factor,
                source="profile_curve",
            )
        )
        comparable.add("wall_thickness")
    for child in record.get("profiles") or []:
        _profile_dims(
            child,
            factor,
            dims,
            comparable,
            scale=scale,
            inherited_flags=tuple(evidence_flags),
        )
    if record.get("parent"):
        _profile_dims(
            record["parent"],
            factor,
            dims,
            comparable,
            scale=scale,
            inherited_flags=tuple(evidence_flags),
        )


def _merge_dimensions(
    swept: list[dict[str, Any]],
    material_profiles: list[dict[str, Any]],
    cut: dict[str, Any] | None,
    axis_length_si: float | None,
    factor: float,
    flags: list[str],
) -> dict[str, Any]:
    dims: dict[str, Any] = {}
    comparable: set[str] = set()
    for record in swept:
        if record.get("boolean_role") == "modifier":
            continue
        scale = (record.get("transform") or {}).get("uniform_scale")
        if record.get("profile") and isinstance(scale, (int, float)):
            _profile_dims(record["profile"], factor, dims, comparable, scale=scale)
        elif record.get("profile"):
            flags.append("exact_profile_nonuniform_scale_refused")
    for record in material_profiles:
        if record.get("profile"):
            _profile_dims(record["profile"], factor, dims, comparable)

    depths = [
        r.get("effective_depth", r.get("depth"))
        for r in swept
        if r.get("boolean_role") != "modifier"
        and isinstance(r.get("effective_depth", r.get("depth")), (int, float))
    ]
    # The mesh is cut across the longest axis. That matches the profile plane
    # only when the extrusion runs along that axis (piles, beams, columns);
    # a wall extrudes up but is longest along its run, so the two planes
    # describe different things and must not be compared.
    aligned = True
    if depths and axis_length_si:
        depth_si = file_to_si(max(depths), factor)
        span = max(depth_si, axis_length_si)
        aligned = span > 0 and abs(depth_si - axis_length_si) <= 0.1 * span
    if depths:
        dims["length"] = _dim(file_value=max(depths), factor=factor, source="extrusion_depth")
    elif axis_length_si is not None:
        dims["length"] = _dim(si=axis_length_si, factor=factor, source="mesh_axis")

    if cut:
        thickness = cut.get("thickness") or {}
        fallback = {
            "width": cut.get("width"),
            "height": cut.get("height"),
            "wall_thickness": thickness.get("median"),
        }
        # a parametric plate thickness of any kind beats the mesh estimate
        thickness_known = any(
            name in dims for name in ("wall_thickness", "flange_thickness", "web_thickness")
        )
        if not aligned:
            if any(name in dims for name in fallback):
                flags.append("profile_plane_differs")
        else:
            for name, si_value in fallback.items():
                if not isinstance(si_value, (int, float)):
                    continue
                have = dims.get(name)
                if have is None:
                    if name == "wall_thickness" and thickness_known:
                        continue
                    dims[name] = _dim(si=si_value, factor=factor, source="mesh_section")
                elif (
                    name in comparable
                    and have["si"] > 0
                    and abs(have["si"] - si_value) / have["si"] > _MISMATCH
                ):
                    flag = f"mismatch:{name}"
                    if flag not in flags:
                        flags.append(flag)
    return dims


def _unit_axis(value: np.ndarray) -> np.ndarray:
    axis = np.asarray(value, dtype=np.float64)
    length = float(np.linalg.norm(axis))
    if length <= 1e-12 or not np.isfinite(length):
        raise ValueError("axis must be finite and non-zero")
    return axis / length


def _axis_record(
    axes: np.ndarray,
    *,
    origin: np.ndarray,
    source: str,
    confidence: str,
    flags: list[str] | None = None,
) -> dict[str, Any]:
    basis = np.asarray(axes, dtype=np.float64)
    return {
        "origin": [round(float(value), 9) for value in origin],
        "longitudinal": [round(float(value), 9) for value in basis[0]],
        "transverse": [round(float(value), 9) for value in basis[1]],
        "vertical": [round(float(value), 9) for value in basis[2]],
        "source": source,
        "confidence": confidence,
        "handedness": "right" if float(np.linalg.det(basis)) > 0 else "left",
        "ambiguity": list(flags or ()),
    }


def _placement_axes(element: Any, factor: float) -> tuple[np.ndarray, np.ndarray, list[str]]:
    rotation = geometry.local_rotation(element)
    flags: list[str] = []
    if rotation is None:
        rotation = np.eye(3)
        flags.append("placement_frame_unavailable_world_used")
    origin = np.zeros(3)
    placement = getattr(element, "ObjectPlacement", None)
    if placement is not None:
        try:
            import ifcopenshell.util.placement as placement_util

            matrix = np.asarray(placement_util.get_local_placement(placement), dtype=np.float64)
            origin = matrix[:3, 3] * factor
        except Exception:
            flags.append("placement_origin_unavailable")
    return rotation.T.copy(), origin, flags


def _orthogonal_from_longitudinal(longitudinal: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    long_axis = _unit_axis(longitudinal)
    ranked = sorted(candidates, key=lambda value: abs(float(np.dot(value, long_axis))))
    transverse = ranked[0] - np.dot(ranked[0], long_axis) * long_axis
    transverse = _unit_axis(transverse)
    vertical = _unit_axis(np.cross(long_axis, transverse))
    return np.stack([long_axis, transverse, vertical])


def _frames(
    element: Any,
    swept: list[dict[str, Any]],
    mesh: tuple[np.ndarray, np.ndarray] | None,
    *,
    factor: float,
) -> dict[str, Any]:
    placement_axes, placement_origin, placement_flags = _placement_axes(element, factor)
    frames: dict[str, Any] = {
        "world": _axis_record(
            np.eye(3), origin=placement_origin, source="world_axes", confidence="high"
        ),
        "placement": _axis_record(
            placement_axes,
            origin=placement_origin,
            source="ifc_local_placement" if not placement_flags else "world_fallback",
            confidence="high" if not placement_flags else "low",
            flags=placement_flags,
        ),
    }
    principal_axes = placement_axes
    principal_flags: list[str] = []
    if mesh is not None:
        try:
            principal_basis, _, principal_flags = surface_principal_frame(*mesh)
            principal_axes = principal_basis.T
            principal_source = "surface_area_weighted_principal_axes"
        except ToolError:
            principal_basis, _, principal_flags = principal_frame(mesh[0])
            principal_axes = principal_basis.T
            principal_source = "vertex_principal_axes_fallback"
            principal_flags.append("surface_principal_frame_unavailable")
    else:
        principal_source = "placement_fallback"
        principal_flags.append("mesh_unavailable")
    frames["principal"] = _axis_record(
        principal_axes,
        origin=placement_origin,
        source=principal_source,
        confidence="high" if not principal_flags else "medium",
        flags=principal_flags,
    )

    cls = element.is_a()
    class_placement = cls in {
        "IfcWall",
        "IfcWallStandardCase",
        "IfcSlab",
        "IfcPlate",
        "IfcRoof",
    }
    sweep = next(
        (
            item
            for item in swept
            if item.get("boolean_role") != "modifier" and item.get("sweep_axis_element")
        ),
        None,
    )
    semantic_flags: list[str] = []
    if class_placement:
        semantic_axes = placement_axes
        semantic_source = "ifc_class_and_placement"
        semantic_confidence = "high" if not placement_flags else "medium"
    elif sweep is not None:
        sweep_element = np.asarray(sweep["sweep_axis_element"], dtype=np.float64)
        sweep_world = placement_axes.T @ sweep_element
        semantic_axes = _orthogonal_from_longitudinal(sweep_world, placement_axes)
        semantic_source = "ifc_sweep_direction_and_placement"
        semantic_confidence = "medium" if sweep.get("exactness") == "pre_boolean" else "high"
        if abs(float(np.dot(semantic_axes[2], placement_axes[2]))) < 0.5:
            semantic_flags.append("semantic_vertical_not_placement_vertical")
    else:
        semantic_axes = principal_axes
        semantic_source = principal_source
        semantic_flags.extend(principal_flags)
        semantic_flags.append("semantic_frame_mesh_fallback")
        semantic_confidence = "medium" if not principal_flags else "low"
    frames["semantic"] = _axis_record(
        semantic_axes,
        origin=placement_origin,
        source=semantic_source,
        confidence=semantic_confidence,
        flags=semantic_flags,
    )
    return frames


def _used_vertices(mesh: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    vertices, faces = mesh
    verts = np.asarray(vertices, dtype=np.float64)
    tris = np.asarray(faces, dtype=np.int64)
    valid = np.all((tris >= 0) & (tris < len(verts)), axis=1)
    valid &= np.all(np.isfinite(verts[np.clip(tris, 0, max(len(verts) - 1, 0))]), axis=(1, 2))
    if np.any(valid):
        return verts[np.unique(tris[valid])]
    return verts[np.all(np.isfinite(verts), axis=1)]


def _semantic_extents(
    mesh: tuple[np.ndarray, np.ndarray], frame_record: dict[str, Any]
) -> dict[str, dict[str, Any]]:
    vertices = _used_vertices(mesh)
    axes = {
        "length": np.asarray(frame_record["longitudinal"], dtype=np.float64),
        "width": np.asarray(frame_record["transverse"], dtype=np.float64),
        "height": np.asarray(frame_record["vertical"], dtype=np.float64),
    }
    result = {}
    for name, axis in axes.items():
        projection = vertices @ axis
        low_index, high_index = int(np.argmin(projection)), int(np.argmax(projection))
        result[name] = {
            "value_si": round(float(projection[high_index] - projection[low_index]), 9),
            "support_points": {
                "min": [round(float(value), 9) for value in vertices[low_index]],
                "max": [round(float(value), 9) for value in vertices[high_index]],
            },
        }
    return result


def _file_value(value_si: float, quantity_kind: str, factor: float) -> float:
    exponent = {"length": 1, "area": 2, "volume": 3, "second_moment": 4}.get(quantity_kind, 0)
    return value_si / (factor**exponent) if exponent else value_si


def _measurement(
    measurement_id: str,
    label: str,
    quantity_kind: str,
    value_si: float,
    *,
    factor: float,
    source: str,
    method: str,
    frame: str | None,
    confidence: str,
    flags: list[str] | None = None,
    direction: str | None = None,
    station: float | None = None,
    component: str = "whole_object",
    uncertainty_si: float | None = None,
    alternatives: list[dict[str, Any]] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    units = {
        "length": "m",
        "area": "m2",
        "volume": "m3",
        "second_moment": "m4",
        "angle": "rad",
        "count": "1",
    }
    result: dict[str, Any] = {
        "id": measurement_id,
        "label": label,
        "quantity_kind": quantity_kind,
        "value_si": round(float(value_si), 9),
        "value_file": round(_file_value(float(value_si), quantity_kind, factor), 6),
        "si_unit": units.get(quantity_kind, "1"),
        "source": source,
        "method": method,
        "frame": frame,
        "direction": direction,
        "station": station,
        "component": component,
        "confidence": confidence,
        "uncertainty_si": uncertainty_si,
        "flags": list(flags or ()),
        "alternatives": list(alternatives or ()),
    }
    if evidence:
        result["evidence"] = evidence
    return result


def _snake_name(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).replace("__", "_").lower()


def _profile_parameters(
    profile: dict[str, Any], *, scale: float = 1.0
) -> list[tuple[str, float, dict[str, Any]]]:
    values = []
    for name, value in (profile.get("parameters") or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(
                (
                    name,
                    float(value),
                    {
                        "profile_class": profile.get("class"),
                        "profile_name": profile.get("name"),
                        "uniform_scale": scale,
                    },
                )
            )
    for child in profile.get("profiles") or ():
        values.extend(_profile_parameters(child, scale=scale))
    if profile.get("parent"):
        values.extend(_profile_parameters(profile["parent"], scale=scale))
    return values


def _measurement_inventory(
    record: dict[str, Any],
    *,
    mesh: tuple[np.ndarray, np.ndarray] | None,
    factor: float,
    selected_frame: str,
    tolerance: dict[str, Any] | None,
    measurement_set: str,
    measurement_ids: tuple[str, ...] | None,
    include_alternatives: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    results: list[dict[str, Any]] = []
    uncertainty = (tolerance or {}).get("absolute_si")
    if mesh is not None:
        extents = _semantic_extents(mesh, record["frames"][selected_frame])
        for name, label, direction in (
            ("length", "Overall length", "longitudinal"),
            ("width", "Overall width", "transverse"),
            ("height", "Overall height", "vertical"),
        ):
            evidence = extents[name]
            results.append(
                _measurement(
                    f"envelope.overall_{name}",
                    label,
                    "length",
                    evidence["value_si"],
                    factor=factor,
                    source="analysis_mesh",
                    method="vertex_support_projection",
                    frame=selected_frame,
                    direction=direction,
                    confidence=record["frames"][selected_frame]["confidence"],
                    uncertainty_si=uncertainty,
                    evidence={"support_points": evidence["support_points"]},
                )
            )
        diagonal = float(np.linalg.norm([extents[name]["value_si"] for name in extents]))
        results.append(
            _measurement(
                "envelope.diagonal",
                "Envelope diagonal",
                "length",
                diagonal,
                factor=factor,
                source="analysis_mesh",
                method="orthogonal_frame_extents",
                frame=selected_frame,
                confidence=record["frames"][selected_frame]["confidence"],
                uncertainty_si=uncertainty,
            )
        )
    legacy = record.get("dimensions") or {}
    mapping = {
        "width": ("profile.overall_width", "Overall profile width"),
        "height": ("profile.overall_height", "Overall profile height"),
        "wall_thickness": ("profile.wall_thickness", "Profile wall thickness"),
        "web_thickness": ("profile.web_thickness", "Profile web thickness"),
        "flange_thickness": ("profile.flange_thickness", "Profile flange thickness"),
        "length": ("longitudinal.axis_length", "Longitudinal axis length"),
    }
    tapered_singular: dict[str, dict[str, Any]] = {}
    profile_ranges = []
    for solid in record.get("swept_solids") or ():
        if (
            solid.get("boolean_role") == "modifier"
            or not solid.get("profile")
            or not solid.get("end_profile")
        ):
            continue
        scale = (solid.get("transform") or {}).get("uniform_scale")
        if not isinstance(scale, (int, float)):
            continue
        start_dims: dict[str, Any] = {}
        end_dims: dict[str, Any] = {}
        _profile_dims(solid["profile"], factor, start_dims, set(), scale=float(scale))
        _profile_dims(solid["end_profile"], factor, end_dims, set(), scale=float(scale))
        pre_boolean = solid.get("exactness") == "pre_boolean"
        for legacy_name, (base_id, label) in mapping.items():
            if legacy_name == "length":
                continue
            start, end = start_dims.get(legacy_name), end_dims.get(legacy_name)
            if not isinstance(start, dict) or not isinstance(end, dict):
                continue
            if not isinstance(start.get("si"), (int, float)) or not isinstance(
                end.get("si"), (int, float)
            ):
                continue
            endpoint_ids = [f"{base_id}.start", f"{base_id}.end"]
            tapered_singular[base_id] = {
                "id": base_id,
                "reason": "tapered_profile_requires_station",
                "station_domain": [0.0, 1.0],
                "endpoint_measurements": endpoint_ids,
            }
            low = min(float(start["si"]), float(end["si"]))
            high = max(float(start["si"]), float(end["si"]))
            profile_ranges.append(
                {
                    "id": base_id,
                    "station_domain": [0.0, 1.0],
                    "start_value_si": float(start["si"]),
                    "end_value_si": float(end["si"]),
                    "minimum_si": low,
                    "maximum_si": high,
                    "source": "tapered_profile_parameters",
                }
            )
            for role, station, value, endpoint_id in (
                ("start", 0.0, start, endpoint_ids[0]),
                ("end", 1.0, end, endpoint_ids[1]),
            ):
                approximate = bool(value.get("approximate"))
                value_flags = list(value.get("flags") or ())
                if approximate:
                    value_flags.append("approximate_profile_curve")
                if pre_boolean:
                    value_flags.append("pre_boolean_parameter")
                approximate_uncertainty = max(
                    float(uncertainty or 0.0),
                    abs(float(value["si"])) * 1e-4,
                    factor * 1e-6,
                )
                results.append(
                    _measurement(
                        endpoint_id,
                        f"{label} at tapered {role}",
                        "length",
                        value["si"],
                        factor=factor,
                        source=(
                            "profile_curve_approximation"
                            if approximate
                            else value.get("source", "profile_parameter")
                        ),
                        method=(
                            "sampled_or_chord_profile_curve"
                            if approximate
                            else "ifc_tapered_profile_endpoint"
                        ),
                        frame=None,
                        station=station,
                        confidence="medium" if approximate or pre_boolean else "high",
                        flags=list(dict.fromkeys(value_flags)),
                        uncertainty_si=(
                            approximate_uncertainty if approximate else None if pre_boolean else 0.0
                        ),
                        evidence={
                            "endpoint_role": role,
                            "station_domain": [0.0, 1.0],
                            "range_si": [low, high],
                        },
                    )
                )
    if profile_ranges:
        record["profile_ranges"] = profile_ranges
    boolean_affected = any(
        solid.get("exactness") == "pre_boolean"
        for solid in record.get("swept_solids") or ()
        if solid.get("boolean_role") != "modifier"
    )
    dominant = (
        ((record.get("section_analysis") or {}).get("representative_sections") or {}).get(
            "dominant"
        )
        or record.get("cross_section")
        or {}
    )
    for legacy_name, (measurement_id, label) in mapping.items():
        value = legacy.get(legacy_name)
        if not isinstance(value, dict) or not isinstance(value.get("si"), (int, float)):
            continue
        source = value.get("source", "unknown")
        if (
            legacy_name == "length"
            and source == "extrusion_depth"
            and record["frames"]["semantic"]["source"] == "ifc_class_and_placement"
        ):
            measurement_id = "representation.extrusion_depth"
            label = "Representation extrusion depth"
        if measurement_id in tapered_singular:
            continue
        represented = source in {"profile_parameter", "profile_curve", "extrusion_depth"}
        approximate = bool(value.get("approximate"))
        exact = represented and not approximate
        flags = list(value.get("flags") or ())
        if approximate:
            flags.append("approximate_profile_curve")
        if represented and boolean_affected:
            flags.append("pre_boolean_parameter")
        alternatives = []
        if include_alternatives:
            mesh_value = None
            if legacy_name in {"width", "height"}:
                mesh_value = dominant.get(legacy_name)
            elif legacy_name == "wall_thickness":
                mesh_value = (dominant.get("thickness") or {}).get("median")
            if isinstance(mesh_value, (int, float)):
                alternative = _measurement(
                    measurement_id,
                    label,
                    "length",
                    mesh_value,
                    factor=factor,
                    source="mesh_section",
                    method="adaptive_section",
                    frame="semantic",
                    confidence="high" if dominant.get("closed") else "low",
                    flags=[] if dominant.get("closed") else ["open_section"],
                    station=dominant.get("at"),
                    uncertainty_si=uncertainty,
                    evidence={"delta_si": round(float(mesh_value) - float(value["si"]), 9)},
                )
                alternatives.append(alternative)
        results.append(
            _measurement(
                measurement_id,
                label,
                "length",
                value["si"],
                factor=factor,
                source="profile_curve_approximation" if approximate else source,
                method=(
                    "sampled_or_chord_profile_curve"
                    if approximate
                    else "ifc_representation"
                    if exact
                    else "mesh_axis_or_section"
                ),
                frame="semantic" if legacy_name == "length" else None,
                direction="longitudinal" if legacy_name == "length" else None,
                confidence=(
                    "medium"
                    if approximate or (boolean_affected and represented)
                    else "high"
                    if exact
                    else "medium"
                ),
                flags=list(dict.fromkeys(flags)),
                uncertainty_si=(
                    max(
                        float(uncertainty or 0.0),
                        abs(float(value["si"])) * 1e-4,
                        factor * 1e-6,
                    )
                    if approximate
                    else None
                    if boolean_affected and represented
                    else 0.0
                    if exact
                    else uncertainty
                ),
                alternatives=alternatives,
            )
        )

    parameter_ids: set[str] = set()
    profile_sources = []
    for solid in record.get("swept_solids") or ():
        if solid.get("boolean_role") == "modifier" or not solid.get("profile"):
            continue
        scale = (solid.get("transform") or {}).get("uniform_scale")
        if isinstance(scale, (int, float)):
            pre_boolean = solid.get("exactness") == "pre_boolean"
            if solid.get("end_profile"):
                profile_sources.extend(
                    [
                        (solid["profile"], float(scale), pre_boolean, "start", 0.0),
                        (solid["end_profile"], float(scale), pre_boolean, "end", 1.0),
                    ]
                )
            else:
                profile_sources.append((solid["profile"], float(scale), pre_boolean, None, None))
    for material_profile in record.get("material_profiles") or ():
        if material_profile.get("profile"):
            profile_sources.append((material_profile["profile"], 1.0, False, None, None))
    for profile_record, scale, pre_boolean, endpoint_role, station in profile_sources:
        profile_flags = _profile_evidence_flags(profile_record)
        approximate = any(flag in _APPROXIMATE_PROFILE_FLAGS for flag in profile_flags)
        for attribute, raw_value, evidence in _profile_parameters(profile_record, scale=scale):
            base_id = f"profile.parameter.{_snake_name(attribute)}"
            measurement_id = f"{base_id}.{endpoint_role}" if endpoint_role else base_id
            if measurement_id in parameter_ids:
                continue
            parameter_ids.add(measurement_id)
            if endpoint_role:
                current = tapered_singular.setdefault(
                    base_id,
                    {
                        "id": base_id,
                        "reason": "tapered_profile_requires_station",
                        "station_domain": [0.0, 1.0],
                        "endpoint_measurements": [],
                    },
                )
                if measurement_id not in current["endpoint_measurements"]:
                    current["endpoint_measurements"].append(measurement_id)
            is_angle = "angle" in attribute.casefold() or "slope" in attribute.casefold()
            value_si = raw_value if is_angle else file_to_si(raw_value * scale, factor)
            value_flags = list(profile_flags)
            if approximate:
                value_flags.append("approximate_profile_definition")
            if pre_boolean:
                value_flags.append("pre_boolean_parameter")
            results.append(
                _measurement(
                    measurement_id,
                    f"Profile parameter {attribute}"
                    + (f" at tapered {endpoint_role}" if endpoint_role else ""),
                    "angle" if is_angle else "length",
                    value_si,
                    factor=factor,
                    source="profile_parameter",
                    method=(
                        "ifc_tapered_profile_endpoint" if endpoint_role else "ifc_representation"
                    ),
                    frame=None,
                    station=station,
                    confidence="medium" if pre_boolean or approximate else "high",
                    flags=list(dict.fromkeys(value_flags)),
                    uncertainty_si=(
                        max(
                            float(uncertainty or 0.0),
                            abs(float(value_si)) * 1e-4,
                            factor * 1e-6,
                        )
                        if approximate
                        else None
                        if pre_boolean
                        else 0.0
                    ),
                    evidence={
                        "ifc_attribute": attribute,
                        "endpoint_role": endpoint_role,
                        "station_domain": [0.0, 1.0] if endpoint_role else None,
                        **evidence,
                    },
                )
            )

    box = record.get("box") or {}
    for key, measurement_id, label, kind in (
        ("volume", "mass.volume", "Mesh volume", "volume"),
        ("surface_area", "mass.surface_area", "Surface area", "area"),
        ("footprint_area", "mass.footprint_area", "Plan footprint area", "area"),
    ):
        value = box.get(key)
        if isinstance(value, (int, float)):
            reliable = key != "volume" or bool(box.get("volume_reliable"))
            results.append(
                _measurement(
                    measurement_id,
                    label,
                    kind,
                    value,
                    factor=factor,
                    source="analysis_mesh",
                    method="triangle_mesh_integration",
                    frame="world" if key == "footprint_area" else None,
                    confidence="high" if reliable else "low",
                    flags=[] if reliable else ["mesh_volume_unreliable"],
                    uncertainty_si=uncertainty,
                )
            )
    if dominant:
        station = dominant.get("at")
        for key, measurement_id, label, kind in (
            ("area", "section.area", "Representative section area", "area"),
            ("perimeter", "section.perimeter", "Representative section perimeter", "length"),
        ):
            value = dominant.get(key)
            if isinstance(value, (int, float)):
                results.append(
                    _measurement(
                        measurement_id,
                        label,
                        kind,
                        value,
                        factor=factor,
                        source="analysis_mesh",
                        method="adaptive_section",
                        frame="semantic",
                        station=station,
                        confidence="high" if dominant.get("closed") else "low",
                        flags=[] if dominant.get("closed") else ["open_section"],
                        uncertainty_si=uncertainty,
                    )
                )
        centroid = dominant.get("centroid_2d")
        if isinstance(centroid, list) and len(centroid) == 2:
            for index, direction in enumerate(("transverse", "vertical")):
                results.append(
                    _measurement(
                        f"section.centroid_{'x' if index == 0 else 'y'}",
                        f"Section centroid {direction} coordinate",
                        "length",
                        centroid[index],
                        factor=factor,
                        source="analysis_mesh",
                        method="closed_section_polygon_integration",
                        frame="semantic",
                        direction=direction,
                        station=station,
                        confidence="high" if dominant.get("closed") else "low",
                        uncertainty_si=uncertainty,
                    )
                )
        moments = dominant.get("second_moments_si4") or {}
        for name, suffix, label in (
            ("i_xx", "x", "Section second moment about x"),
            ("i_yy", "y", "Section second moment about y"),
            ("i_xy", "xy", "Section product of inertia"),
        ):
            value = moments.get(name)
            if isinstance(value, (int, float)):
                results.append(
                    _measurement(
                        f"section.second_moment_{suffix}",
                        label,
                        "second_moment",
                        value,
                        factor=factor,
                        source="analysis_mesh",
                        method="closed_section_polygon_integration",
                        frame="semantic",
                        station=station,
                        confidence="high",
                        uncertainty_si=uncertainty,
                    )
                )
    results.append(
        _measurement(
            "opening.count",
            "Related opening count",
            "count",
            len(record.get("openings") or ()),
            factor=factor,
            source="ifc_relationship",
            method="IfcRelVoidsElement_inventory",
            frame=None,
            confidence="high",
            uncertainty_si=0.0,
        )
    )
    for layer in record.get("material_layers") or ():
        value = layer.get("thickness_si")
        if isinstance(value, (int, float)):
            results.append(
                _measurement(
                    f"material.layer.{layer['index']}.thickness",
                    f"Material layer {layer['index']} thickness",
                    "length",
                    value,
                    factor=factor,
                    source="material_layer_parameter",
                    method="ifc_material_layer_set",
                    frame=None,
                    confidence="high",
                    uncertainty_si=0.0,
                )
            )
    topology = record.get("topology") or {}
    for key, measurement_id, label in (
        ("connected_components", "topology.component_count", "Connected components"),
        ("closed_shells", "topology.closed_shell_count", "Closed shells"),
        ("through_holes", "topology.through_hole_count", "Through holes"),
    ):
        value = topology.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            results.append(
                _measurement(
                    measurement_id,
                    label,
                    "count",
                    value,
                    factor=factor,
                    source="analysis_mesh",
                    method="mesh_topology",
                    frame=None,
                    confidence=topology.get("confidence", "low"),
                    uncertainty_si=0.0,
                )
            )

    prefixes = {
        "standard": (
            "envelope.",
            "mass.",
            "profile.",
            "representation.",
            "longitudinal.",
            "section.",
            "material.",
            "topology.",
            "opening.",
        ),
        "profile": ("profile.", "representation.", "longitudinal.", "section.", "material."),
        "envelope": ("envelope.", "mass.", "topology."),
        "fabrication": (
            "envelope.",
            "mass.",
            "profile.",
            "representation.",
            "longitudinal.",
            "section.",
            "material.",
            "topology.",
            "opening.",
        ),
    }
    requested = list(measurement_ids or ())
    if measurement_ids is not None:
        wanted = set(measurement_ids)
        endpoint_ids = {
            endpoint
            for base_id, evidence in tapered_singular.items()
            if base_id in wanted
            for endpoint in evidence["endpoint_measurements"]
        }
        results = [item for item in results if item["id"] in wanted or item["id"] in endpoint_ids]
    else:
        results = [item for item in results if item["id"].startswith(prefixes[measurement_set])]
        if measurement_set == "standard":
            results = [
                item
                for item in results
                if not item["id"].startswith("profile.parameter.")
                and not item["id"].startswith("section.second_moment_")
                and not item["id"].startswith("section.centroid_")
            ]
    extracted = [item["id"] for item in results]
    ambiguous = list(tapered_singular.values())
    ambiguous_ids = set(tapered_singular)
    unavailable = [
        {"id": measurement_id, "reason": "not_supported_or_not_available_for_element"}
        for measurement_id in requested
        if measurement_id not in extracted and measurement_id not in ambiguous_ids
    ]
    conflicting = []
    for flag in record.get("flags") or ():
        if flag.startswith("mismatch:"):
            legacy_name = flag.split(":", 1)[1]
            measurement_id = mapping.get(legacy_name, (f"profile.{legacy_name}", ""))[0]
            conflicting.append({"id": measurement_id, "reason": "source_values_disagree"})
    coverage = {
        "requested": requested,
        "extracted": extracted,
        "unavailable": unavailable,
        "ambiguous": ambiguous,
        "conflicting": conflicting,
    }
    return results, coverage


def _fixed_sections_analysis(
    verts: np.ndarray,
    faces: np.ndarray,
    axis: np.ndarray,
    stations: tuple[float, ...],
    *,
    include_outline: bool,
    tolerance: float,
    max_thickness_rays: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    centre = verts.mean(axis=0)
    projection = (verts - centre) @ axis
    low, high = float(projection.min()), float(projection.max())
    records = []
    for at in stations:
        origin = centre + axis * (low + at * (high - low))
        metrics = section.section_metrics(
            verts,
            faces,
            origin,
            axis,
            include_outline=include_outline,
            tolerance=tolerance,
            max_thickness_rays=max_thickness_rays,
        )
        if metrics is not None:
            records.append(
                {"at": at, "descriptor": section._section_descriptor(metrics), **metrics}
            )
    if not records:
        return {
            "strategy": "fixed",
            "stations_evaluated": len(stations),
            "stations": [],
            "profile_regions": [],
            "representative_sections": {},
            "variation": "unavailable",
            "absolute_tolerance_si": round(float(tolerance), 12),
            "station_budget": len(stations),
            "thickness_ray_budget": max_thickness_rays,
            "stations_missed": len(stations),
            "flags": ["all_stations_missed_mesh"],
        }, None
    pick = int(np.argsort([item["perimeter"] for item in records])[len(records) // 2])
    dominant = records[pick]
    areas = [item for item in records if isinstance(item.get("area"), (int, float))]
    minimum = min(areas, key=lambda item: item["area"]) if areas else records[0]
    maximum = max(areas, key=lambda item: item["area"]) if areas else records[-1]
    descriptors = [item["descriptor"] for item in records]
    constant = all(
        section._descriptor_delta(left, right, relative_tolerance=0.02) == 0
        for left, right in zip(descriptors, descriptors[1:], strict=False)
    )
    analysis = {
        "strategy": "fixed",
        "stations_evaluated": len(stations),
        "stations": records,
        "profile_regions": [
            {
                "start": 0.0,
                "end": 1.0,
                "representative_station": dominant["at"],
                "sample_count": len(records),
                "descriptor": dominant["descriptor"],
                "thickness_modes": (dominant.get("thickness") or {}).get("modes") or [],
            }
        ],
        "representative_sections": {
            "dominant": dominant,
            "minimum": minimum,
            "maximum": maximum,
            "transitions": [],
        },
        "variation": "constant" if constant else "variable",
        "relative_change_tolerance": 0.02,
        "absolute_tolerance_si": round(float(tolerance), 12),
        "station_budget": len(stations),
        "thickness_ray_budget": max_thickness_rays,
        "seed_stations": len(stations),
        "adaptive_refinements": 0,
        "stations_missed": len(stations) - len(records),
        "axis_extent_si": round(high - low, 9),
        "flags": [],
    }
    return analysis, dominant


def _no_sections_analysis() -> dict[str, Any]:
    return {
        "strategy": "none",
        "stations_evaluated": 0,
        "stations": [],
        "profile_regions": [],
        "representative_sections": {},
        "variation": "not_evaluated",
        "flags": [],
    }


def _shape_record(record: dict[str, Any], detail: str) -> None:
    def bounded_outline(outline: Any, budget: int) -> list[list[list[float]]]:
        if not isinstance(outline, list) or not outline:
            return []
        shaped = []
        per_loop = max(4, budget // len(outline))
        for loop in outline[:8]:
            if not isinstance(loop, list) or not loop:
                continue
            step = max(1, int(np.ceil(len(loop) / per_loop)))
            shaped.append(loop[::step][:per_loop])
        return shaped

    def section_summary(
        item: Any, *, outline_budget: int = 0, compact: bool = False
    ) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        if compact:
            return {key: item.get(key) for key in ("at", "descriptor", "closed") if key in item}
        summary = {
            key: item.get(key)
            for key in (
                "at",
                "width",
                "height",
                "perimeter",
                "area",
                "closed",
                "segments",
                "loop_count",
                "hole_count",
                "material_region_count",
                "effective_tolerance_si",
                "thickness_ray_budget",
                "descriptor",
            )
            if key in item
        }
        for key in (
            "thickness",
            "centroid_2d",
            "centroid_world",
            "second_moments_si4",
            "outline_frame",
            "width_direction",
            "height_direction",
        ):
            if key in item:
                summary[key] = item[key]
        if outline_budget and item.get("outline"):
            summary["outline"] = bounded_outline(item["outline"], outline_budget)
        return summary

    def region_summary(region: dict[str, Any], *, include_modes: bool) -> dict[str, Any]:
        result = {
            key: region.get(key)
            for key in (
                "start",
                "end",
                "representative_station",
                "sample_count",
                "descriptor",
            )
        }
        if include_modes and region.get("thickness_modes"):
            result["thickness_modes"] = list(region["thickness_modes"][:4])
        return result

    selected_frame = record.get("selected_frame", "semantic")
    frames = record.get("frames") or {}
    record["frames"] = {
        name: frames[name]
        for name in dict.fromkeys(("semantic", "placement", selected_frame))
        if name in frames
    }
    if detail != "full":
        for solid in record.get("swept_solids") or ():
            provenance = solid.pop("provenance", None)
            if provenance:
                solid["provenance_steps"] = len(provenance)
        (record.get("tolerance") or {}).pop("components_si", None)
    inventory = record.get("representation_inventory") or {}
    if detail != "full":
        inventory.pop("tree", None)
    analysis = record.get("section_analysis") or {}
    regions = analysis.get("profile_regions") or []
    region_limit = 6
    analysis["profile_regions"] = [
        region_summary(region, include_modes=False) for region in regions[:region_limit]
    ]
    if len(regions) > region_limit:
        analysis["regions_omitted"] = len(regions) - region_limit
    representatives = analysis.get("representative_sections") or {}
    if detail == "compact":
        analysis.pop("stations", None)
        compact = {}
        for name in ("dominant", "minimum", "maximum"):
            shaped = section_summary(representatives.get(name), compact=True)
            if shaped:
                compact[name] = shaped
        if representatives.get("transitions"):
            compact["transition_stations"] = [
                item.get("at") for item in representatives["transitions"][:6]
            ]
        analysis["representative_sections"] = compact
    elif detail == "standard":
        analysis.pop("stations", None)
        analysis["representative_sections"] = {
            **{
                name: shaped
                for name in ("dominant", "minimum", "maximum")
                if (shaped := section_summary(representatives.get(name), compact=True)) is not None
            },
            "transition_stations": [
                item.get("at") for item in (representatives.get("transitions") or [])[:4]
            ],
        }
    else:
        stations = analysis.get("stations") or []
        analysis["stations"] = [
            shaped
            for item in stations[:3]
            if (shaped := section_summary(item, compact=True)) is not None
        ]
        if len(stations) > 3:
            analysis["station_records_omitted"] = len(stations) - 3
        analysis["representative_sections"] = {
            "dominant": section_summary(representatives.get("dominant"), outline_budget=12),
            "minimum": section_summary(representatives.get("minimum"), compact=True),
            "maximum": section_summary(representatives.get("maximum"), compact=True),
            "transition_stations": [
                item.get("at") for item in (representatives.get("transitions") or [])[:3]
            ],
        }
    if record.get("cross_section"):
        cross_section = record["cross_section"]
        record["cross_section"] = {
            key: cross_section.get(key)
            for key in (
                "at",
                "width",
                "height",
                "width_direction",
                "height_direction",
                "perimeter",
                "area",
                "closed",
                "segments",
                "thickness",
                "outline_frame",
            )
            if key in cross_section
        }


def analyze_element(
    ifc: Any,
    element: Any,
    mesh: tuple[np.ndarray, np.ndarray] | None,
    *,
    factor: float,
    stations: tuple[float, ...] = STATIONS,
    include_outline: bool = False,
    detail: str = "standard",
    measurement_set: str = "standard",
    measurement_ids: tuple[str, ...] | None = None,
    frame: str = "semantic",
    station_strategy: str = "auto",
    precision: str = "standard",
    include_alternatives: bool = True,
    include_sections: bool = True,
    tessellation: dict[str, Any] | None = None,
    intrinsic_reuse: dict[str, Any] | None = None,
) -> dict[str, Any]:
    import ifcopenshell.util.element as element_util

    flags: list[str] = []
    record = _describe(element)
    try:
        etype = element_util.get_type(element)
    except Exception:
        etype = None
    if etype is not None and etype != element:
        record["type"] = {"class": etype.is_a(), "name": getattr(etype, "Name", None)}
    try:
        record["material"] = _material_info(element_util.get_material(element))
    except Exception:
        record["material"] = None

    inventory = representation.representation_inventory(
        element,
        factor=factor,
        include_tree=detail == "full",
    )
    record["representation_inventory"] = inventory
    record["geometry_family"] = representation.geometry_family(inventory)
    swept = _swept_records(element, factor, reuse=intrinsic_reuse)
    profiles = _material_profiles(element, factor, reuse=intrinsic_reuse)
    layers = _material_layers(element, factor)
    if swept:
        record["swept_solids"] = swept
    if profiles:
        record["material_profiles"] = profiles
    if layers:
        record["material_layers"] = layers
    if not swept and not profiles:
        flags.append("no_parametric_profile")
    flags.extend(inventory.get("flags") or ())
    openings = []
    for relation in (getattr(element, "HasOpenings", None) or ())[:32]:
        opening = getattr(relation, "RelatedOpeningElement", None)
        if opening is not None:
            openings.append(_describe(opening))
    if openings:
        record["openings"] = openings

    axis_length: float | None = None
    cut: dict[str, Any] | None = None
    frames = _frames(element, swept, mesh, factor=factor)
    record["frames"] = frames
    record["selected_frame"] = frame
    tolerance = None
    precision_budget = _PRECISION_BUDGETS[precision]
    section_analysis = _no_sections_analysis()
    if mesh is not None:
        verts, faces = mesh
        tolerance = scale_aware_tolerance(
            verts,
            file_unit_scale=factor,
            tessellation=tessellation,
            precision=precision,
        )
        record["tolerance"] = tolerance
        probe = geometry.probe_element(element, mesh)
        for key in ("global_id", "class", "name"):
            probe.pop(key, None)
        record["box"] = probe
        if not probe["volume_reliable"]:
            flags.append("mesh_volume_unreliable")
        record["topology"] = topology_summary(
            verts,
            faces,
            tolerance=max(float(tolerance["absolute_si"]), 1e-12),
            backend="builtin",
        )
        axis = np.asarray(frames[frame]["longitudinal"], dtype=np.float64)
        projections = _used_vertices(mesh) @ axis
        axis_length = float(projections.max() - projections.min())
        record["axis"] = {
            "direction": [round(float(c), 6) for c in axis],
            "length_si": round(axis_length, 6),
            "source": frames[frame]["source"],
            "frame": frame,
        }
        adaptive_cut: dict[str, Any] | None = None
        if include_sections and station_strategy == "auto":
            section_analysis = section.adaptive_sections(
                verts,
                faces,
                axis,
                relative_tolerance=max(float(tolerance["relative"]), 0.005),
                absolute_tolerance=max(float(tolerance["absolute_si"]), 1e-12),
                max_stations=precision_budget["stations"],
                max_thickness_rays=precision_budget["thickness_rays"],
                include_outline=include_outline,
            )
            adaptive_cut = section_analysis.get("representative_sections", {}).get("dominant")
        elif include_sections and station_strategy == "fixed":
            section_analysis, adaptive_cut = _fixed_sections_analysis(
                verts,
                faces,
                axis,
                stations,
                include_outline=include_outline,
                tolerance=max(float(tolerance["absolute_si"]), 1e-12),
                max_thickness_rays=precision_budget["thickness_rays"],
            )
        record["section_analysis"] = section_analysis
        legacy_stations: list[dict[str, Any]] = []
        if include_sections:
            legacy_axis, _, legacy_projection = _principal_axis(verts)
            legacy_stations, legacy_cut = _sections(
                verts,
                faces,
                legacy_axis,
                legacy_projection,
                stations if station_strategy == "fixed" else STATIONS,
                include_outline,
                tolerance=max(float(tolerance["absolute_si"]), 1e-12),
                max_thickness_rays=precision_budget["thickness_rays"],
            )
            cut = legacy_cut or adaptive_cut
        if cut is not None:
            record["cross_section"] = cut
            record["section_stations"] = legacy_stations
            record["uniform_along_axis"] = section_analysis.get("variation") == "constant"
            if not cut.get("closed"):
                flags.append("open_section")
        elif include_sections:
            flags.append("no_section")
    else:
        flags.append("no_geometry")
        record["section_analysis"] = section_analysis
        record["topology"] = {
            "classification": "unavailable",
            "confidence": "low",
            "flags": ["no_geometry"],
        }

    record["dimensions"] = _merge_dimensions(swept, profiles, cut, axis_length, factor, flags)
    if any(
        isinstance(value, dict) and value.get("approximate")
        for value in record["dimensions"].values()
    ):
        flags.append("approximate_profile_curve_dimensions")
    if any(solid.get("end_profile") for solid in swept):
        flags.append("tapered_profile_station_dependent")
    record["flags"] = list(dict.fromkeys(flags))
    object_info = _describe(element)
    if record.get("type"):
        object_info["type"] = record["type"]
    record["object"] = object_info
    measurements, coverage = _measurement_inventory(
        record,
        mesh=mesh,
        factor=factor,
        selected_frame=frame,
        tolerance=tolerance,
        measurement_set=measurement_set,
        measurement_ids=measurement_ids,
        include_alternatives=include_alternatives,
    )
    record["measurements"] = measurements
    record["coverage"] = coverage
    record["geometry_signature"] = build_geometry_signature(record)
    record["analysis_evidence"] = {
        "triangles": int(len(mesh[1])) if mesh is not None else 0,
        "vertices": int(len(mesh[0])) if mesh is not None else 0,
        "stations_evaluated": int(section_analysis.get("stations_evaluated", 0)),
        "station_budget": (
            precision_budget["stations"]
            if include_sections and station_strategy == "auto"
            else len(stations)
            if include_sections
            else 0
        ),
        "thickness_ray_budget": precision_budget["thickness_rays"],
        "adaptive_refinements": int(section_analysis.get("adaptive_refinements", 0)),
        "stations_missed": int(section_analysis.get("stations_missed", 0)),
        "sections_skipped": not include_sections or station_strategy == "none",
        "representation_nodes": inventory.get("nodes", 0),
        "representation_node_budget": inventory.get("node_limit", representation.MAX_NODES),
        "representation_nodes_skipped": inventory.get("nodes_skipped", 0),
        "precision": {
            "mode": precision,
            "mesh_profile": "analysis",
            "mesh_reused": True,
            "higher_sampling": precision == "high",
        },
    }
    _shape_record(record, detail)
    return record


def analyze_elements(
    ifc: Any,
    *,
    selector: str | None = None,
    global_ids: list[str] | None = None,
    stations: tuple[float, ...] | None = None,
    include_outline: bool = False,
    physical_only: bool = True,
    max_elements: int = 10,
    detail: str = "standard",
    measurement_set: str = "standard",
    measurement_ids: list[str] | None = None,
    frame: str = "semantic",
    station_strategy: str = "auto",
    precision: str = "standard",
    include_alternatives: bool = True,
    include_sections: bool = True,
) -> dict[str, Any]:
    """Versioned high-level geometry analysis with legacy compatibility fields."""
    analysis_started = time.perf_counter()
    choices = (
        (detail, DETAIL_LEVELS, "detail"),
        (measurement_set, MEASUREMENT_SETS, "measurement_set"),
        (frame, FRAME_CHOICES, "frame"),
        (station_strategy, STATION_STRATEGIES, "station_strategy"),
        (precision, PRECISION_LEVELS, "precision"),
    )
    for value, allowed, name in choices:
        if value not in allowed:
            raise ToolError(
                "INVALID_INPUT",
                f"{name} must be one of {', '.join(allowed)}",
                f"Use {allowed[0]!r} for the recommended default.",
            )
    if measurement_ids is not None and any(
        not isinstance(item, str) or not item.strip() for item in measurement_ids
    ):
        raise ToolError(
            "INVALID_INPUT",
            "measurement_ids must contain non-empty namespaced strings",
            "Use ids such as envelope.overall_height or profile.web_thickness.",
        )
    at = tuple(stations) if stations else STATIONS
    if any(not 0.0 <= s <= 1.0 for s in at):
        raise ToolError(
            "INVALID_INPUT",
            "stations must be fractions between 0 and 1",
            "0.5 cuts mid-element; 0.3 and 0.7 avoid end details.",
        )
    effective_station_strategy = (
        "fixed" if stations is not None and station_strategy == "auto" else station_strategy
    )
    if not include_sections:
        effective_station_strategy = "none"
    elements = geometry.resolve_targets(
        ifc,
        selector=selector,
        global_ids=global_ids,
        physical_only=physical_only,
        max_elements=max_elements,
    )
    units = unit_info(ifc)
    factor = units["to_si_factor"]
    tessellation = geometry.tessellation_evidence("analysis")
    meshes = geometry.element_meshes(ifc, elements, profile="analysis")
    intrinsic_reuse: dict[str, Any] = {
        "profiles": {},
        "profile_hits": 0,
        "profile_misses": 0,
        "solids": {},
        "solid_hits": 0,
        "solid_misses": 0,
    }
    records = []
    for element in elements:
        mesh = meshes.get(element.id())
        record = analyze_element(
            ifc,
            element,
            mesh,
            factor=factor,
            stations=at,
            include_outline=include_outline,
            detail=detail,
            measurement_set=measurement_set,
            measurement_ids=tuple(measurement_ids) if measurement_ids is not None else None,
            frame=frame,
            station_strategy=effective_station_strategy,
            precision=precision,
            include_alternatives=include_alternatives,
            include_sections=include_sections,
            tessellation=tessellation,
            intrinsic_reuse=intrinsic_reuse,
        )
        if mesh is not None:
            record["mesh_source"] = mesh_source(mesh[0], mesh[1], tessellation=tessellation)
        records.append(record)
    triangle_count = sum(
        int(record.get("analysis_evidence", {}).get("triangles", 0)) for record in records
    )
    station_count = sum(
        int(record.get("analysis_evidence", {}).get("stations_evaluated", 0)) for record in records
    )
    mapped_source_ids = {
        source_id
        for record in records
        for source_id in record.get("representation_inventory", {}).get("mapped_source_ids", ())
    }
    mapped_source_occurrences = sum(
        int(record.get("representation_inventory", {}).get("mapped_source_occurrences", 0))
        for record in records
    )
    analysis_elapsed_ms = round((time.perf_counter() - analysis_started) * 1000.0, 3)
    return {
        "analysis_version": ANALYSIS_VERSION,
        "selector": selector,
        "units": {**units, "si_values": "metres", "profile_values": "file units"},
        "tessellation": tessellation,
        "matched": len(elements),
        "returned": len(records),
        "analysis_options": {
            "detail": detail,
            "measurement_set": measurement_set,
            "measurement_ids": list(measurement_ids) if measurement_ids is not None else None,
            "frame": frame,
            "station_strategy": effective_station_strategy,
            "precision": precision,
            "include_alternatives": include_alternatives,
            "include_sections": include_sections,
            "include_outline": include_outline,
        },
        "budgets": {
            "max_elements": max_elements,
            "max_triangles_per_element": tessellation["max_triangles"],
            "max_stations_per_element": (
                _PRECISION_BUDGETS[precision]["stations"]
                if effective_station_strategy == "auto"
                else len(at)
                if effective_station_strategy == "fixed"
                else 0
            ),
            "max_thickness_rays_per_section": _PRECISION_BUDGETS[precision]["thickness_rays"],
            "analysis_mesh_reused_for_precision": True,
            "max_representation_nodes_per_element": representation.MAX_NODES,
        },
        "counts": {
            "elements": len(records),
            "with_mesh": sum(
                record.get("analysis_evidence", {}).get("triangles", 0) > 0 for record in records
            ),
            "triangles": triangle_count,
            "stations_evaluated": station_count,
        },
        "performance": {
            "timing_ms": {"analysis_call": analysis_elapsed_ms},
            "intrinsic_reuse": {
                "unique_mapped_sources": len(mapped_source_ids),
                "mapped_source_occurrences": mapped_source_occurrences,
                "solid_definitions_computed": intrinsic_reuse["solid_misses"],
                "solid_cache_hits": intrinsic_reuse["solid_hits"],
                "profile_definitions_computed": intrinsic_reuse["profile_misses"],
                "profile_cache_hits": intrinsic_reuse["profile_hits"],
            },
            "skipped_work": {
                "sections": sum(
                    bool(record.get("analysis_evidence", {}).get("sections_skipped"))
                    for record in records
                ),
                "representation_nodes": sum(
                    int(record.get("analysis_evidence", {}).get("representation_nodes_skipped", 0))
                    for record in records
                ),
                "mesh_derivations": len(records)
                - sum(
                    record.get("analysis_evidence", {}).get("triangles", 0) > 0
                    for record in records
                ),
            },
        },
        "elements": records,
    }


__all__ = [
    "ANALYSIS_VERSION",
    "DETAIL_LEVELS",
    "FRAME_CHOICES",
    "MEASUREMENT_SETS",
    "PRECISION_LEVELS",
    "STATIONS",
    "STATION_STRATEGIES",
    "analyze_element",
    "analyze_elements",
]
