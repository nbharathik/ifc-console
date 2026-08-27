"""Full per-element measurement probe: parametric profiles plus mesh sections.

Two independent sources feed one answer. The parametric side reads the IFC
representation (extruded profiles carry exact widths, depths and plate
thicknesses in file units). The mesh side slices the tessellated solid across
its long axis and measures the cut. Both are reported, the best value per
dimension is merged into `dimensions`, and disagreement raises a flag instead
of being hidden.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc import geometry, section
from ifc_console.ifc.elements import _material_info
from ifc_console.ifc.mesh_analysis import mesh_source
from ifc_console.ifc.units import file_to_si, si_to_file, unit_info

_MAX_SOLIDS = 12
_MAX_DEPTH = 8
STATIONS = (0.3, 0.5, 0.7)
# relative disagreement between parametric and measured before flagging
_MISMATCH = 0.05

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
        if any(seg.is_a("IfcArcIndex") for seg in curve.Segments or ()):
            flags.append("arcs_approximated")
        return np.asarray([tuple(c[:2]) for c in coords], dtype=np.float64)
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
    samples, weights = section.thickness_samples(np.concatenate(segs) * factor)
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


def _profile_record(profile: Any, factor: float, depth: int = 0) -> dict[str, Any]:
    flags: list[str] = []
    record: dict[str, Any] = {
        "class": profile.is_a(),
        "name": getattr(profile, "ProfileName", None),
    }
    if depth > _MAX_DEPTH:
        return record
    if profile.is_a("IfcDerivedProfileDef"):
        flags.append("derived")
        parent = _profile_record(profile.ParentProfile, factor, depth + 1)
        record["parent"] = parent
        record["flags"] = flags + parent.pop("flags", [])
        return record
    if profile.is_a("IfcCompositeProfileDef"):
        flags.append("composite")
        record["profiles"] = [
            _profile_record(p, factor, depth + 1) for p in (profile.Profiles or ())[:6]
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


def _swept_records(element: Any, factor: float) -> list[dict[str, Any]]:
    records = []
    for solid in _body_solids(element):
        record: dict[str, Any] = {"class": solid.is_a()}
        profile = getattr(solid, "SweptArea", None)
        if profile is not None:
            record["profile"] = _profile_record(profile, factor)
        depth = getattr(solid, "Depth", None)
        if isinstance(depth, (int, float)):
            record["depth"] = round(float(depth), 6)
        direction = getattr(solid, "ExtrudedDirection", None)
        ratios = getattr(direction, "DirectionRatios", None)
        if ratios:
            record["direction"] = [round(float(r), 6) for r in ratios]
        records.append(record)
    return records


def _material_profiles(element: Any, factor: float) -> list[dict[str, Any]]:
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
                "profile": _profile_record(profile, factor) if profile is not None else None,
            }
        )
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
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    center = verts.mean(axis=0)
    lo, hi = float(proj.min()), float(proj.max())
    cuts = []
    for at in stations:
        origin = center + axis * (lo + at * (hi - lo))
        metrics = section.section_metrics(verts, faces, origin, axis)
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
        best = section.section_metrics(verts, faces, origin, axis, include_outline=True) or best
    return stations_out, {"at": at, **best}


def _dim(si: float | None = None, file_value: float | None = None, *, factor: float, source: str):
    if si is None and file_value is None:
        return None
    if si is None:
        si = file_to_si(float(file_value), factor)
    if file_value is None:
        file_value = si_to_file(float(si), factor)
    return {"si": round(float(si), 6), "file": round(float(file_value), 6), "source": source}


def _profile_dims(
    record: dict[str, Any], factor: float, dims: dict[str, Any], comparable: set[str]
) -> None:
    """Fill headline dimensions from one profile record, file units in."""
    cls = record.get("class", "")
    mapping = _PROFILE_DIMS.get(cls)
    params = record.get("parameters") or {}
    if mapping:
        for name, attr, multiplier in mapping:
            value = params.get(attr)
            if isinstance(value, (int, float)) and name not in dims:
                dims[name] = _dim(
                    file_value=float(value) * multiplier,
                    factor=factor,
                    source="profile_parameter",
                )
                if cls in _BBOX_TRUE:
                    comparable.add(name)
    curve = record.get("curve") or {}
    for name in ("width", "height"):
        if name not in dims and isinstance(curve.get(name), (int, float)):
            dims[name] = _dim(file_value=curve[name], factor=factor, source="profile_curve")
            comparable.add(name)
    if "wall_thickness" not in dims and isinstance(curve.get("thickness_median"), (int, float)):
        dims["wall_thickness"] = _dim(
            file_value=curve["thickness_median"], factor=factor, source="profile_curve"
        )
        comparable.add("wall_thickness")
    for child in record.get("profiles") or []:
        _profile_dims(child, factor, dims, comparable)
    if record.get("parent"):
        _profile_dims(record["parent"], factor, dims, comparable)


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
        if record.get("profile"):
            _profile_dims(record["profile"], factor, dims, comparable)
    for record in material_profiles:
        if record.get("profile"):
            _profile_dims(record["profile"], factor, dims, comparable)

    depths = [r["depth"] for r in swept if isinstance(r.get("depth"), (int, float))]
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


def analyze_element(
    ifc: Any,
    element: Any,
    mesh: tuple[np.ndarray, np.ndarray] | None,
    *,
    factor: float,
    stations: tuple[float, ...] = STATIONS,
    include_outline: bool = False,
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

    swept = _swept_records(element, factor)
    profiles = _material_profiles(element, factor)
    if swept:
        record["swept_solids"] = swept
    if profiles:
        record["material_profiles"] = profiles
    if not swept and not profiles:
        flags.append("no_parametric_profile")

    axis_length: float | None = None
    cut: dict[str, Any] | None = None
    if mesh is not None:
        verts, faces = mesh
        probe = geometry.probe_element(element, mesh)
        for key in ("global_id", "class", "name"):
            probe.pop(key, None)
        record["box"] = probe
        if not probe["volume_reliable"]:
            flags.append("mesh_volume_unreliable")
        axis, axis_length, proj = _principal_axis(verts)
        record["axis"] = {
            "direction": [round(float(c), 6) for c in axis],
            "length_si": round(axis_length, 6),
            "source": "principal_component",
        }
        stations_out, cut = _sections(verts, faces, axis, proj, stations, include_outline)
        if cut is not None:
            record["cross_section"] = cut
            record["section_stations"] = stations_out
            widths = [s["width"] for s in stations_out]
            if len(widths) > 1 and max(widths) > 0:
                uniform = (max(widths) - min(widths)) / max(widths) < 0.02
                record["uniform_along_axis"] = uniform
            if not cut.get("closed"):
                flags.append("open_section")
        else:
            flags.append("no_section")
    else:
        flags.append("no_geometry")

    record["dimensions"] = _merge_dimensions(swept, profiles, cut, axis_length, factor, flags)
    record["flags"] = flags
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
) -> dict[str, Any]:
    """The full measurement probe for a small element set."""
    at = tuple(stations) if stations else STATIONS
    if any(not 0.0 <= s <= 1.0 for s in at):
        raise ToolError(
            "INVALID_INPUT",
            "stations must be fractions between 0 and 1",
            "0.5 cuts mid-element; 0.3 and 0.7 avoid end details.",
        )
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
        )
        if mesh is not None:
            record["mesh_source"] = mesh_source(
                mesh[0], mesh[1], tessellation=tessellation
            )
        records.append(record)
    return {
        "selector": selector,
        "units": {**units, "si_values": "metres", "profile_values": "file units"},
        "tessellation": tessellation,
        "matched": len(elements),
        "elements": records,
    }


__all__ = ["STATIONS", "analyze_element", "analyze_elements"]
