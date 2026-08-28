"""Shared triangle-mesh machinery and the per-element geometry probe.

The stock ifcopenshell wheel has no OpenCASCADE, so like clash detection every
derived number here comes from world-space triangle meshes. Mesh coordinates
from the iterator are SI metres regardless of the file's length unit.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Literal

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

# A room has a solid to measure even though it is not a physical element;
# these classes have none, so a derived takeoff always skips them.
NON_MEASURABLE = (
    "IfcOpeningElement",
    "IfcAnnotation",
    "IfcGrid",
    "IfcVirtualElement",
)

# Triangles per element above this are dropped: an element that dense is a
# rendering mesh, not something anyone measures or clashes by hand.
MAX_TRIANGLES = 20_000
ANALYSIS_MAX_TRIANGLES = 100_000
_EPS = 1e-9

TessellationProfile = Literal["standard", "analysis"]

# The standard profile deliberately leaves IfcOpenShell's mesher controls at
# their installed-version defaults.  Changing those values would invalidate
# the meshes used by every existing probe, takeoff and clash call.  Analysis is
# opt-in and records every override applied to the iterator.
_TESSELLATION_PROFILES: dict[str, dict[str, Any]] = {
    "standard": {
        "max_triangles": MAX_TRIANGLES,
        "settings": {"use-world-coords": True},
    },
    "analysis": {
        "max_triangles": ANALYSIS_MAX_TRIANGLES,
        "settings": {
            "use-world-coords": True,
            "mesher-linear-deflection": 0.0005,
            "mesher-angular-deflection": 0.25,
            "weld-vertices": True,
            # Reorientation changes topology evidence, so inspect the raw IFC
            # tessellation and report bad winding instead of repairing it.
            "reorient-shells": False,
        },
    },
}


def tessellation_evidence(
    profile: TessellationProfile = "standard", *, max_triangles: int | None = None
) -> dict[str, Any]:
    """The exact iterator overrides and element budget used by a mesh request."""
    if profile not in _TESSELLATION_PROFILES:
        names = ", ".join(sorted(_TESSELLATION_PROFILES))
        raise ToolError(
            "INVALID_INPUT",
            f"unknown tessellation profile {profile!r}",
            f"Use one of: {names}.",
        )
    if max_triangles is not None and (
        isinstance(max_triangles, bool)
        or not isinstance(max_triangles, int)
        or max_triangles <= 0
    ):
        raise ToolError(
            "INVALID_INPUT",
            f"max_triangles must be a positive integer, got {max_triangles!r}",
            "Use the profile default, or pass a positive per-element triangle cap.",
        )
    spec = _TESSELLATION_PROFILES[profile]
    return {
        "profile": profile,
        "max_triangles": max_triangles or int(spec["max_triangles"]),
        "settings": dict(spec["settings"]),
        "repairs_applied": False,
    }


def _class_hit(element: Any, classes: tuple[str, ...], cache: dict[str, bool]) -> bool:
    name = element.is_a()
    hit = cache.get(name)
    if hit is None:
        # is_a(cls) matches subtypes; an exact name test misses IFC4 cases
        # like IfcOpeningStandardCase. Verdicts are cached per class name.
        hit = any(element.is_a(cls) for cls in classes)
        cache[name] = hit
    return hit


def is_non_physical(element: Any, cache: dict[str, bool]) -> bool:
    return _class_hit(element, NON_PHYSICAL, cache)


def is_non_measurable(element: Any, cache: dict[str, bool]) -> bool:
    """True when the class has no solid to measure, spaces excluded."""
    return _class_hit(element, NON_MEASURABLE, cache)


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
    offset: int = 0,
) -> list[Any]:
    """Resolve exactly one of selector or global_ids to elements.

    offset skips the first N of the deterministic order, so a caller can walk a
    match one page at a time instead of only capping it.
    """
    if offset < 0:
        raise ToolError(
            "INVALID_INPUT",
            f"offset {offset} is negative",
            "offset counts elements to skip; the first page is offset=0.",
        )
    if isinstance(max_elements, bool) or not isinstance(max_elements, int) or max_elements < 1:
        raise ToolError(
            "INVALID_INPUT",
            "max_elements must be a positive integer",
            "Use max_elements as the page size and offset to request later pages.",
        )
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
    matched = len(elements)
    elements = elements[offset : offset + max_elements]
    if not elements:
        if matched:
            raise ToolError(
                "NO_MATCH",
                f"offset {offset} is past the {matched} matched elements",
                "The last page is the one that comes back short; start again at offset=0.",
            )
        raise ToolError(
            "NO_MATCH",
            f"selector {selector!r} matched no measurable elements",
            "Check it with query_elements; voids and spaces are excluded "
            "unless physical_only=false.",
        )
    return elements


# Set by the caller that owns a mesh cache; thread-local because each model
# session tessellates on its own worker thread.
_provider = threading.local()

MeshProvider = Callable[..., dict[int, tuple[np.ndarray, np.ndarray]]]


@contextmanager
def mesh_provider(provider: MeshProvider) -> Iterator[None]:
    """Serve element_meshes from `provider` inside the block.

    The slot is cleared while the provider runs, so a cache that tessellates
    its misses through element_meshes reaches the real work, not itself.
    """
    previous = getattr(_provider, "fn", None)
    _provider.fn = provider
    try:
        yield
    finally:
        _provider.fn = previous


def element_meshes(
    ifc: Any,
    elements: list[Any],
    *,
    profile: TessellationProfile = "standard",
    max_triangles: int | None = None,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """World-space (vertices, triangle indices) per element id, geometry only.

    Served by the installed mesh provider when there is one, so probing an
    element and then measuring it tessellates once.
    """
    # Validate before entering a provider so every cache sees a canonical,
    # supported request.
    tessellation_evidence(profile, max_triangles=max_triangles)
    default_request = profile == "standard" and max_triangles is None
    provider = getattr(_provider, "fn", None)
    if provider is None:
        # Keep two-argument monkeypatches and integrations working for the
        # unchanged default path.
        if default_request:
            return _tessellate(ifc, elements)
        return _tessellate(
            ifc,
            elements,
            profile=profile,
            max_triangles=max_triangles,
        )
    _provider.fn = None
    try:
        if default_request:
            return provider(ifc, elements)
        return provider(
            ifc,
            elements,
            profile=profile,
            max_triangles=max_triangles,
        )
    finally:
        _provider.fn = provider


def _tessellate(
    ifc: Any,
    elements: list[Any],
    *,
    profile: TessellationProfile = "standard",
    max_triangles: int | None = None,
) -> dict[int, tuple[np.ndarray, np.ndarray]]:
    """One iterator run: the real work behind element_meshes."""
    import multiprocessing

    import ifcopenshell.geom as geom

    wanted = {e.id() for e in elements}
    if not wanted:
        return {}

    evidence = tessellation_evidence(profile, max_triangles=max_triangles)
    settings = geom.settings()
    for name, value in evidence["settings"].items():
        settings.set(name, value)
    out: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    try:
        # Spinning up the worker threads costs more than it saves on a small
        # set, and a probe is usually a handful of elements.
        cpus = 1 if len(elements) < 32 else max(1, min(multiprocessing.cpu_count() - 1, 8))
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
            if len(verts) and len(faces) and len(faces) <= evidence["max_triangles"]:
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
    # The sum is translation invariant, so centre first: on a georeferenced
    # file the raw terms reach 1e18 while the answer is ~1e0 and float64
    # cancellation eats every significant digit.
    tris = (verts - verts.mean(axis=0))[faces]
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


def surface_areas(
    verts: np.ndarray, faces: np.ndarray, rotation: np.ndarray | None = None
) -> dict[str, float]:
    """Mesh area split by dominant face normal, in square metres.

    The buckets partition the mesh, so they sum to surface_area. The split is
    taken in the element's frame when a rotation is given, so a wall on a skew
    grid reports one side area instead of two halves.
    """
    cross = _triangle_cross(verts, faces)
    if rotation is not None:
        cross = cross @ rotation
    areas = np.linalg.norm(cross, axis=1) / 2.0
    axis = np.argmax(np.abs(cross), axis=1)
    up = cross[:, 2] > 0.0
    return {
        "surface_area": float(areas.sum()),
        "top_area": float(areas[(axis == 2) & up].sum()),
        "bottom_area": float(areas[(axis == 2) & ~up].sum()),
        "side_area_x": float(areas[axis == 0].sum()),
        "side_area_y": float(areas[axis == 1].sum()),
    }


def probe_element(element: Any, mesh: tuple[np.ndarray, np.ndarray]) -> dict[str, Any]:
    """Derived geometry for one element, all values in SI metres."""
    from ifc_console.ifc.mesh_analysis import mesh_health

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
    health = mesh_health(verts, faces, backend="builtin")
    box_volume = float(np.prod(np.maximum(extents, _EPS)))
    ratio = volume / box_volume if box_volume > _EPS else 0.0
    if not health["valid_volume"]:
        confidence = "low"
    elif ratio >= 0.95:
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
        **{name: round(value, 6) for name, value in surface_areas(verts, faces, rotation).items()},
        "volume": round(volume, 6),
        "volume_reliable": health["valid_volume"],
        "centroid": [round(v, 6) for v in centroid.tolist()],
        "triangles": int(len(faces)),
        # ratio of mesh volume to the local-extent box; near 1 means the
        # element is prismatic and the extents are trustworthy dimensions
        "prismatic_ratio": round(ratio, 3),
        "confidence": confidence,
        "mesh_health": {
            key: health[key]
            for key in (
                "connected_components",
                "watertight",
                "winding_consistent",
                "valid_volume",
                "boundary_edges",
                "non_manifold_edges",
                "degenerate_faces",
                "duplicate_faces",
                "euler_characteristic",
                "flags",
            )
        },
    }


def probe_elements(
    ifc: Any,
    *,
    selector: str | None = None,
    global_ids: list[str] | None = None,
    physical_only: bool = True,
    max_elements: int = 500,
    offset: int = 0,
) -> dict[str, Any]:
    """Geometry records for a selector- or id-defined element set."""
    from ifc_console.ifc.units import unit_info

    elements = resolve_targets(
        ifc,
        selector=selector,
        global_ids=global_ids,
        physical_only=physical_only,
        max_elements=max_elements,
        offset=offset,
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
    "ANALYSIS_MAX_TRIANGLES",
    "MAX_TRIANGLES",
    "NON_MEASURABLE",
    "NON_PHYSICAL",
    "element_meshes",
    "footprint_area",
    "is_non_measurable",
    "is_non_physical",
    "local_rotation",
    "mesh_boxes",
    "mesh_provider",
    "mesh_volume",
    "points_to_triangles_distance",
    "probe_element",
    "probe_elements",
    "resolve_targets",
    "selected",
    "surface_areas",
    "tessellation_evidence",
]
