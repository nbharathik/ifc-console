"""Shared triangle-mesh machinery and the per-element geometry probe.

The stock ifcopenshell wheel has no OpenCASCADE, so like clash detection every
derived number here comes from world-space triangle meshes. Mesh coordinates
from the iterator are SI metres regardless of the file's length unit.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from ifc_console.core.results import ToolError

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

# Triangles per element above this are dropped: an element that dense is a
# rendering mesh, not something anyone measures or clashes by hand.
MAX_TRIANGLES = 20_000
_EPS = 1e-9


def is_non_physical(element: Any, cache: dict[str, bool]) -> bool:
    name = element.is_a()
    hit = cache.get(name)
    if hit is None:
        # is_a(cls) matches subtypes; an exact name test misses IFC4 cases
        # like IfcOpeningStandardCase. Verdicts are cached per class name.
        hit = any(element.is_a(cls) for cls in NON_PHYSICAL)
        cache[name] = hit
    return hit


def selected(ifc: Any, selector: str) -> list[Any]:
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


def resolve_targets(
    ifc: Any,
    *,
    selector: str | None = None,
    global_ids: list[str] | None = None,
    physical_only: bool = True,
    max_elements: int = 1000,
) -> list[Any]:
    """Resolve exactly one of selector or global_ids to elements."""
    if (selector is None) == (not global_ids):
        raise ToolError(
            "INVALID_INPUT",
            "pass exactly one of selector or global_ids",
            "Use selector for sets (`IfcWall, type=X`) or global_ids for "
            "specific elements from search_elements or get_viewer_selection.",
        )
    if global_ids:
        elements = []
        missing = []
        for gid in dict.fromkeys(global_ids):
            try:
                elements.append(ifc.by_guid(gid))
            except Exception:
                missing.append(gid)
        if missing:
            raise ToolError(
                "NOT_FOUND",
                f"no element with GlobalId {', '.join(missing[:5])}",
                "Confirm the ids with search_elements or get_viewer_selection.",
            )
    else:
        assert selector is not None
        elements = selected(ifc, selector)
        if physical_only:
            verdict: dict[str, bool] = {}
            elements = [e for e in elements if not is_non_physical(e, verdict)]
        # filter_elements returns a set; keep results reproducible
        elements.sort(key=lambda e: e.id())
    if not elements:
        raise ToolError(
            "NO_MATCH",
            f"selector {selector!r} matched no measurable elements",
            "Check it with query_elements; voids and spaces are excluded "
            "unless physical_only=false.",
        )
    if len(elements) > max_elements:
        raise ToolError(
            "TOO_MANY_ELEMENTS",
            f"matched {len(elements)} elements, over the {max_elements} cap",
            "Narrow the selector, or raise max_elements if you accept the cost.",
        )
    return elements


def element_meshes(ifc: Any, elements: list[Any]) -> dict[int, tuple[np.ndarray, np.ndarray]]:
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
            if len(verts) and len(faces) and len(faces) <= MAX_TRIANGLES:
                out[shape.id] = (verts, faces)
        if not iterator.next():
            break
    return out


def mesh_boxes(meshes: dict[int, tuple[np.ndarray, np.ndarray]], ids: list[int]):
    lows = np.zeros((len(ids), 3))
    highs = np.zeros((len(ids), 3))
    for i, eid in enumerate(ids):
        verts = meshes[eid][0]
        lows[i] = verts.min(axis=0)
        highs[i] = verts.max(axis=0)
    return lows, highs


def local_rotation(element: Any) -> np.ndarray | None:
    """The element placement's rotation, or None when there is no placement.

    Only the rotation matters here: meshes are already world SI metres, so the
    local frame is applied around the mesh's own centroid.
    """
    placement = getattr(element, "ObjectPlacement", None)
    if placement is None:
        return None
    try:
        import ifcopenshell.util.placement as placement_util

        matrix = np.asarray(placement_util.get_local_placement(placement), dtype=np.float64)
    except Exception:
        return None
    rotation = matrix[:3, :3]
    norms = np.linalg.norm(rotation, axis=0)
    if not np.all(norms > _EPS):
        return None
    rotation = rotation / norms
    if abs(abs(float(np.linalg.det(rotation))) - 1.0) > 1e-3:
        return None
    return rotation


def _triangle_cross(verts: np.ndarray, faces: np.ndarray) -> np.ndarray:
    tris = verts[faces]
    return np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])


def mesh_volume(verts: np.ndarray, faces: np.ndarray) -> float:
    """Signed-tetrahedron volume of a closed mesh, in cubic metres."""
    tris = verts[faces]
    signed = np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2]))
    return abs(float(signed.sum())) / 6.0


def footprint_area(verts: np.ndarray, faces: np.ndarray) -> float:
    """Plan-projected area in square metres.

    For a closed solid every plan column is covered once from above and once
    from below, so half the unsigned z-projected triangle area is the
    footprint. Overhangs count more than once; probe_element flags those via
    the prismatic confidence check.
    """
    cross = _triangle_cross(verts, faces)
    return float(np.abs(cross[:, 2]).sum()) / 4.0


def probe_element(element: Any, mesh: tuple[np.ndarray, np.ndarray]) -> dict[str, Any]:
    """Derived geometry for one element, all values in SI metres."""
    verts, faces = mesh
    low = verts.min(axis=0)
    high = verts.max(axis=0)
    centroid = verts.mean(axis=0)

    rotation = local_rotation(element)
    if rotation is None:
        local = verts - centroid
        aligned = False
    else:
        local = (verts - centroid) @ rotation
        aligned = True
    local_low = local.min(axis=0)
    local_high = local.max(axis=0)
    extents = local_high - local_low

    volume = mesh_volume(verts, faces)
    box_volume = float(np.prod(np.maximum(extents, _EPS)))
    ratio = volume / box_volume if box_volume > _EPS else 0.0
    if ratio >= 0.95:
        confidence = "high"
    elif ratio >= 0.75:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
        "aabb": {
            "min": [round(v, 6) for v in low.tolist()],
            "max": [round(v, 6) for v in high.tolist()],
        },
        "local_extents": {
            "x": round(float(extents[0]), 6),
            "y": round(float(extents[1]), 6),
            "z": round(float(extents[2]), 6),
        },
        "placement_aligned": aligned,
        "footprint_area": round(footprint_area(verts, faces), 6),
        "volume": round(volume, 6),
        "centroid": [round(v, 6) for v in centroid.tolist()],
        "triangles": int(len(faces)),
        # ratio of mesh volume to the local-extent box; near 1 means the
        # element is prismatic and the extents are trustworthy dimensions
        "prismatic_ratio": round(ratio, 3),
        "confidence": confidence,
    }


def probe_elements(
    ifc: Any,
    *,
    selector: str | None = None,
    global_ids: list[str] | None = None,
    physical_only: bool = True,
    max_elements: int = 500,
) -> dict[str, Any]:
    """Geometry records for a selector- or id-defined element set."""
    from ifc_console.ifc.units import unit_info

    elements = resolve_targets(
        ifc,
        selector=selector,
        global_ids=global_ids,
        physical_only=physical_only,
        max_elements=max_elements,
    )
    meshes = element_meshes(ifc, elements)
    records = []
    missing = []
    for element in elements:
        mesh = meshes.get(element.id())
        if mesh is None:
            gid = getattr(element, "GlobalId", None)
            if gid:
                missing.append(gid)
            continue
        records.append(probe_element(element, mesh))
    if not records:
        raise ToolError(
            "NO_GEOMETRY",
            "none of the matched elements have usable geometry",
            "The probe needs solid geometry; annotations and empty spaces have none.",
        )
    return {
        "selector": selector,
        "units": {**unit_info(ifc), "values": "SI metres"},
        "matched": len(elements),
        "returned": len(records),
        "without_geometry": missing,
        "elements": records,
    }


def points_to_triangles_distance(points: np.ndarray, tris: np.ndarray) -> float:
    """Smallest distance from any point to any triangle, in metres.

    Closest-point-on-triangle (Ericson, Real-Time Collision Detection),
    vectorized over the full point x triangle grid in bounded blocks.
    """
    if not len(points) or not len(tris):
        return float("inf")
    best = float("inf")
    step = max(1, 200_000 // max(len(tris), 1))
    for start in range(0, len(points), step):
        p = points[start : start + step][:, None, :]
        a = tris[None, :, 0, :]
        b = tris[None, :, 1, :]
        c = tris[None, :, 2, :]
        ab = b - a
        ac = c - a
        ap = p - a
        d1 = np.einsum("ijk,ijk->ij", ab, ap)
        d2 = np.einsum("ijk,ijk->ij", ac, ap)
        bp = p - b
        d3 = np.einsum("ijk,ijk->ij", ab, bp)
        d4 = np.einsum("ijk,ijk->ij", ac, bp)
        cp = p - c
        d5 = np.einsum("ijk,ijk->ij", ab, cp)
        d6 = np.einsum("ijk,ijk->ij", ac, cp)

        va = d3 * d6 - d5 * d4
        vb = d5 * d2 - d1 * d6
        vc = d1 * d4 - d3 * d2
        denom = va + vb + vc
        safe = np.where(np.abs(denom) > _EPS, denom, 1.0)
        v = vb / safe
        w = vc / safe
        closest = a + v[..., None] * ab + w[..., None] * ac

        # vertex and edge regions override the face projection
        vertex_a = (d1 <= 0) & (d2 <= 0)
        vertex_b = (d3 >= 0) & (d4 <= d3)
        vertex_c = (d6 >= 0) & (d5 <= d6)
        edge_ab = (d1 >= 0) & (d3 <= 0) & (d1 * d4 - d3 * d2 <= 0)
        t_ab = d1 / np.where(np.abs(d1 - d3) > _EPS, d1 - d3, 1.0)
        edge_ac = (d2 >= 0) & (d6 <= 0) & (d5 * d2 - d1 * d6 <= 0)
        t_ac = d2 / np.where(np.abs(d2 - d6) > _EPS, d2 - d6, 1.0)
        edge_bc = (d4 - d3 >= 0) & (d5 - d6 >= 0) & (d3 * d6 - d5 * d4 <= 0)
        num_bc = d4 - d3
        den_bc = (d4 - d3) + (d5 - d6)
        t_bc = num_bc / np.where(np.abs(den_bc) > _EPS, den_bc, 1.0)

        closest = np.where(edge_bc[..., None], b + t_bc[..., None] * (c - b), closest)
        closest = np.where(edge_ac[..., None], a + t_ac[..., None] * ac, closest)
        closest = np.where(edge_ab[..., None], a + t_ab[..., None] * ab, closest)
        closest = np.where(vertex_c[..., None], c, closest)
        closest = np.where(vertex_b[..., None], b, closest)
        closest = np.where(vertex_a[..., None], a, closest)

        distances = np.linalg.norm(p - closest, axis=2)
        best = min(best, float(distances.min()))
    return best


__all__ = [
    "MAX_TRIANGLES",
    "NON_PHYSICAL",
    "element_meshes",
    "footprint_area",
    "is_non_physical",
    "local_rotation",
    "mesh_boxes",
    "mesh_volume",
    "points_to_triangles_distance",
    "probe_element",
    "probe_elements",
    "resolve_targets",
    "selected",
]
