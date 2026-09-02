"""Planar cross sections of triangle meshes and their 2D metrics.

All inputs and outputs are SI metres, matching the geometry iterator. A
section is the set of segments where a plane cuts the mesh; every metric is
derived from those segments, so open meshes still produce numbers (with the
closed flag telling the caller how much to trust the area).
"""

from __future__ import annotations

from typing import Any

import numpy as np

_EPS = 1e-9
# endpoint merge tolerance; adjacent triangles produce bitwise-equal cut
# points, so this only has to absorb float noise
_WELD = 1e-6
_MAX_THICKNESS_RAYS = 2000


def _unit(vector: np.ndarray) -> np.ndarray:
    vector = np.asarray(vector, dtype=np.float64)
    length = float(np.linalg.norm(vector))
    if length < _EPS:
        raise ValueError("zero-length direction")
    return vector / length


def slice_segments(
    verts: np.ndarray,
    faces: np.ndarray,
    origin: np.ndarray,
    normal: np.ndarray,
    *,
    tolerance: float = _WELD,
) -> np.ndarray:
    """Cut segments (n, 2, 3) where the plane meets the mesh."""
    if not np.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance must be positive")
    normal = _unit(normal)
    distances = (verts - origin) @ normal
    plane_epsilon = max(tolerance * 1e-3, 1e-12)
    distances = np.where(np.abs(distances) < plane_epsilon, plane_epsilon, distances)
    tri = distances[faces]

    crossings = []
    points = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        di, dj = tri[:, i], tri[:, j]
        cross = di * dj < 0
        t = di / np.where(np.abs(di - dj) > plane_epsilon, di - dj, 1.0)
        p = verts[faces[:, i]] + t[:, None] * (verts[faces[:, j]] - verts[faces[:, i]])
        crossings.append(cross)
        points.append(p)
    crossing = np.stack(crossings, axis=1)
    point = np.stack(points, axis=1)
    cut = np.nonzero(crossing.sum(axis=1) == 2)[0]
    if not len(cut):
        return np.zeros((0, 2, 3))
    return np.stack([point[k][crossing[k]] for k in cut])


