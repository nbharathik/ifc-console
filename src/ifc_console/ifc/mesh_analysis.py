"""Deterministic evidence from an IFC triangle mesh.

The LLM-facing tools in this module return measurements and the small amount
of geometry needed to verify them, never the complete vertex/face arrays.
Raw IFC tessellations are inspected without mutation.  Trimesh is an optional
second opinion for standard health predicates; every operation also has a
NumPy implementation so the base install remains useful.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
from typing import Any

import numpy as np

from ifc_console.core.results import ToolError
from ifc_console.ifc import section

BACKENDS = ("auto", "builtin", "trimesh")
FRAMES = ("world", "local", "principal")

_EPS = 1e-12


def _arrays(
    vertices: np.ndarray, faces: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    verts = np.asarray(vertices, dtype=np.float64)
    raw_faces = np.asarray(faces)
    if verts.ndim != 2 or verts.shape[1:] != (3,):
        raise ToolError(
            "INVALID_GEOMETRY",
            f"vertices must have shape (n, 3), got {verts.shape}",
            "Re-tessellate the element before running mesh analysis.",
        )
    if raw_faces.ndim != 2 or raw_faces.shape[1:] != (3,):
        raise ToolError(
            "INVALID_GEOMETRY",
            f"faces must have shape (m, 3), got {raw_faces.shape}",
            "Mesh analysis needs triangular faces from the IFC tessellator.",
        )
    try:
        numeric_faces = np.asarray(raw_faces, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ToolError(
            "INVALID_GEOMETRY",
            "face indices must be integers",
            "Re-tessellate the element before running mesh analysis.",
        ) from exc
    if not np.all(np.isfinite(numeric_faces)) or not np.all(numeric_faces == np.floor(numeric_faces)):
        raise ToolError(
            "INVALID_GEOMETRY",
            "face indices must be finite integers",
            "Re-tessellate the element before running mesh analysis.",
        )
    tris = np.asarray(raw_faces, dtype=np.int64)
    return verts, tris


def mesh_hash(vertices: np.ndarray, faces: np.ndarray) -> str:
    """Stable SHA-256 of the untouched numeric mesh arrays."""
    verts, tris = _arrays(vertices, faces)
    canonical_vertices = np.ascontiguousarray(verts, dtype="<f8")
    canonical_faces = np.ascontiguousarray(tris, dtype="<i8")
    digest = hashlib.sha256()
    digest.update(b"ifc-console-mesh-v1\0")
    digest.update(np.asarray(canonical_vertices.shape, dtype="<i8").tobytes())
    digest.update(canonical_vertices.tobytes())
    digest.update(np.asarray(canonical_faces.shape, dtype="<i8").tobytes())
    digest.update(canonical_faces.tobytes())
    return f"sha256:{digest.hexdigest()}"


def _unit(vector: Any, *, name: str = "direction") -> np.ndarray:
    value = np.asarray(vector, dtype=np.float64)
    if value.shape != (3,) or not np.all(np.isfinite(value)):
        raise ToolError(
            "INVALID_INPUT",
            f"{name} must contain three finite numbers",
            f"Pass {name}=[x, y, z], for example [1, 0, 0].",
        )
    length = float(np.linalg.norm(value))
    if length <= _EPS:
        raise ToolError(
            "INVALID_INPUT",
            f"{name} must not be zero length",
            f"Pass a non-zero {name}, for example [0, 1, 0].",
        )
    return value / length


def _face_facts(
    vertices: np.ndarray, faces: np.ndarray, *, tolerance: float
) -> dict[str, Any]:
    """Validate faces and return masks used by health and query operations."""
    verts, tris = _arrays(vertices, faces)
    finite_vertices = np.all(np.isfinite(verts), axis=1)
    index_valid = np.all((tris >= 0) & (tris < len(verts)), axis=1)
    finite_faces = np.zeros(len(tris), dtype=bool)
    finite_faces[index_valid] = np.all(finite_vertices[tris[index_valid]], axis=1)
    repeated = np.zeros(len(tris), dtype=bool)
    repeated[index_valid] = (
        (tris[index_valid, 0] == tris[index_valid, 1])
        | (tris[index_valid, 1] == tris[index_valid, 2])
        | (tris[index_valid, 2] == tris[index_valid, 0])
    )
    geometric_degenerate = np.zeros(len(tris), dtype=bool)
    geometric_candidates = index_valid & finite_faces & ~repeated
    if np.any(geometric_candidates):
        triangle = verts[tris[geometric_candidates]]
        cross = np.cross(triangle[:, 1] - triangle[:, 0], triangle[:, 2] - triangle[:, 0])
        scale = np.maximum(
            np.maximum(
                np.linalg.norm(triangle[:, 1] - triangle[:, 0], axis=1),
                np.linalg.norm(triangle[:, 2] - triangle[:, 0], axis=1),
            ),
            max(tolerance, _EPS),
        )
        geometric_degenerate[geometric_candidates] = (
            np.linalg.norm(cross, axis=1) <= max(tolerance, _EPS) * scale
        )
    degenerate = repeated | geometric_degenerate
    topology_mask = index_valid & finite_faces & ~degenerate

    duplicate = np.zeros(len(tris), dtype=bool)
    if np.any(topology_mask):
        indices = np.flatnonzero(topology_mask)
        canonical = np.sort(tris[indices], axis=1)
        _, first = np.unique(canonical, axis=0, return_index=True)
        keep = np.zeros(len(indices), dtype=bool)
        keep[first] = True
        duplicate[indices[~keep]] = True
    usable_mask = topology_mask & ~duplicate
    return {
        "vertices": verts,
        "faces": tris,
        "finite_vertices": finite_vertices,
        "index_valid": index_valid,
        "finite_faces": finite_faces,
        "degenerate": degenerate,
        "duplicate": duplicate,
        "topology_mask": topology_mask,
        "usable_mask": usable_mask,
    }


def _components(face_count: int, inverse: np.ndarray, owners: np.ndarray) -> int:
    if face_count == 0:
        return 0
    parent = np.arange(face_count)

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = int(parent[item])
        return item

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    order = np.argsort(inverse, kind="stable")
    sorted_inverse = inverse[order]
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and sorted_inverse[end] == sorted_inverse[start]:
            end += 1
        group = owners[order[start:end]]
        for other in group[1:]:
            union(int(group[0]), int(other))
        start = end
    return len({find(index) for index in range(face_count)})


def _builtin_health(
    vertices: np.ndarray, faces: np.ndarray, *, tolerance: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    facts = _face_facts(vertices, faces, tolerance=tolerance)
    verts = facts["vertices"]
    tris = facts["faces"]
    topology_faces = tris[facts["topology_mask"]]
    usable_faces = tris[facts["usable_mask"]]

    boundary_edges = non_manifold_edges = unique_edges = components = 0
    winding_consistent = True
    used_vertices = np.zeros(0, dtype=np.int64)
    if len(topology_faces):
        directed_edges = np.concatenate(
            [
                topology_faces[:, [0, 1]],
                topology_faces[:, [1, 2]],
                topology_faces[:, [2, 0]],
            ],
            axis=0,
        )
        owners = np.concatenate([np.arange(len(topology_faces))] * 3)
        canonical_edges = np.sort(directed_edges, axis=1)
        _, inverse, counts = np.unique(
            canonical_edges, axis=0, return_inverse=True, return_counts=True
        )
        signs = np.where(directed_edges[:, 0] < directed_edges[:, 1], 1, -1)
        sign_sum = np.bincount(inverse, weights=signs, minlength=len(counts))
        boundary_edges = int(np.count_nonzero(counts == 1))
        non_manifold_edges = int(np.count_nonzero(counts > 2))
        unique_edges = int(len(counts))
        paired = counts == 2
        winding_consistent = bool(
            non_manifold_edges == 0 and np.all(np.abs(sign_sum[paired]) < 0.5)
        )
        components = _components(len(topology_faces), inverse, owners)
        used_vertices = np.unique(topology_faces)

    invalid_faces = int(np.count_nonzero(~facts["index_valid"] | ~facts["finite_faces"]))
    degenerate_faces = int(np.count_nonzero(facts["degenerate"] & facts["index_valid"]))
    duplicate_faces = int(np.count_nonzero(facts["duplicate"]))
    watertight = bool(
        len(topology_faces)
        and invalid_faces == 0
        and degenerate_faces == 0
        and duplicate_faces == 0
        and boundary_edges == 0
        and non_manifold_edges == 0
    )

    signed_volume = 0.0
    if len(usable_faces):
        centred = verts - np.mean(verts[np.unique(usable_faces)], axis=0)
        triangle = centred[usable_faces]
        signed_volume = float(
            np.einsum(
                "ij,ij->i", triangle[:, 0], np.cross(triangle[:, 1], triangle[:, 2])
            ).sum()
            / 6.0
        )
    scale = float(np.ptp(verts[np.all(np.isfinite(verts), axis=1)], axis=0).max()) if np.any(
        np.all(np.isfinite(verts), axis=1)
    ) else 0.0
    volume_epsilon = max(
        max(tolerance, _EPS) ** 3,
        max(scale, 1.0) ** 3 * 1e-15,
    )
    valid_volume = bool(
        watertight and winding_consistent and signed_volume > volume_epsilon
    )
    unique_usable_faces = len(usable_faces)
    euler = int(len(used_vertices) - unique_edges + unique_usable_faces)

    flags = []
    if invalid_faces:
        flags.append("invalid_faces")
    if int(np.count_nonzero(~facts["finite_vertices"])):
        flags.append("nonfinite_vertices")
    if degenerate_faces:
        flags.append("degenerate_faces")
    if duplicate_faces:
        flags.append("duplicate_faces")
    if boundary_edges:
        flags.append("open_boundary")
    if non_manifold_edges:
        flags.append("non_manifold_edges")
    if not winding_consistent:
        flags.append("inconsistent_winding")
    if watertight and signed_volume < -volume_epsilon:
        flags.append("inward_winding")
    if not valid_volume:
        flags.append("volume_unreliable")

    health = {
        "vertices": int(len(verts)),
        "triangles": int(len(tris)),
        "used_vertices": int(len(used_vertices)),
        "unreferenced_vertices": int(len(verts) - len(used_vertices)),
        "connected_components": components,
        "watertight": watertight,
        "winding_consistent": winding_consistent,
        "valid_volume": valid_volume,
        "boundary_edges": boundary_edges,
        "non_manifold_edges": non_manifold_edges,
        "degenerate_faces": degenerate_faces,
        "duplicate_faces": duplicate_faces,
        "invalid_faces": invalid_faces,
        "nonfinite_vertices": int(np.count_nonzero(~facts["finite_vertices"])),
        "euler_characteristic": euler,
        "signed_volume_si": round(signed_volume, 9),
        "volume_si": round(abs(signed_volume), 9),
        "confidence": "high" if valid_volume else "low",
        "flags": flags,
    }
    return health, facts


def _trimesh_module(*, required: bool) -> Any | None:
    try:
        import trimesh
    except ImportError as exc:
        if required:
            raise ToolError(
                "EXTRA_NOT_INSTALLED",
                "the Trimesh geometry backend is not installed",
                "Install `ifc-console[geometry]`, restart ifc-console, then retry; "
                "or use backend='builtin'.",
            ) from exc
        return None
    return trimesh


def to_trimesh(vertices: np.ndarray, faces: np.ndarray) -> Any:
    """Build a raw Trimesh adapter without processing or validation."""
    trimesh = _trimesh_module(required=True)
    verts, tris = _arrays(vertices, faces)
    # Trimesh 5 may share memory when process=False.  A caller is allowed to
    # experiment with the returned adapter, but it must never mutate the
    # session's cached IFC arrays.
    return trimesh.Trimesh(
        vertices=np.array(verts, dtype=np.float64, order="C", copy=True),
        faces=np.array(tris, dtype=np.int64, order="C", copy=True),
        process=False,
        validate=False,
    )


def mesh_health(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    backend: str = "auto",
    tolerance: float = 1e-9,
    include_filtered_preview: bool = False,
) -> dict[str, Any]:
    """Topology and volume prerequisites for the untouched tessellation."""
    if backend not in BACKENDS:
        raise ToolError(
            "INVALID_INPUT",
            f"backend must be one of {', '.join(BACKENDS)}",
            "Use auto for Trimesh when installed with a deterministic NumPy fallback.",
        )
    if not np.isfinite(tolerance) or tolerance <= 0:
        raise ToolError(
            "INVALID_INPUT",
            "tolerance must be a finite positive number",
            "Use a small SI-metre tolerance such as 0.000001.",
        )

    health, facts = _builtin_health(vertices, faces, tolerance=tolerance)
    trimesh = _trimesh_module(required=backend == "trimesh") if backend != "builtin" else None
    if trimesh is not None and health["invalid_faces"] == 0 and health["nonfinite_vertices"] == 0:
        try:
            raw = trimesh.Trimesh(
                vertices=np.array(facts["vertices"], dtype=np.float64, order="C", copy=True),
                faces=np.array(facts["faces"], dtype=np.int64, order="C", copy=True),
                process=False,
                validate=False,
            )
            health["watertight"] = bool(raw.is_watertight)
            health["winding_consistent"] = bool(raw.is_winding_consistent)
            health["valid_volume"] = bool(raw.is_volume)
            health["euler_characteristic"] = int(raw.euler_number)
            health["backend"] = "trimesh"
            try:
                health["backend_version"] = importlib.metadata.version("trimesh")
            except importlib.metadata.PackageNotFoundError:
                health["backend_version"] = getattr(trimesh, "__version__", None)
            # Keep the detailed raw flags deterministic, but make the verdict
            # match the selected backend's standard predicates.
            if health["valid_volume"]:
                health["confidence"] = "high"
                health["flags"] = [flag for flag in health["flags"] if flag != "volume_unreliable"]
            else:
                health["confidence"] = "low"
                if "volume_unreliable" not in health["flags"]:
                    health["flags"].append("volume_unreliable")
        except Exception as exc:
            if backend == "trimesh":
                raise ToolError(
                    "GEOMETRY_ANALYSIS_FAILED",
                    f"Trimesh could not inspect the raw mesh: {exc}",
                    "Retry with backend='builtin' to use the dependency-free checks.",
                ) from exc
            health["backend"] = "builtin"
            health["backend_fallback"] = type(exc).__name__
    else:
        health["backend"] = "builtin"
        if backend == "auto" and trimesh is None:
            health["optional_backend"] = "trimesh_not_installed"
    health["repair_applied"] = False

    if include_filtered_preview:
        usable = facts["faces"][facts["usable_mask"]]
        if len(usable):
            used = np.unique(usable)
            remap = np.full(len(facts["vertices"]), -1, dtype=np.int64)
            remap[used] = np.arange(len(used))
            preview_health = mesh_health(
                facts["vertices"][used],
                remap[usable],
                backend=backend,
                tolerance=tolerance,
                include_filtered_preview=False,
            )
        else:
            preview_health = None
        health["filtered_copy_preview"] = {
            "used_for_measurements": False,
            "operations": [
                "drop invalid and non-finite faces",
                "drop degenerate faces",
                "drop duplicate faces",
                "compact unreferenced vertices",
            ],
            "health": preview_health,
        }
    return health


def mesh_source(
    vertices: np.ndarray,
    faces: np.ndarray,
    *,
    tessellation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verts, tris = _arrays(vertices, faces)
    result: dict[str, Any] = {
        "mesh_hash": mesh_hash(verts, tris),
        "vertices": int(len(verts)),
        "triangles": int(len(tris)),
        "coordinate_frame": "world",
        "repair_applied": False,
    }
    if tessellation is not None:
        result["tessellation"] = tessellation
    return result


def principal_frame(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray, list[str]]:
    """Right-handed deterministic PCA basis (columns) and eigenvalues."""
    verts = np.asarray(vertices, dtype=np.float64)
    finite = verts[np.all(np.isfinite(verts), axis=1)]
    if len(finite) < 3:
        raise ToolError(
            "INVALID_GEOMETRY",
            "at least three finite vertices are needed for a principal frame",
            "Inspect the mesh health before requesting principal coordinates.",
        )
    centred = finite - finite.mean(axis=0)
    values, basis = np.linalg.eigh(centred.T @ centred / max(len(centred), 1))
    order = np.argsort(values)[::-1]
    values, basis = values[order], basis[:, order]
    for column in range(3):
        vector = basis[:, column]
        anchor = int(np.argmax(np.abs(vector)))
        if vector[anchor] < 0:
            basis[:, column] *= -1
    if np.linalg.det(basis) < 0:
        basis[:, 2] *= -1
    flags = []
    scale = max(float(values[0]), _EPS)
    if abs(float(values[0] - values[1])) / scale < 1e-5:
        flags.append("principal_axis_1_2_ambiguous")
    if abs(float(values[1] - values[2])) / scale < 1e-5:
        flags.append("principal_axis_2_3_ambiguous")
    return basis, values, flags


def resolve_direction(
    vertices: np.ndarray,
    direction: Any,
    *,
    frame: str,
    local_rotation: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, Any], list[str]]:
    if frame not in FRAMES:
        raise ToolError(
            "INVALID_INPUT",
            f"frame must be one of {', '.join(FRAMES)}",
            "world uses model axes; local uses the IFC placement; principal uses mesh PCA.",
        )
    requested = _unit(direction)
    flags: list[str] = []
    evidence: dict[str, Any] = {
        "frame": frame,
        "input_direction": [round(float(value), 9) for value in requested],
    }
    if frame == "world":
        world = requested
    elif frame == "local":
        if local_rotation is None:
            raise ToolError(
                "FRAME_UNAVAILABLE",
                "the element has no usable local placement frame",
                "Use frame='world' or frame='principal' for this element.",
            )
        rotation = np.asarray(local_rotation, dtype=np.float64)
        if rotation.shape != (3, 3) or not np.all(np.isfinite(rotation)):
            raise ToolError(
                "FRAME_UNAVAILABLE",
                "the element's local placement rotation is invalid",
                "Use frame='world' or frame='principal' for this element.",
            )
        world = _unit(rotation @ requested)
        evidence["basis_world"] = [
            [round(float(value), 9) for value in rotation[:, column]] for column in range(3)
        ]
    else:
        basis, eigenvalues, principal_flags = principal_frame(vertices)
        world = _unit(basis @ requested)
        flags.extend(principal_flags)
        evidence["basis_world"] = [
            [round(float(value), 9) for value in basis[:, column]] for column in range(3)
        ]
        evidence["eigenvalues"] = [round(float(value), 12) for value in eigenvalues]
    evidence["world_direction"] = [round(float(value), 9) for value in world]
    return world, evidence, flags


def directional_extent(
    vertices: np.ndarray,
    faces: np.ndarray,
    direction: Any,
    *,
    frame: str = "world",
    local_rotation: np.ndarray | None = None,
    tessellation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Outside-to-outside support extent along an arbitrary direction."""
    verts, tris = _arrays(vertices, faces)
    facts = _face_facts(verts, tris, tolerance=1e-12)
    usable_faces = tris[facts["usable_mask"]]
    if not len(usable_faces):
        raise ToolError(
            "INVALID_GEOMETRY",
            "the mesh has no usable triangular faces",
            "Inspect the mesh health and re-tessellate the element.",
        )
    used = np.unique(usable_faces)
    measured_vertices = verts[used]
    world, frame_evidence, flags = resolve_direction(
        measured_vertices, direction, frame=frame, local_rotation=local_rotation
    )
    ignored = len(verts) - len(used)
    if ignored:
        flags.append("unreferenced_or_invalid_vertices_ignored")
    if np.count_nonzero(~facts["usable_mask"]):
        flags.append("invalid_faces_ignored")
    centre = measured_vertices.mean(axis=0)
    relative = measured_vertices - centre
    projection = relative @ world
    low_index, high_index = int(np.argmin(projection)), int(np.argmax(projection))
    low, high = float(projection[low_index]), float(projection[high_index])
    return {
        "definition": "outside_to_outside_extent",
        "method": "vertex_support_projection",
        "extent_si": round(high - low, 9),
        "projection_si": {"min": round(low, 9), "max": round(high, 9)},
        "support_points": {
            "min": [round(float(value), 9) for value in measured_vertices[low_index]],
            "max": [round(float(value), 9) for value in measured_vertices[high_index]],
        },
        "direction": frame_evidence,
        "source": mesh_source(verts, tris, tessellation=tessellation),
        "confidence": "high" if not flags else "medium",
        "flags": flags,
    }


