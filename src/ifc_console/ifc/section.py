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
    verts: np.ndarray, faces: np.ndarray, origin: np.ndarray, normal: np.ndarray
) -> np.ndarray:
    """Cut segments (n, 2, 3) where the plane meets the mesh."""
    normal = _unit(normal)
    distances = (verts - origin) @ normal
    # nudge on-plane vertices so every crossing is a clean sign change
    distances = np.where(np.abs(distances) < 1e-12, 1e-12, distances)
    tri = distances[faces]

    crossings = []
    points = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        di, dj = tri[:, i], tri[:, j]
        cross = di * dj < 0
        t = di / np.where(np.abs(di - dj) > _EPS, di - dj, 1.0)
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
    """Segments in plane coordinates aligned to the section's principal axes.

    Returns (flat segments (n, 2, 2), width direction 3D, height direction 3D)
    where width is the major principal axis of the cut.
    """
    normal = _unit(normal)
    helper = np.array([1.0, 0.0, 0.0]) if abs(normal[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = _unit(np.cross(normal, helper))
    v = np.cross(normal, u)

    flat = (segments - np.asarray(origin, dtype=np.float64)) @ np.stack([u, v], axis=1)
    pts = flat.reshape(-1, 2)
    center = pts.mean(axis=0)
    spread = pts - center
    _, vecs = np.linalg.eigh(spread.T @ spread)
    major, minor = vecs[:, 1], vecs[:, 0]
    rotation = np.stack([major, minor], axis=1)
    aligned = ((pts - center) @ rotation).reshape(-1, 2, 2)
    width_dir = major[0] * u + major[1] * v
    height_dir = minor[0] * u + minor[1] * v
    return aligned, width_dir, height_dir


def _segment_arrays(flat: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    p = flat[:, 0]
    r = flat[:, 1] - flat[:, 0]
    lengths = np.linalg.norm(r, axis=1)
    keep = lengths > _EPS
    return p[keep], r[keep], lengths[keep]


def thickness_samples(flat: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Wall thickness candidates in metres, with boundary-length weights.

    For each segment midpoint, the distance along the segment normal to the
    nearest non-adjacent segment. On a thin-walled section most boundary
    length is paired across the wall, so the length-weighted distribution
    centres on the real plate thicknesses; short tip segments would otherwise
    dominate a coarse tessellation.
    """
    p, r, lengths = _segment_arrays(flat)
    count = len(p)
    if count < 4:
        return np.zeros(0), np.zeros(0)
    if count > _MAX_THICKNESS_RAYS:
        pick = np.random.default_rng(0).choice(count, _MAX_THICKNESS_RAYS, replace=False)
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
    keys = np.round(ends / _WELD).astype(np.int64)
    start_keys, end_keys = keys[:count], keys[count:]
    adjacent = (
        np.all(start_keys[:, None] == start_keys[None, :], axis=2)
        | np.all(start_keys[:, None] == end_keys[None, :], axis=2)
        | np.all(end_keys[:, None] == start_keys[None, :], axis=2)
        | np.all(end_keys[:, None] == end_keys[None, :], axis=2)
    )

    valid = (u >= 0.0) & (u <= 1.0) & np.isfinite(s) & (np.abs(s) > _WELD) & ~adjacent
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


def _loops(flat: np.ndarray) -> tuple[list[np.ndarray], bool]:
    """Chain segments into loops; closed=False when any chain dead-ends."""
    p, r, _ = _segment_arrays(flat)
    count = len(p)
    ends = np.concatenate([p, p + r], axis=0)
    keys = [tuple(k) for k in np.round(ends / _WELD).astype(np.int64)]

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


def section_metrics(
    verts: np.ndarray,
    faces: np.ndarray,
    origin: np.ndarray,
    normal: np.ndarray,
    *,
    include_outline: bool = False,
    outline_points: int = 160,
) -> dict[str, Any] | None:
    """All 2D metrics for one plane cut, or None when the plane misses."""
    segments = slice_segments(verts, faces, origin, normal)
    if not len(segments):
        return None
    flat, width_dir, height_dir = project_segments(segments, origin, normal)
    pts = flat.reshape(-1, 2)
    low = pts.min(axis=0)
    high = pts.max(axis=0)
    lengths = np.linalg.norm(flat[:, 1] - flat[:, 0], axis=1)
    perimeter = float(lengths.sum())

    samples, weights = thickness_samples(flat)
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

    loops, closed = _loops(flat)
    area = round(_loop_area(loops), 9) if loops and closed else None

    result: dict[str, Any] = {
        "width": round(float(high[0] - low[0]), 6),
        "height": round(float(high[1] - low[1]), 6),
        "width_direction": [round(float(c), 6) for c in width_dir],
        "height_direction": [round(float(c), 6) for c in height_dir],
        "perimeter": round(perimeter, 6),
        "area": area,
        "closed": closed,
        "segments": int(len(flat)),
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
    if include_outline:
        outline = []
        budget = max(outline_points, 12)
        for loop in loops if loops else []:
            step = max(1, int(np.ceil(len(loop) / max(budget // max(len(loops), 1), 4))))
            outline.append([[round(float(x), 6), round(float(y), 6)] for x, y in loop[::step]])
        result["outline"] = outline
    return result


__all__ = [
    "project_segments",
    "section_metrics",
    "slice_segments",
    "split_thickness",
    "thickness_samples",
    "weighted_percentile",
]