def project_segments(
    segments: np.ndarray, origin: np.ndarray, normal: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Segments in deterministic minimum-area section coordinates."""
    flat, width, height, _ = _project_segments_with_evidence(segments, origin, normal)
    return flat, width, height


def _canonical_direction(vector: np.ndarray) -> np.ndarray:
    value = _unit(vector)
    anchor = int(np.argmax(np.abs(value)))
    return -value if value[anchor] < 0 else value


def _direction_key(vector: np.ndarray) -> tuple[float, ...]:
    value = _canonical_direction(vector)
    return (
        -round(abs(float(value[0])), 12),
        -round(abs(float(value[1])), 12),
        -round(abs(float(value[2])), 12),
        *[round(float(component), 12) for component in value],
    )


def _project_segments_with_evidence(
    segments: np.ndarray, origin: np.ndarray, normal: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[str]]:
    """Use boundary directions for rotation-invariant oriented section bounds."""
    normal = _unit(normal)
    helper = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = _unit(np.cross(normal, helper))
    v = np.cross(normal, u)

    base = (segments - np.asarray(origin, dtype=np.float64)) @ np.stack([u, v], axis=1)
    points = base.reshape(-1, 2)
    edge = base[:, 1] - base[:, 0]
    lengths = np.linalg.norm(edge, axis=1)
    directions = edge[lengths > _EPS] / lengths[lengths > _EPS, None]
    flags: list[str] = []
    if not len(directions):
        directions = np.asarray([[1.0, 0.0]])
        flags.append("section_boundary_directions_unavailable")

    half_turn = np.pi / 2.0
    angles = np.mod(np.arctan2(directions[:, 1], directions[:, 0]), half_turn)
    angles = np.where(np.isclose(angles, half_turn, atol=1e-12), 0.0, angles)
    unique_angles = np.unique(np.round(angles, 12))
    candidates = []
    for angle in unique_angles:
        first = np.array([np.cos(angle), np.sin(angle)])
        second = np.array([-first[1], first[0]])
        rotation = np.stack([first, second], axis=1)
        projected = points @ rotation
        extents = np.ptp(projected, axis=0)
        area = float(extents[0] * extents[1])
        world_first = first[0] * u + first[1] * v
        candidates.append((area, _direction_key(world_first), first, extents))

    minimum_area = min(candidate[0] for candidate in candidates)
    area_tolerance = max(minimum_area, _EPS) * 1e-9
    best = [candidate for candidate in candidates if candidate[0] <= minimum_area + area_tolerance]
    if len(best) > 1:
        flags.append("section_minimum_bounds_axes_ambiguous")
    _, _, first, extents = min(best, key=lambda candidate: candidate[1])
    second = np.array([-first[1], first[0]])
    extent_tolerance = max(float(extents.max()), _EPS) * 1e-7
    if abs(float(extents[0] - extents[1])) <= extent_tolerance:
        flags.append("section_equal_bounds_axes_ambiguous")
        world_first = first[0] * u + first[1] * v
        world_second = second[0] * u + second[1] * v
        if _direction_key(world_second) < _direction_key(world_first):
            first = second
    elif extents[1] > extents[0]:
        first = second

    width_dir = _canonical_direction(first[0] * u + first[1] * v)
    height_dir = _unit(np.cross(normal, width_dir))
    world_points = segments.reshape(-1, 3)
    center = world_points.mean(axis=0)
    aligned = ((world_points - center) @ np.stack([width_dir, height_dir], axis=1)).reshape(
        -1, 2, 2
    )
    return aligned, width_dir, height_dir, flags


def _segment_arrays(
    flat: np.ndarray, *, tolerance: float = _EPS
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    p = flat[:, 0]
    r = flat[:, 1] - flat[:, 0]
    lengths = np.linalg.norm(r, axis=1)
    keep = lengths > max(tolerance * 1e-3, _EPS)
    return p[keep], r[keep], lengths[keep]


def thickness_samples(
    flat: np.ndarray,
    *,
    tolerance: float = _WELD,
    max_rays: int = _MAX_THICKNESS_RAYS,
) -> tuple[np.ndarray, np.ndarray]:
    """Wall thickness candidates in metres, with boundary-length weights.

    For each segment midpoint, the distance along the segment normal to the
    nearest non-adjacent segment. On a thin-walled section most boundary
    length is paired across the wall, so the length-weighted distribution
    centres on the real plate thicknesses; short tip segments would otherwise
    dominate a coarse tessellation.
    """
    if not np.isfinite(tolerance) or tolerance <= 0:
        raise ValueError("tolerance must be positive")
    if not isinstance(max_rays, int) or isinstance(max_rays, bool) or max_rays < 1:
        raise ValueError("max_rays must be a positive integer")
    p, r, lengths = _segment_arrays(flat, tolerance=tolerance)
    count = len(p)
    if count < 4:
        return np.zeros(0), np.zeros(0)
    if count > max_rays:
        pick = np.random.default_rng(0).choice(count, max_rays, replace=False)
        p, r, lengths = p[pick], r[pick], lengths[pick]
        count = len(p)

    mid = p + r / 2.0
    normals = np.stack([-r[:, 1], r[:, 0]], axis=1) / lengths[:, None]

    po = p[None, :, :] - mid[:, None, :]
    denom = normals[:, None, 0] * r[None, :, 1] - normals[:, None, 1] * r[None, :, 0]
    safe = np.where(np.abs(denom) > _EPS, denom, np.nan)
    s = (po[..., 0] * r[None, :, 1] - po[..., 1] * r[None, :, 0]) / safe
    u = (po[..., 0] * normals[:, None, 1] - po[..., 1] * normals[:, None, 0]) / safe

    ends = np.concatenate([p, p + r], axis=0)
    # adjacency via shared endpoints; rays into a neighbour measure the
    # corner, not the wall
    keys = np.round(ends / tolerance).astype(np.int64)
    start_keys, end_keys = keys[:count], keys[count:]
    adjacent = (
        np.all(start_keys[:, None] == start_keys[None, :], axis=2)
        | np.all(start_keys[:, None] == end_keys[None, :], axis=2)
        | np.all(end_keys[:, None] == start_keys[None, :], axis=2)
        | np.all(end_keys[:, None] == end_keys[None, :], axis=2)
    )

    valid = (
        (u >= 0.0)
        & (u <= 1.0)
        & np.isfinite(s)
        & (np.abs(s) > tolerance)
        & ~adjacent
    )
    hit = np.where(valid, np.abs(s), np.inf)
    best = hit.min(axis=1)
    finite = np.isfinite(best)
    samples = best[finite]
    weights = lengths[finite]
    if not len(samples):
        return samples, weights
    # a wall thicker than half the section span is a cavity crossing
    span = float(np.linalg.norm(flat.reshape(-1, 2).max(axis=0) - flat.reshape(-1, 2).min(axis=0)))
    keep = samples <= max(span * 0.5, _EPS)
    return samples[keep], weights[keep]


def weighted_percentile(samples: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(samples)
    values = samples[order]
    cum = np.cumsum(weights[order])
    cum = (cum - cum[0] / 2.0) / max(float(cum[-1]), _EPS)
    return float(np.interp(q / 100.0, cum, values))


def split_thickness(samples: np.ndarray, weights: np.ndarray) -> dict[str, Any] | None:
    """Two-group split for sections with distinct plate thicknesses.

    Returns the two length-weighted group means (web vs flange style) when
    they are clearly separated, else None.
    """
    if len(samples) < 6:
        return None
    # plates come in families near the median; rays far beyond it crossed a
    # cavity or a junction, not a wall
    cap = 2.5 * weighted_percentile(samples, weights, 50)
    keep = samples <= cap
    samples, weights = samples[keep], weights[keep]
    if len(samples) < 6:
        return None
    order = np.argsort(samples)
    values = samples[order]
    weight = weights[order]
    total = float(weight.sum())
    if total <= _EPS:
        return None
    best: tuple[float, int] | None = None
    for cut in range(2, len(values) - 2):
        low_w, high_w = weight[:cut], weight[cut:]
        if low_w.sum() < 0.1 * total or high_w.sum() < 0.1 * total:
            continue
        low_m = float(np.average(values[:cut], weights=low_w))
        high_m = float(np.average(values[cut:], weights=high_w))
        score = float(
            np.sum(low_w * (values[:cut] - low_m) ** 2)
            + np.sum(high_w * (values[cut:] - high_m) ** 2)
        )
        if best is None or score < best[0]:
            best = (score, cut)
    if best is None:
        return None
    cut = best[1]
    lower = float(np.average(values[:cut], weights=weight[:cut]))
    upper = float(np.average(values[cut:], weights=weight[cut:]))
    if upper - lower < 0.12 * upper:
        return None
    return {
        "lower": {"value": round(lower, 6), "share": round(float(weight[:cut].sum()) / total, 3)},
        "upper": {"value": round(upper, 6), "share": round(float(weight[cut:].sum()) / total, 3)},
    }


def _loops(flat: np.ndarray, *, tolerance: float = _WELD) -> tuple[list[np.ndarray], bool]:
    """Chain segments into loops; closed=False when any chain dead-ends."""
    p, r, _ = _segment_arrays(flat, tolerance=tolerance)
    count = len(p)
    ends = np.concatenate([p, p + r], axis=0)
    keys = [tuple(k) for k in np.round(ends / tolerance).astype(np.int64)]

    by_key: dict[tuple[int, ...], list[int]] = {}
    for idx, key in enumerate(keys):
        by_key.setdefault(key, []).append(idx)

    used = np.zeros(count, dtype=bool)
    loops: list[np.ndarray] = []
    closed = True
    for start in range(count):
        if used[start]:
            continue
        chain = [start]
        used[start] = True
        head_key = keys[start + count]
        while True:
            candidates = [
                e
                for e in by_key.get(head_key, ())
                if not used[e % count] and e % count != chain[-1]
            ]
            if not candidates:
                break
            edge = candidates[0]
            seg = edge % count
            used[seg] = True
            chain.append(seg)
            # continue from the segment's other endpoint
            head_key = keys[seg + count] if edge < count else keys[seg]
        if head_key == keys[start]:
            pts = []
            prev_key = keys[start]
            for seg in chain:
                if keys[seg] == prev_key:
                    pts.append(p[seg])
                    prev_key = keys[seg + count]
                else:
                    pts.append(p[seg] + r[seg])
                    prev_key = keys[seg]
            loops.append(np.asarray(pts))
        else:
            closed = False
    return loops, closed


def _shoelace(loop: np.ndarray) -> float:
    x, y = loop[:, 0], loop[:, 1]
    return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0


def _contains(loop: np.ndarray, point: np.ndarray) -> bool:
    x, y = point
    px, py = loop[:, 0], loop[:, 1]
    qx, qy = np.roll(px, -1), np.roll(py, -1)
    crosses = ((py > y) != (qy > y)) & (
        x < px + (y - py) * (qx - px) / np.where(np.abs(qy - py) > _EPS, qy - py, 1.0)
    )
    return bool(crosses.sum() % 2)


def _loop_area(loops: list[np.ndarray]) -> float:
    """Material area: outer loops add, holes subtract, by containment parity."""
    total = 0.0
    for i, loop in enumerate(loops):
        if len(loop) < 3:
            continue
        depth = sum(
            1
            for j, other in enumerate(loops)
            if j != i and len(other) >= 3 and _contains(other, loop[0])
        )
        sign = 1.0 if depth % 2 == 0 else -1.0
        total += sign * abs(_shoelace(loop))
    return max(total, 0.0)


def _section_properties(loops: list[np.ndarray]) -> dict[str, Any] | None:
    """Material centroid and centroidal second moments in section coordinates."""
    totals = np.zeros(6, dtype=np.float64)
    for index, loop in enumerate(loops):
        if len(loop) < 3:
            continue
        x, y = loop[:, 0], loop[:, 1]
        nx, ny = np.roll(x, -1), np.roll(y, -1)
        cross = x * ny - nx * y
        signed_area = float(cross.sum()) / 2.0
        if abs(signed_area) <= _EPS:
            continue
        centroid_x = float(((x + nx) * cross).sum()) / (6.0 * signed_area)
        centroid_y = float(((y + ny) * cross).sum()) / (6.0 * signed_area)
        orientation = 1.0 if signed_area > 0.0 else -1.0
        i_xx = orientation * float(
            (cross * (y * y + y * ny + ny * ny)).sum()
        ) / 12.0
        i_yy = orientation * float(
            (cross * (x * x + x * nx + nx * nx)).sum()
        ) / 12.0
        i_xy = orientation * float(
            (
                cross
                * (2.0 * x * y + x * ny + nx * y + 2.0 * nx * ny)
            ).sum()
        ) / 24.0
        depth = sum(
            1
            for other_index, other in enumerate(loops)
            if other_index != index and len(other) >= 3 and _contains(other, loop[0])
        )
        material_sign = 1.0 if depth % 2 == 0 else -1.0
        area = material_sign * abs(signed_area)
        totals += np.asarray(
            [area, area * centroid_x, area * centroid_y, material_sign * i_xx,
             material_sign * i_yy, material_sign * i_xy]
        )
    area, first_x, first_y, i_xx_origin, i_yy_origin, i_xy_origin = totals
    if area <= _EPS:
        return None
    centroid_x, centroid_y = first_x / area, first_y / area
    return {
        "centroid_2d": [round(float(centroid_x), 9), round(float(centroid_y), 9)],
        "second_moments_si4": {
            "i_xx": round(max(float(i_xx_origin - area * centroid_y**2), 0.0), 12),
            "i_yy": round(max(float(i_yy_origin - area * centroid_x**2), 0.0), 12),
            "i_xy": round(float(i_xy_origin - area * centroid_x * centroid_y), 12),
        },
    }


def section_metrics(
    verts: np.ndarray,
    faces: np.ndarray,
    origin: np.ndarray,
    normal: np.ndarray,
    *,
    include_outline: bool = False,
    outline_points: int = 160,
    tolerance: float = _WELD,
    max_thickness_rays: int = _MAX_THICKNESS_RAYS,
) -> dict[str, Any] | None:
    """All 2D metrics for one plane cut, or None when the plane misses."""
    segments = slice_segments(verts, faces, origin, normal, tolerance=tolerance)
    if not len(segments):
        return None
    flat, width_dir, height_dir, frame_flags = _project_segments_with_evidence(
        segments, origin, normal
    )
    pts = flat.reshape(-1, 2)
    low = pts.min(axis=0)
    high = pts.max(axis=0)
    lengths = np.linalg.norm(flat[:, 1] - flat[:, 0], axis=1)
    perimeter = float(lengths.sum())

    samples, weights = thickness_samples(
        flat, tolerance=tolerance, max_rays=max_thickness_rays
    )
    thickness: dict[str, Any] | None = None
    if len(samples):
        thickness = {
            "samples": int(len(samples)),
            "min": round(float(samples.min()), 6),
            "p25": round(weighted_percentile(samples, weights, 25), 6),
            "median": round(weighted_percentile(samples, weights, 50), 6),
            "p75": round(weighted_percentile(samples, weights, 75), 6),
            "max": round(float(samples.max()), 6),
        }
        pair = split_thickness(samples, weights)
        if pair:
            thickness["pair"] = pair
            thickness["modes"] = [
                {
                    "value_si": entry["value"],
                    "share": entry["share"],
                    "sample_count": int(round(len(samples) * entry["share"])),
                    "method": "section_boundary_normal_sampling",
                }
                for entry in pair.values()
            ]
        else:
            thickness["modes"] = [
                {
                    "value_si": thickness["median"],
                    "variation_si": round(thickness["p75"] - thickness["p25"], 6),
                    "share": 1.0,
                    "sample_count": int(len(samples)),
                    "method": "section_boundary_normal_sampling",
                }
            ]

    loops, closed = _loops(flat, tolerance=tolerance)
    area = round(_loop_area(loops), 9) if loops and closed else None
    properties = _section_properties(loops) if loops and closed else None
    loop_depths = [
        sum(
            1
            for other_index, other in enumerate(loops)
            if other_index != index and len(other) >= 3 and _contains(other, loop[0])
        )
        for index, loop in enumerate(loops)
        if len(loop) >= 3
    ]

    result: dict[str, Any] = {
        "width": round(float(high[0] - low[0]), 6),
        "height": round(float(high[1] - low[1]), 6),
        "width_direction": [round(float(c), 6) for c in width_dir],
        "height_direction": [round(float(c), 6) for c in height_dir],
        "perimeter": round(perimeter, 6),
        "area": area,
        "closed": closed,
        "segments": int(len(flat)),
        "loop_count": len(loops),
        "hole_count": sum(depth % 2 == 1 for depth in loop_depths),
        "material_region_count": sum(depth % 2 == 0 for depth in loop_depths),
        "frame_flags": frame_flags,
        "effective_tolerance_si": round(float(tolerance), 12),
        "thickness_ray_budget": max_thickness_rays,
        "thickness": thickness,
        # The 2D outline is centred here. Together with the two directions it
        # can be reconstructed in world space without shipping mesh arrays.
        "outline_frame": {
            "origin": [
                round(float(value), 9) for value in segments.reshape(-1, 3).mean(axis=0)
            ],
            "x_direction": [round(float(value), 9) for value in width_dir],
            "y_direction": [round(float(value), 9) for value in height_dir],
        },
    }
    if properties is not None:
        result.update(properties)
        centroid = np.asarray(properties["centroid_2d"], dtype=np.float64)
        world_centroid = (
            np.asarray(result["outline_frame"]["origin"], dtype=np.float64)
            + centroid[0] * width_dir
            + centroid[1] * height_dir
        )
        result["centroid_world"] = [round(float(value), 9) for value in world_centroid]
    if include_outline:
        outline = []
        budget = max(outline_points, 12)
        for loop in loops if loops else []:
            step = max(1, int(np.ceil(len(loop) / max(budget // max(len(loops), 1), 4))))
            outline.append([[round(float(x), 6), round(float(y), 6)] for x, y in loop[::step]])
        result["outline"] = outline
    return result


def _section_descriptor(metrics: dict[str, Any]) -> dict[str, Any]:
    thickness = metrics.get("thickness") or {}
    return {
        "area_si": metrics.get("area"),
        "perimeter_si": metrics.get("perimeter"),
        "width_si": metrics.get("width"),
        "height_si": metrics.get("height"),
        "loop_count": metrics.get("loop_count", 0),
        "hole_count": metrics.get("hole_count", 0),
        "thickness_modes_si": [
            mode["value_si"] for mode in thickness.get("modes") or ()
        ],
    }


def _descriptor_delta(
    left: dict[str, Any], right: dict[str, Any], *, relative_tolerance: float
) -> float:
    if left["loop_count"] != right["loop_count"] or left["hole_count"] != right["hole_count"]:
        return 1.0
    changes = []
    for name in ("area_si", "perimeter_si", "width_si", "height_si"):
        a, b = left.get(name), right.get(name)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            if a != b:
                return 1.0
            continue
        scale = max(abs(float(a)), abs(float(b)), _EPS)
        changes.append(abs(float(a) - float(b)) / scale)
    modes_left, modes_right = left["thickness_modes_si"], right["thickness_modes_si"]
    if len(modes_left) != len(modes_right):
        return 1.0
    for a, b in zip(modes_left, modes_right, strict=True):
        scale = max(abs(float(a)), abs(float(b)), _EPS)
        changes.append(abs(float(a) - float(b)) / scale)
    delta = max(changes, default=0.0)
    return 0.0 if delta <= relative_tolerance else delta


def adaptive_sections(
    verts: np.ndarray,
    faces: np.ndarray,
    axis: np.ndarray,
    *,
    relative_tolerance: float = 0.02,
    absolute_tolerance: float = _WELD,
    max_stations: int = 17,
    max_thickness_rays: int = _MAX_THICKNESS_RAYS,
    include_outline: bool = False,
    outline_points: int = 160,
) -> dict[str, Any]:
    """Bounded adaptive sections and constant-profile station regions."""
    if max_stations < 3:
        raise ValueError("max_stations must be at least 3")
    if (
        not isinstance(max_thickness_rays, int)
        or isinstance(max_thickness_rays, bool)
        or max_thickness_rays < 1
    ):
        raise ValueError("max_thickness_rays must be a positive integer")
    if not np.isfinite(relative_tolerance) or relative_tolerance <= 0:
        raise ValueError("relative_tolerance must be positive")
    if not np.isfinite(absolute_tolerance) or absolute_tolerance <= 0:
        raise ValueError("absolute_tolerance must be positive")
    vertices = np.asarray(verts, dtype=np.float64)
    direction = _unit(axis)
    centre = vertices.mean(axis=0)
    projection = (vertices - centre) @ direction
    low, high = float(projection.min()), float(projection.max())
    span = high - low
    if span <= _EPS:
        return {
            "strategy": "auto",
            "stations_evaluated": 0,
            "stations": [],
            "profile_regions": [],
            "representative_sections": {},
            "variation": "unavailable",
            "absolute_tolerance_si": round(float(absolute_tolerance), 12),
            "station_budget": max_stations,
            "thickness_ray_budget": max_thickness_rays,
            "flags": ["zero_axis_extent"],
        }

    samples: dict[float, dict[str, Any] | None] = {}

    def evaluate(at: float) -> None:
        at = round(float(at), 8)
        if at in samples or len(samples) >= max_stations:
            return
        origin = centre + direction * (low + at * span)
        metrics = section_metrics(
            vertices,
            faces,
            origin,
            direction,
            include_outline=False,
            tolerance=absolute_tolerance,
            max_thickness_rays=max_thickness_rays,
        )
        samples[at] = metrics

    seeds = (0.02, 0.1, 0.25, 0.5, 0.75, 0.9, 0.98)
    for station in seeds[:max_stations]:
        evaluate(station)

    while len(samples) < max_stations:
        valid = sorted((at, value) for at, value in samples.items() if value is not None)
        candidates: list[tuple[float, float]] = []
        for (left_at, left), (right_at, right) in zip(valid, valid[1:], strict=False):
            gap = right_at - left_at
            if gap <= 0.025:
                continue
            delta = _descriptor_delta(
                _section_descriptor(left),
                _section_descriptor(right),
                relative_tolerance=relative_tolerance,
            )
            if delta > 0:
                candidates.append((delta * gap, (left_at + right_at) / 2.0))
        if not candidates:
            break
        for _, station in sorted(candidates, reverse=True):
            if len(samples) >= max_stations:
                break
            evaluate(station)

    valid = sorted((at, value) for at, value in samples.items() if value is not None)
    stations: list[dict[str, Any]] = []
    for at, metrics in valid:
        assert metrics is not None
        stations.append({"at": at, "descriptor": _section_descriptor(metrics), **metrics})

    groups: list[list[dict[str, Any]]] = []
    for station in stations:
        if not groups:
            groups.append([station])
            continue
        delta = _descriptor_delta(
            groups[-1][-1]["descriptor"],
            station["descriptor"],
            relative_tolerance=relative_tolerance,
        )
        if delta == 0:
            groups[-1].append(station)
        else:
            groups.append([station])

    regions = []
    for index, group in enumerate(groups):
        start = 0.0 if index == 0 else (groups[index - 1][-1]["at"] + group[0]["at"]) / 2.0
        end = 1.0 if index == len(groups) - 1 else (
            group[-1]["at"] + groups[index + 1][0]["at"]
        ) / 2.0
        representative = group[len(group) // 2]
        modes = []
        for mode in (representative.get("thickness") or {}).get("modes") or ():
            modes.append({**mode, "station_range": [round(start, 6), round(end, 6)]})
        regions.append(
            {
                "start": round(start, 6),
                "end": round(end, 6),
                "representative_station": representative["at"],
                "sample_count": len(group),
                "descriptor": representative["descriptor"],
                "thickness_modes": modes,
            }
        )

    representatives: dict[str, Any] = {}
    if stations:
        dominant_region = max(regions, key=lambda item: item["end"] - item["start"])
        by_station = {item["at"]: item for item in stations}
        dominant = by_station[dominant_region["representative_station"]]
        available_area = [item for item in stations if isinstance(item.get("area"), (int, float))]
        minimum = min(available_area, key=lambda item: item["area"]) if available_area else stations[0]
        maximum = max(available_area, key=lambda item: item["area"]) if available_area else stations[-1]

        def shaped(item: dict[str, Any]) -> dict[str, Any]:
            if include_outline and "outline" not in item:
                origin = centre + direction * (low + item["at"] * span)
                detailed = section_metrics(
                    vertices,
                    faces,
                    origin,
                    direction,
                    include_outline=True,
                    outline_points=outline_points,
                    tolerance=absolute_tolerance,
                    max_thickness_rays=max_thickness_rays,
                )
                if detailed is not None:
                    return {"at": item["at"], "descriptor": item["descriptor"], **detailed}
            return item

        transitions = [shaped(group[0]) for group in groups[1:]][:6]
        representatives = {
            "dominant": shaped(dominant),
            "minimum": shaped(minimum),
            "maximum": shaped(maximum),
            "transitions": transitions,
        }

    areas = [item.get("area") for item in stations]
    numeric_areas = [float(value) for value in areas if isinstance(value, (int, float))]
    if len(groups) <= 1:
        variation = "constant"
    elif len(numeric_areas) >= 3 and (
        all(a <= b for a, b in zip(numeric_areas, numeric_areas[1:], strict=False))
        or all(a >= b for a, b in zip(numeric_areas, numeric_areas[1:], strict=False))
    ):
        variation = "tapered"
    elif len(groups) <= max(3, len(stations) // 3):
        variation = "piecewise_constant"
    else:
        variation = "variable"

    flags = []
    if len(samples) >= max_stations:
        flags.append("station_budget_reached")
    if len(valid) != len(samples):
        flags.append("some_stations_missed_mesh")
    return {
        "strategy": "auto",
        "stations_evaluated": len(samples),
        "stations": stations,
        "profile_regions": regions,
        "representative_sections": representatives,
        "variation": variation,
        "relative_change_tolerance": relative_tolerance,
        "absolute_tolerance_si": round(float(absolute_tolerance), 12),
        "station_budget": max_stations,
        "thickness_ray_budget": max_thickness_rays,
        "seed_stations": min(len(seeds), max_stations),
        "adaptive_refinements": max(0, len(samples) - min(len(seeds), max_stations)),
        "stations_missed": len(samples) - len(valid),
        "axis_extent_si": round(span, 9),
        "flags": flags,
    }


__all__ = [
    "adaptive_sections",
    "project_segments",
    "section_metrics",
    "slice_segments",
    "split_thickness",
    "thickness_samples",
    "weighted_percentile",
]