def slice_mesh(
    vertices: np.ndarray,
    faces: np.ndarray,
    normal: Any,
    *,
    origin: Any | None = None,
    frame: str = "world",
    local_rotation: np.ndarray | None = None,
    backend: str = "auto",
    tolerance: float = 1e-9,
    include_outline: bool = True,
    outline_points: int = 160,
    tessellation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Arbitrary plane section with a reconstructable world-space frame."""
    verts, tris = _arrays(vertices, faces)
    facts = _face_facts(verts, tris, tolerance=max(tolerance, 1e-12))
    usable_faces = tris[facts["usable_mask"]]
    if not len(usable_faces):
        raise ToolError(
            "INVALID_GEOMETRY",
            "the mesh has no usable triangular faces",
            "Inspect the mesh health and re-tessellate the element.",
        )
    used_vertices = verts[np.unique(usable_faces)]
    world_normal, frame_evidence, flags = resolve_direction(
        used_vertices,
        normal,
        frame=frame,
        local_rotation=local_rotation,
    )
    if origin is None:
        plane_origin = used_vertices.mean(axis=0)
        origin_source = "referenced_vertex_centroid"
    else:
        plane_origin = np.asarray(origin, dtype=np.float64)
        if plane_origin.shape != (3,) or not np.all(np.isfinite(plane_origin)):
            raise ToolError(
                "INVALID_INPUT",
                "origin must contain three finite world coordinates",
                "Pass origin=[x, y, z] in SI metres, or omit it for the mesh centroid.",
            )
        origin_source = "requested_world_point"
    health = mesh_health(verts, tris, backend=backend, tolerance=tolerance)
    metrics = section.section_metrics(
        verts,
        tris,
        plane_origin,
        world_normal,
        include_outline=include_outline,
        outline_points=outline_points,
    )
    if metrics is None:
        flags.append("plane_misses_mesh")
    elif not metrics["closed"]:
        flags.append("open_section")
    confidence = "low"
    if metrics is not None and metrics["closed"]:
        confidence = "high" if health["valid_volume"] else "medium"
    return {
        "definition": "triangle_mesh_plane_section",
        "method": "triangle_plane_intersection_loops",
        "intersects": metrics is not None,
        "plane": {
            "origin": [round(float(value), 9) for value in plane_origin],
            "origin_source": origin_source,
            "normal": frame_evidence,
        },
        "section": metrics,
        "prerequisites": {
            "mesh_watertight": health["watertight"],
            "mesh_valid_volume": health["valid_volume"],
            "section_closed": metrics["closed"] if metrics is not None else False,
        },
        "source": mesh_source(verts, tris, tessellation=tessellation),
        "backend": health["backend"],
        "confidence": confidence,
        "flags": list(dict.fromkeys(flags)),
    }


def _line_hits(
    vertices: np.ndarray,
    faces: np.ndarray,
    origin: np.ndarray,
    direction: np.ndarray,
    *,
    tolerance: float,
) -> list[dict[str, Any]]:
    facts = _face_facts(vertices, faces, tolerance=max(tolerance * 1e-3, 1e-12))
    face_ids = np.flatnonzero(facts["usable_mask"])
    if not len(face_ids):
        return []
    triangle = facts["vertices"][facts["faces"][face_ids]] - origin
    edge_1 = triangle[:, 1] - triangle[:, 0]
    edge_2 = triangle[:, 2] - triangle[:, 0]
    h = np.cross(np.broadcast_to(direction, edge_2.shape), edge_2)
    determinant = np.einsum("ij,ij->i", edge_1, h)
    determinant_scale = np.maximum(
        np.linalg.norm(edge_1, axis=1) * np.linalg.norm(edge_2, axis=1), _EPS
    )
    candidate = np.abs(determinant) > 1e-12 * determinant_scale
    safe = np.where(candidate, determinant, 1.0)
    inv = 1.0 / safe
    s = -triangle[:, 0]
    u = inv * np.einsum("ij,ij->i", s, h)
    q = np.cross(s, edge_1)
    v = inv * np.einsum("ij,j->i", q, direction)
    distance = inv * np.einsum("ij,ij->i", edge_2, q)
    barycentric_tolerance = 1e-9
    hit = (
        candidate
        & np.isfinite(distance)
        & (u >= -barycentric_tolerance)
        & (v >= -barycentric_tolerance)
        & (u + v <= 1.0 + barycentric_tolerance)
    )
    selected = np.flatnonzero(hit)
    if not len(selected):
        return []
    order = selected[np.argsort(distance[selected], kind="stable")]
    span = float(np.ptp(facts["vertices"], axis=0).max()) if len(facts["vertices"]) else 0.0
    merge_tolerance = max(tolerance, span * 1e-10, 1e-10)
    groups: list[list[int]] = []
    for face_index in order:
        if not groups or abs(float(distance[face_index] - distance[groups[-1][0]])) > merge_tolerance:
            groups.append([int(face_index)])
        else:
            groups[-1].append(int(face_index))

    result = []
    for group in groups:
        signed_distance = float(np.mean(distance[group]))
        normals = np.cross(edge_1[group], edge_2[group])
        lengths = np.linalg.norm(normals, axis=1)
        normals = normals[lengths > _EPS] / lengths[lengths > _EPS, None]
        mean_normal = normals.mean(axis=0) if len(normals) else np.zeros(3)
        if np.linalg.norm(mean_normal) > _EPS:
            mean_normal = mean_normal / np.linalg.norm(mean_normal)
        result.append(
            {
                "distance_si": round(signed_distance, 9),
                "point": [
                    round(float(value), 9) for value in origin + signed_distance * direction
                ],
                "normal": [round(float(value), 9) for value in mean_normal],
                "normal_dot_direction": round(
                    float(np.dot(mean_normal, direction)), 9
                ),
                "normal_signs": sorted(
                    {
                        -1 if float(np.dot(normal, direction)) < -1e-9 else 1
                        for normal in normals
                        if abs(float(np.dot(normal, direction))) > 1e-9
                    }
                ),
                "face_ids": [int(face_ids[index]) for index in group],
                "merged_triangle_hits": len(group),
            }
        )
    return result


def ray_intervals(
    vertices: np.ndarray,
    faces: np.ndarray,
    origin: Any,
    direction: Any,
    *,
    frame: str = "world",
    local_rotation: np.ndarray | None = None,
    backend: str = "auto",
    tolerance: float = 1e-6,
    tessellation: dict[str, Any] | None = None,
    max_intersections: int | None = None,
) -> dict[str, Any]:
    """Line/mesh intersections and trustworthy material/cavity intervals."""
    verts, tris = _arrays(vertices, faces)
    point = np.asarray(origin, dtype=np.float64)
    if point.shape != (3,) or not np.all(np.isfinite(point)):
        raise ToolError(
            "INVALID_INPUT",
            "origin must contain three finite world coordinates",
            "Pass origin=[x, y, z] in SI metres.",
        )
    if not np.isfinite(tolerance) or tolerance <= 0:
        raise ToolError(
            "INVALID_INPUT",
            "tolerance must be a finite positive number",
            "Use a small SI-metre tolerance such as 0.000001.",
        )
    facts = _face_facts(verts, tris, tolerance=1e-12)
    usable_faces = tris[facts["usable_mask"]]
    if len(usable_faces):
        direction_vertices = verts[np.unique(usable_faces)]
    else:
        direction_vertices = verts[np.all(np.isfinite(verts), axis=1)]
    world, frame_evidence, flags = resolve_direction(
        direction_vertices,
        direction,
        frame=frame,
        local_rotation=local_rotation,
    )
    if len(usable_faces) and len(np.unique(usable_faces)) < len(verts):
        flags.append("unreferenced_or_invalid_vertices_ignored")
    health = mesh_health(verts, tris, backend=backend, tolerance=max(tolerance * 1e-3, 1e-12))
    hits = _line_hits(verts, tris, point, world, tolerance=tolerance)
    if max_intersections is not None and len(hits) > max_intersections:
        raise ToolError(
            "RESULT_TOO_LARGE",
            f"the line produced {len(hits)} surface intersections; the limit is {max_intersections}",
            "Choose a more local origin/direction or analyze a simpler IFC element.",
        )
    reliable = bool(health["valid_volume"] and len(hits) >= 2)
    if not hits:
        flags.append("line_misses_mesh")
    if not health["valid_volume"]:
        flags.append("mesh_not_a_valid_volume")
    material_intervals = []
    non_material_intervals = []
    if reliable:
        depth = 0
        interval_start = None
        for event in hits:
            signs = event.pop("normal_signs")
            if signs == [-1]:
                event["kind"] = "entry"
                before = depth
                depth += 1
                if before == 0:
                    interval_start = event
            elif signs == [1]:
                event["kind"] = "exit"
                before = depth
                depth -= 1
                if depth < 0:
                    reliable = False
                    flags.append("exit_before_entry")
                    break
                if before > 0 and depth == 0 and interval_start is not None:
                    material_intervals.append(
                        {
                            "start_distance_si": interval_start["distance_si"],
                            "end_distance_si": event["distance_si"],
                            "thickness_si": round(
                                event["distance_si"] - interval_start["distance_si"], 9
                            ),
                            "start_point": interval_start["point"],
                            "end_point": event["point"],
                        }
                    )
                    interval_start = None
            else:
                event["kind"] = "ambiguous_surface"
                reliable = False
                flags.append("ambiguous_tangent_or_coincident_hit")
                break
        if depth != 0:
            reliable = False
            flags.append("unbalanced_intersections")
        if not material_intervals:
            reliable = False
        if reliable:
            for previous, following in zip(
                material_intervals, material_intervals[1:], strict=False
            ):
                non_material_intervals.append(
                    {
                        "start_distance_si": previous["end_distance_si"],
                        "end_distance_si": following["start_distance_si"],
                        "clear_width_si": round(
                            following["start_distance_si"] - previous["end_distance_si"], 9
                        ),
                        "start_point": previous["end_point"],
                        "end_point": following["start_point"],
                    }
                )
        else:
            material_intervals = []
            non_material_intervals = []
    if not reliable:
        for event in hits:
            event.pop("normal_signs", None)
            if event.get("kind") not in {"ambiguous_surface"}:
                event["kind"] = "unclassified_surface"

    origin_classification = "outside"
    if reliable:
        for interval in material_intervals:
            if interval["start_distance_si"] < 0 < interval["end_distance_si"]:
                origin_classification = "material"
                break
        else:
            for interval in non_material_intervals:
                if interval["start_distance_si"] < 0 < interval["end_distance_si"]:
                    origin_classification = "internal_void_or_gap"
                    break

    if health["connected_components"] > 1 and non_material_intervals:
        flags.append("voids_may_be_gaps_between_components")
    refusal = None
    if not reliable:
        refusal = (
            "Material intervals were not inferred because the mesh is not a valid "
            "outward-oriented closed volume or the line crossings cannot be paired."
        )

    return {
        "definition": "material_intervals_along_infinite_line",
        "method": "oriented_moller_trumbore_occupancy_sweep",
        "origin": [round(float(value), 9) for value in point],
        "direction": frame_evidence,
        "intersections": hits,
        "material_intervals": material_intervals,
        "non_material_intervals": non_material_intervals,
        "overall_width_si": round(hits[-1]["distance_si"] - hits[0]["distance_si"], 9)
        if len(hits) >= 2
        else None,
        "clear_internal_width_si": max(
            (interval["clear_width_si"] for interval in non_material_intervals), default=None
        ),
        "origin_classification": origin_classification,
        "prerequisites": {
            "watertight": health["watertight"],
            "winding_consistent": health["winding_consistent"],
            "valid_volume": health["valid_volume"],
            "balanced_oriented_intersections": reliable,
        },
        "source": mesh_source(verts, tris, tessellation=tessellation),
        "backend": health["backend"],
        "confidence": "high" if reliable and health["connected_components"] == 1 else (
            "medium" if reliable else "low"
        ),
        "flags": list(dict.fromkeys(flags)),
        "refusal": refusal,
    }


__all__ = [
    "BACKENDS",
    "FRAMES",
    "directional_extent",
    "mesh_hash",
    "mesh_health",
    "mesh_source",
    "principal_frame",
    "ray_intervals",
    "resolve_direction",
    "slice_mesh",
    "to_trimesh",
]
