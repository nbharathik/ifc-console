"""Turn viewer measurements into a saved agent skill.

A recording stores intent, not coordinates. Each measured value is matched
against the exemplar element's analyzed dimensions so the skill names what
was measured, and the generated steps tell an agent how to compute the same
intent on similar elements, including ones with a different shape.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from ifc_console.ifc.similarity import build_geometry_signature

from ifc_console_agents.skills import (
    MeasurementApplicability,
    MeasurementExemplar,
    MeasurementExemplarObject,
    MeasurementIntent,
    MeasurementModelRevision,
    MeasurementObjectRole,
    MeasurementRule,
    MeasurementSkillSpec,
    MeasurementTolerance,
    measurement_spec_block,
)

_MAX_ROWS = 40
_MAX_ELEMENTS = 12
_DEFAULT_REL_TOL = 0.02
_SCALE_REL_TOL = 1e-5

_AXIS_NAMES = ("x", "y", "z")
_LEGACY_MEASUREMENT_IDS = {
    "length": "envelope.overall_length",
    "width": "envelope.overall_width",
    "height": "envelope.overall_height",
    "wall_thickness": "profile.wall_thickness",
    "web_thickness": "profile.web_thickness",
    "flange_thickness": "profile.flange_thickness",
    "area": "section.area",
    "perimeter": "section.perimeter",
}
_RULE_FALLBACKS = {
    "envelope": ("directional_extent",),
    "profile": ("adaptive_section.thickness_modes",),
    "section": ("adaptive_section",),
    "material": ("material_layer_analysis",),
}


@dataclass
class RecordedSkill:
    content: str
    description: str
    applies_to: str | None
    spec: MeasurementSkillSpec
    classes: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    guids: tuple[str, ...] = field(default=())
    unresolved_intents: tuple[int, ...] = field(default=())


@dataclass(frozen=True)
class _MeasurementMatch:
    measurement_id: str
    legacy_key: str | None
    source: str
    frame: str
    direction: str | None
    confidence: str
    delta_si: float
    tolerance_si: float
    candidate_outputs: tuple[str, ...]
    ambiguous: bool = False


def _fmt(value: Any, unit: str = "m") -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{text or '0'} {unit}".strip()


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def measured_guids(items: list[dict[str, Any]]) -> list[str]:
    """Distinct GlobalIds a measurement set touched, in first-seen order."""
    seen: dict[str, None] = {}
    for item in items:
        for anchor in item.get("anchors") or []:
            guid = anchor.get("guid") if isinstance(anchor, dict) else None
            if isinstance(guid, str) and guid:
                seen.setdefault(guid, None)
        guid = item.get("guid")
        if isinstance(guid, str) and guid:
            seen.setdefault(guid, None)
        axes = item.get("axes")
        if isinstance(axes, dict):
            for axis in axes.values():
                if not isinstance(axis, dict):
                    continue
                for side in ("negative", "positive"):
                    hit = axis.get(side)
                    if isinstance(hit, dict) and isinstance(hit.get("guid"), str):
                        seen.setdefault(hit["guid"], None)
    return list(seen)


def _object_info(record: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(record, dict):
        return {}
    nested = record.get("object")
    return nested if isinstance(nested, dict) else record


def _record_guid(record: dict[str, Any]) -> str | None:
    value = _object_info(record).get("global_id") or record.get("global_id")
    return value if isinstance(value, str) and value else None


def _element_index(analysis: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(analysis, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for record in analysis.get("elements") or []:
        if isinstance(record, dict):
            guid = _record_guid(record)
            if guid:
                out[guid] = record
    return out


def _describe(record: dict[str, Any] | None, guid: str) -> str:
    if not record:
        return guid
    identity = _object_info(record)
    label = identity.get("class") or record.get("class") or "element"
    name = identity.get("name") or record.get("name")
    return f"{label} '{name}' ({guid})" if name else f"{label} ({guid})"


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _vector(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    values = tuple(_finite(part) for part in value)
    if any(part is None for part in values):
        return None
    return tuple(float(part) for part in values)  # type: ignore[arg-type]


def _unit_vector(value: Any) -> tuple[float, float, float] | None:
    raw = _vector(value)
    if raw is None:
        return None
    length = math.sqrt(sum(part * part for part in raw))
    if length <= 1e-12:
        return None
    return tuple(round(part / length, 9) for part in raw)


def _measurement_records(record: dict[str, Any] | None) -> list[dict[str, Any]]:
    """V2 inventory first, then namespaced compatibility views for legacy probes."""
    if not isinstance(record, dict):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in record.get("measurements") or ():
        if not isinstance(raw, dict) or not isinstance(raw.get("id"), str):
            continue
        value = _finite(raw.get("value_si"))
        if value is None:
            continue
        row = dict(raw)
        row["value_si"] = value
        rows.append(row)
        seen.add(raw["id"])
    dims = record.get("dimensions")
    if isinstance(dims, dict):
        for key, dim in dims.items():
            measurement_id = _LEGACY_MEASUREMENT_IDS.get(str(key))
            if measurement_id is None or measurement_id in seen or not isinstance(dim, dict):
                continue
            value = _finite(dim.get("si"))
            if value is None:
                continue
            rows.append(
                {
                    "id": measurement_id,
                    "legacy_key": str(key),
                    "value_si": value,
                    "source": str(dim.get("source") or "legacy_dimension"),
                    "method": "legacy_compatibility_view",
                    "frame": "semantic",
                    "confidence": str(dim.get("confidence") or "medium"),
                    "flags": ["legacy_measurement_mapping"],
                }
            )
            seen.add(measurement_id)
    return rows


def _element_scale(record: dict[str, Any] | None) -> float:
    if not isinstance(record, dict):
        return 1.0
    values: list[float] = []
    box = record.get("box") or {}
    extents = box.get("local_extents") if isinstance(box, dict) else None
    if isinstance(extents, dict):
        values.extend(
            value for value in (_finite(item) for item in extents.values()) if value is not None
        )
    signature = record.get("geometry_signature")
    if isinstance(signature, dict):
        envelope = _finite(signature.get("object_envelope_si"))
        if envelope is not None:
            values.append(envelope)
    for measurement in _measurement_records(record):
        if str(measurement.get("id", "")).startswith("envelope."):
            value = _finite(measurement.get("value_si"))
            if value is not None:
                values.append(abs(value))
    return max(values, default=1.0)


def _reported_tolerance(
    record: dict[str, Any] | None, measurement: dict[str, Any]
) -> tuple[float, float]:
    absolute_values = [_finite(measurement.get("uncertainty_si"))]
    relative_values: list[float | None] = []
    for candidate in (
        measurement.get("tolerance"),
        (record or {}).get("tolerance"),
        (record or {}).get("tolerance_policy"),
    ):
        if not isinstance(candidate, dict):
            continue
        absolute_values.append(
            _finite(candidate.get("absolute_si") or candidate.get("effective_absolute_si"))
        )
        relative_values.append(_finite(candidate.get("relative")))
    scale_absolute = _element_scale(record) * _SCALE_REL_TOL
    absolute = max(
        [scale_absolute]
        + [abs(value) for value in absolute_values if value is not None and value >= 0]
    )
    relative = max(
        [value for value in relative_values if value is not None and value >= 0],
        default=_DEFAULT_REL_TOL,
    )
    return absolute, relative


def _semantic_axes(record: dict[str, Any] | None) -> dict[str, tuple[float, float, float]]:
    if not isinstance(record, dict):
        return {}
    frames = record.get("frames")
    semantic = frames.get("semantic") if isinstance(frames, dict) else None
    if not isinstance(semantic, dict):
        return {}
    nested = semantic.get("axes")
    source = nested if isinstance(nested, dict) else semantic
    out: dict[str, tuple[float, float, float]] = {}
    for name in ("longitudinal", "transverse", "vertical"):
        vector = _unit_vector(source.get(name))
        if vector is not None:
            out[name] = vector
    return out


def _distance_vectors(
    item: dict[str, Any], record: dict[str, Any] | None, guid: str | None
) -> tuple[str | None, tuple[float, float, float] | None]:
    anchors = [anchor for anchor in item.get("anchors") or () if isinstance(anchor, dict)]
    local_points = []
    for anchor in anchors:
        if guid and anchor.get("guid") not in (None, guid):
            continue
        point = _vector(anchor.get("local"))
        if point is not None:
            local_points.append(point)
    local_direction = None
    if len(local_points) >= 2:
        local_direction = _unit_vector(
            [local_points[-1][index] - local_points[0][index] for index in range(3)]
        )

    semantic_direction = None
    world_direction = _unit_vector(item.get("delta"))
    axes = _semantic_axes(record)
    if world_direction is not None and axes:
        scores = {
            name: abs(sum(world_direction[index] * axis[index] for index in range(3)))
            for name, axis in axes.items()
        }
        best = max(scores, key=scores.get)
        if scores[best] >= 0.9:
            semantic_direction = best
            if local_direction is None:
                local_direction = tuple(
                    round(
                        sum(world_direction[index] * axes[name][index] for index in range(3)),
                        9,
                    )
                    for name in ("longitudinal", "transverse", "vertical")
                    if name in axes
                )
                if len(local_direction) != 3:
                    local_direction = None
    return semantic_direction, local_direction


def _match_measurement(
    value: float,
    record: dict[str, Any] | None,
    *,
    semantic_direction: str | None = None,
    expected_id: str | None = None,
) -> _MeasurementMatch | None:
    scored: list[tuple[float, float, dict[str, Any]]] = []
    for measurement in _measurement_records(record):
        candidate = float(measurement["value_si"])
        absolute, relative = _reported_tolerance(record, measurement)
        tolerance = absolute + relative * max(abs(value), abs(candidate))
        delta = abs(value - candidate)
        if delta > tolerance:
            continue
        direction = measurement.get("direction")
        if semantic_direction and direction and direction != semantic_direction:
            continue
        score = delta / max(tolerance, 1e-15)
        if expected_id and measurement.get("id") != expected_id:
            score += 0.35
        if semantic_direction and direction == semantic_direction:
            score -= 0.1
        scored.append((score, tolerance, measurement))
    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], str(item[2].get("id"))))
    best_score, tolerance, best = scored[0]
    candidate_outputs = tuple(dict.fromkeys(str(item[2]["id"]) for item in scored[:8]))
    ambiguous = len(scored) > 1 and abs(scored[1][0] - best_score) <= 0.1
    return _MeasurementMatch(
        measurement_id=str(best["id"]),
        legacy_key=str(best.get("legacy_key")) if best.get("legacy_key") else None,
        source=str(best.get("source") or best.get("method") or "analysis"),
        frame=str(best.get("frame") or "semantic"),
        direction=str(best.get("direction")) if best.get("direction") else semantic_direction,
        confidence=str(best.get("confidence") or "medium"),
        delta_si=abs(value - float(best["value_si"])),
        tolerance_si=tolerance,
        candidate_outputs=candidate_outputs,
        ambiguous=ambiguous,
    )


def _match_dimension(value: float, record: dict[str, Any] | None) -> tuple[str, str] | None:
    """Compatibility wording for the best scale-aware analyzed match."""
    match = _match_measurement(value, record)
    if match is None or match.ambiguous:
        return None
    return (match.legacy_key or match.measurement_id, match.source)


def _distance_axis(item: dict[str, Any]) -> str | None:
    axis = item.get("axis")
    if isinstance(axis, str) and axis in _AXIS_NAMES:
        return axis
    delta = item.get("delta")
    distance = item.get("distance")
    if not isinstance(delta, list) or len(delta) != 3 or not isinstance(distance, (int, float)):
        return None
    spans = [abs(component) for component in delta]
    dominant = max(range(3), key=lambda index: spans[index])
    if distance > 0 and spans[dominant] >= 0.95 * float(distance):
        return _AXIS_NAMES[dominant]
    return None


def _anchor_guids(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for anchor in item.get("anchors") or []:
        guid = anchor.get("guid") if isinstance(anchor, dict) else None
        if isinstance(guid, str) and guid and guid not in out:
            out.append(guid)
    return out


def _relationship_roles(item: dict[str, Any]) -> tuple[MeasurementObjectRole, ...]:
    """Keep endpoint identity and object-local anchors, never world coordinates."""
    roles: list[MeasurementObjectRole] = []
    anchors = item.get("anchors") or ()
    if not isinstance(anchors, (list, tuple)):
        return ()
    for index, role in enumerate(("from", "to")):
        if index >= len(anchors) or not isinstance(anchors[index], dict):
            continue
        anchor = anchors[index]
        guid = anchor.get("guid")
        if not isinstance(guid, str) or not guid:
            continue
        reach = _finite(anchor.get("reach"))
        roles.append(
            MeasurementObjectRole(
                role=role,
                global_id=guid,
                anchor_index=index,
                local_point=_vector(anchor.get("local")),
                reach_si=reach if reach is not None and reach >= 0.0 else None,
            )
        )
    return tuple(roles)


def _snap_kinds(item: dict[str, Any]) -> tuple[str, ...]:
    values: list[str] = []
    for value in item.get("ends") or ():
        if isinstance(value, str) and value and value not in values:
            values.append(value[:80])
    for anchor in item.get("anchors") or ():
        if not isinstance(anchor, dict):
            continue
        for key in ("snap_kind", "snap", "kind", "feature"):
            value = anchor.get(key)
            if isinstance(value, str) and value and value not in values:
                values.append(value[:80])
    return tuple(values[:12])


def _confidence(value: str | None) -> str:
    return value if value in {"low", "medium", "high", "exact"} else "medium"


def _frame(value: str | None) -> str:
    return (
        value if value in {"semantic", "placement", "principal", "local", "world"} else "semantic"
    )


def _fallbacks(measurement_id: str) -> tuple[str, ...]:
    family = measurement_id.partition(".")[0]
    return _RULE_FALLBACKS.get(family, ())


def _intent(
    *,
    item: dict[str, Any],
    index: int,
    record: dict[str, Any] | None,
    guid: str | None,
    value_si: float | None,
    relationship: str,
    match: _MeasurementMatch | None = None,
) -> MeasurementIntent:
    semantic_direction, local_direction = _distance_vectors(item, record, guid)
    world_axis = _distance_axis(item)
    object_roles = _relationship_roles(item) if relationship == "between_objects" else ()
    matched_by: list[str] = []
    if object_roles:
        matched_by.append("ordered_object_roles")
    if match is not None:
        matched_by.extend(("scale_aware_value", "stable_measurement_id"))
        if semantic_direction and match.direction == semantic_direction:
            matched_by.append("semantic_direction")
        if local_direction is not None:
            matched_by.append("object_local_anchors")
    label = item.get("label")
    return MeasurementIntent(
        viewer_kind=(
            str(item.get("kind"))
            if item.get("kind") in {"distance", "dimensions", "area", "path", "angle", "laser"}
            else "unknown"
        ),
        viewer_index=index,
        label=label.strip()[:200] if isinstance(label, str) and label.strip() else None,
        value_si=value_si,
        semantic_direction=semantic_direction,
        local_direction=local_direction,
        world_axis=world_axis,
        snap_kinds=_snap_kinds(item),
        object_roles=object_roles,
        anchor_relationship=relationship,
        matched_by=tuple(matched_by),
        candidate_outputs=match.candidate_outputs if match else (),
        match_delta_si=match.delta_si if match else None,
        match_tolerance_si=match.tolerance_si if match else None,
    )


def _distance_rule(
    item: dict[str, Any], index: int, elements: dict[str, dict[str, Any]]
) -> MeasurementRule:
    guids = _anchor_guids(item)
    value = _finite(item.get("distance"))
    if len(guids) != 1 or value is None:
        relationship = "between_objects" if len(guids) >= 2 else "unanchored"
        return MeasurementRule(
            output=None,
            rule_type="relationship" if len(guids) >= 2 else "object_measurement",
            unresolved=True,
            frame="local",
            minimum_confidence="medium",
            tolerance=MeasurementTolerance(relative=_DEFAULT_REL_TOL),
            intent=_intent(
                item=item,
                index=index,
                record=elements.get(guids[0]) if guids else None,
                guid=guids[0] if guids else None,
                value_si=value,
                relationship=relationship,
            ),
        )
    record = elements.get(guids[0])
    semantic_direction, _ = _distance_vectors(item, record, guids[0])
    match = _match_measurement(value, record, semantic_direction=semantic_direction)
    unresolved = match is None or match.ambiguous
    tolerance = (
        MeasurementTolerance(absolute_si=match.tolerance_si, relative=0.0)
        if match
        else MeasurementTolerance(
            absolute_si=_element_scale(record) * _SCALE_REL_TOL,
            relative=_DEFAULT_REL_TOL,
        )
    )
    return MeasurementRule(
        output=None if unresolved else match.measurement_id,
        rule_type="object_measurement",
        preferred_sources=(match.source,) if match else (),
        fallbacks=_fallbacks(match.measurement_id) if match else (),
        frame=_frame(match.frame if match else "semantic"),
        direction=match.direction if match else semantic_direction,
        minimum_confidence=_confidence(match.confidence if match else None),
        tolerance=tolerance,
        unresolved=unresolved,
        intent=_intent(
            item=item,
            index=index,
            record=record,
            guid=guids[0],
            value_si=value,
            relationship="same_object",
            match=match,
        ),
    )


def _generic_rules(
    item: dict[str, Any], index: int, elements: dict[str, dict[str, Any]]
) -> list[MeasurementRule]:
    kind = str(item.get("kind") or "unknown")
    guids = _anchor_guids(item)
    target = item.get("guid")
    guid = target if isinstance(target, str) and target else (guids[0] if guids else None)
    record = elements.get(guid) if guid else None
    relationship = "element_record" if guid else "unanchored"
    if kind == "dimensions":
        rules = []
        for key in ("length", "width", "thickness"):
            value = _finite(item.get(key))
            if value is None:
                continue
            expected = _LEGACY_MEASUREMENT_IDS.get(key)
            match = _match_measurement(value, record, expected_id=expected)
            unresolved = match is None or match.ambiguous
            rules.append(
                MeasurementRule(
                    output=None if unresolved else match.measurement_id,
                    rule_type="element_size",
                    preferred_sources=(match.source,) if match else (),
                    fallbacks=_fallbacks(match.measurement_id) if match else (),
                    frame=_frame(match.frame if match else "semantic"),
                    direction=match.direction if match else None,
                    minimum_confidence=_confidence(match.confidence if match else None),
                    tolerance=(
                        MeasurementTolerance(absolute_si=match.tolerance_si, relative=0.0)
                        if match
                        else MeasurementTolerance(
                            absolute_si=_element_scale(record) * _SCALE_REL_TOL,
                            relative=_DEFAULT_REL_TOL,
                        )
                    ),
                    unresolved=unresolved,
                    intent=_intent(
                        item=item,
                        index=index,
                        record=record,
                        guid=guid,
                        value_si=value,
                        relationship=relationship,
                        match=match,
                    ),
                )
            )
        return rules
    value_key = {
        "area": "area",
        "path": "distance",
        "angle": "degrees",
    }.get(kind)
    value = _finite(item.get(value_key)) if value_key else None
    match = _match_measurement(value, record) if kind == "area" and value is not None else None
    resolved = bool(match and not match.ambiguous and match.measurement_id == "section.area")
    rule_type = {
        "area": "area",
        "path": "path",
        "angle": "angle",
        "laser": "clearance",
    }.get(kind, "object_measurement")
    return [
        MeasurementRule(
            output=match.measurement_id if resolved else None,
            rule_type=rule_type,
            preferred_sources=(match.source,) if resolved and match else (),
            fallbacks=_fallbacks(match.measurement_id) if resolved and match else (),
            frame=_frame(match.frame if match else "local"),
            direction=match.direction if match else None,
            minimum_confidence=_confidence(match.confidence if match else None),
            tolerance=(
                MeasurementTolerance(absolute_si=match.tolerance_si, relative=0.0)
                if match
                else MeasurementTolerance(relative=_DEFAULT_REL_TOL)
            ),
            unresolved=not resolved,
            intent=_intent(
                item=item,
                index=index,
                record=record,
                guid=guid,
                value_si=value,
                relationship=relationship,
                match=match,
            ),
        )
    ]


def _record_type_name(record: dict[str, Any]) -> str | None:
    identity = _object_info(record)
    value = identity.get("type") or record.get("type")
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name else None
    return str(value) if value else None


def _geometry_signature(record: dict[str, Any]) -> dict[str, Any]:
    supplied = record.get("geometry_signature")
    if isinstance(supplied, dict) and supplied:
        return dict(supplied)
    try:
        return build_geometry_signature(record)
    except (TypeError, ValueError):
        return {}


def _build_spec(
    *,
    items: list[dict[str, Any]],
    elements: dict[str, dict[str, Any]],
    guids: list[str],
    classes: list[str],
    analysis: dict[str, Any] | None,
    model_name: str,
    measured_at: str | None,
) -> MeasurementSkillSpec:
    rules: list[MeasurementRule] = []
    resolved_outputs: set[str] = set()
    for index, item in enumerate(items[:_MAX_ROWS]):
        candidates = (
            [_distance_rule(item, index, elements)]
            if item.get("kind", "distance") == "distance"
            else _generic_rules(item, index, elements)
        )
        for rule in candidates:
            if rule.output and not rule.unresolved:
                if rule.output in resolved_outputs:
                    continue
                resolved_outputs.add(rule.output)
            rules.append(rule)
    if not rules:
        rules.append(
            MeasurementRule(
                output=None,
                unresolved=True,
                intent=MeasurementIntent(viewer_kind="unknown", viewer_index=0),
            )
        )

    exemplars: list[MeasurementExemplarObject] = []
    profile_families: list[str] = []
    geometry_families: list[str] = []
    hard_requirements: list[str] = []
    for guid in guids[:_MAX_ELEMENTS]:
        record = elements.get(guid)
        if not record:
            continue
        identity = _object_info(record)
        signature = _geometry_signature(record)
        profile_family = signature.get("profile_family")
        geometry_family = record.get("geometry_family") or signature.get("geometry_family")
        if isinstance(profile_family, str) and profile_family not in profile_families:
            profile_families.append(profile_family)
        if isinstance(geometry_family, str) and geometry_family not in geometry_families:
            geometry_families.append(geometry_family)
        if isinstance(geometry_family, str) and any(
            token in geometry_family for token in ("constant_profile", "piecewise_profile")
        ):
            hard_requirements.append("constant_or_piecewise_profile")
        exemplars.append(
            MeasurementExemplarObject(
                global_id=guid,
                ifc_class=str(identity.get("class")) if identity.get("class") else None,
                type_name=_record_type_name(record),
                geometry_family=str(geometry_family) if geometry_family else None,
                geometry_signature=signature,
            )
        )

    revision = (analysis or {}).get("model_revision")
    revision = revision if isinstance(revision, dict) else {}
    exemplar = MeasurementExemplar(
        model_name=model_name,
        recorded_at=measured_at,
        model_revision=MeasurementModelRevision(
            model_id=str(revision.get("model_id")) if revision.get("model_id") else None,
            fingerprint=(str(revision.get("fingerprint")) if revision.get("fingerprint") else None),
            revision=(
                int(revision["revision"])
                if isinstance(revision.get("revision"), int) and revision["revision"] >= 0
                else None
            ),
        ),
        objects=tuple(exemplars),
    )
    return MeasurementSkillSpec(
        applicability=MeasurementApplicability(
            ifc_classes=tuple(classes),
            profile_families=tuple(profile_families),
            geometry_families=tuple(geometry_families),
            hard_requirements=tuple(dict.fromkeys(hard_requirements)),
            similarity_threshold=0.85,
        ),
        measurements=tuple(rules),
        exemplar=exemplar,
        outputs=tuple(rule.output for rule in rules if rule.output and not rule.unresolved),
    )


def _distance_row(
    item: dict[str, Any], elements: dict[str, dict[str, Any]], intents: list[str]
) -> tuple[str, str]:
    value = _fmt(item.get("distance"))
    parts: list[str] = []
    axis = _distance_axis(item)
    if axis:
        parts.append(f"along model {axis.upper()}")
    ends = item.get("ends")
    if isinstance(ends, list) and len(ends) == 2:
        parts.append(f"{ends[0]} to {ends[1]} snap")
    guids = _anchor_guids(item)
    if len(guids) == 1:
        parts.append(f"on {_describe(elements.get(guids[0]), guids[0])}")
        distance = item.get("distance")
        if isinstance(distance, (int, float)):
            match = _match_dimension(float(distance), elements.get(guids[0]))
            if match:
                parts.append(f"equals the element's {match[0]} ({match[1]})")
                intents.append(match[0])
    elif len(guids) >= 2:
        described = " and ".join(_describe(elements.get(g), g) for g in guids[:2])
        parts.append(f"clear distance between {described}")
        intents.append("clear_distance")
    return value, "; ".join(parts) or "free distance"


def _generic_row(
    item: dict[str, Any], elements: dict[str, dict[str, Any]], intents: list[str]
) -> tuple[str, str]:
    kind = item.get("kind", "distance")
    guids = _anchor_guids(item)
    where = f" on {_describe(elements.get(guids[0]), guids[0])}" if guids else ""
    if kind == "dimensions":
        dims = [
            f"{key} {_fmt(item[key])}"
            for key in ("length", "width", "thickness")
            if isinstance(item.get(key), (int, float))
        ]
        intents.extend(["length", "width", "thickness"])
        method = item.get("method", "bounding box")
        target = item.get("guid")
        if target and not where:
            where = f" on {_describe(elements.get(target), target)}"
        return ", ".join(dims) or "element size", f"whole element size ({method}){where}"
    if kind == "area":
        value = _fmt(item.get("area"), "m2")
        extra = f", perimeter {_fmt(item.get('perimeter'))}" if item.get("perimeter") else ""
        intents.append("area")
        return value, f"traced face area{extra}{where}"
    if kind == "path":
        points = len(item.get("points") or [])
        intents.append("path_length")
        return _fmt(item.get("distance")), f"polyline length over {points} points{where}"
    if kind == "angle":
        intents.append("angle")
        return f"{_fmt(item.get('degrees'), '')} deg", f"angle between two legs{where}"
    if kind == "laser":
        spans = []
        axes = item.get("axes")
        if isinstance(axes, dict):
            for name in _AXIS_NAMES:
                axis = axes.get(name)
                if isinstance(axis, dict) and isinstance(axis.get("span"), (int, float)):
                    spans.append(f"{name.upper()} {_fmt(axis['span'])}")
        intents.append("clearance")
        return ", ".join(spans) or "clearances", f"free spans to neighbours{where}"
    return "", str(kind)


def build_recorded_skill(
    *,
    items: list[dict[str, Any]],
    measured_at: str | None,
    model_name: str,
    analysis: dict[str, Any] | None,
    notes: str = "",
) -> RecordedSkill:
    """Skill markdown (without front matter) for a viewer measurement set."""
    elements = _element_index(analysis)
    guids = measured_guids(items)
    classes: list[str] = []
    for guid in guids:
        record = elements.get(guid)
        identity = _object_info(record)
        if identity.get("class") and identity["class"] not in classes:
            classes.append(identity["class"])

    intents: list[str] = []
    rows: list[tuple[str, str, str]] = []
    for item in items[:_MAX_ROWS]:
        kind = item.get("kind", "distance")
        if kind == "distance":
            value, meaning = _distance_row(item, elements, intents)
        else:
            value, meaning = _generic_row(item, elements, intents)
        label = item.get("label")
        if isinstance(label, str) and label.strip():
            meaning = f"{label.strip()}; {meaning}"
        rows.append((str(kind), value, meaning))
    unique_intents = list(dict.fromkeys(intents))
    spec = _build_spec(
        items=items,
        elements=elements,
        guids=guids,
        classes=classes,
        analysis=analysis,
        model_name=model_name,
        measured_at=measured_at,
    )

    lines: list[str] = []
    lines.append("## When to use")
    lines.append(
        "The user asks to repeat this measurement pattern on similar elements, "
        f"or names this skill. Recorded in the 3D viewer from {len(items)} "
        f"measurement(s) on '{model_name}'" + (f" at {measured_at}." if measured_at else ".")
    )
    if notes.strip():
        lines.append("")
        lines.append(f"Notes from the recording user: {notes.strip()}")
    lines.append("")
    lines.append("## Recorded example")
    if guids:
        lines.append("Elements measured:")
        for guid in guids[:_MAX_ELEMENTS]:
            record = elements.get(guid)
            entry = f"- {_describe(record, guid)}"
            identity = _object_info(record)
            type_info = identity.get("type") or (record or {}).get("type")
            if isinstance(type_info, dict) and type_info.get("name"):
                entry += f", type '{type_info['name']}'"
            elif isinstance(type_info, str) and type_info:
                entry += f", type '{type_info}'"
            lines.append(entry)
        if len(guids) > _MAX_ELEMENTS:
            lines.append(f"- and {len(guids) - _MAX_ELEMENTS} more")
        lines.append("")
    lines.append("| # | kind | value | what it means |")
    lines.append("| - | ---- | ----- | ------------- |")
    for index, (kind, value, meaning) in enumerate(rows, start=1):
        lines.append(f"| {index} | {_cell(kind)} | {_cell(value)} | {_cell(meaning)} |")
    if len(items) > _MAX_ROWS:
        lines.append("")
        lines.append(f"({len(items) - _MAX_ROWS} further measurements not listed.)")
    lines.append("")
    lines.append(
        "All values are metres, model axes (z up), as reported by get_viewer_measurements."
    )
    lines.append("")
    lines.append("## Executable measurement spec")
    if not spec.executable:
        unresolved = sum(1 for rule in spec.measurements if rule.unresolved)
        lines.append(
            f"Review required: {unresolved} recorded intent(s) remain unresolved. "
            "Name or remove them before deterministic batch application."
        )
        lines.append("")
    lines.append(measurement_spec_block(spec))
    lines.append("")
    lines.append("## Steps")
    selector = classes[0] if classes else "<IfcClass>"
    intent_text = ", ".join(unique_intents) if unique_intents else "the values above"
    lines.append(
        "1. Resolve the targets: the user's viewer selection, or query_elements "
        f"with a selector (same class: `{selector}`; narrow with `, "
        'type="..."` or a property filter when the user says so).'
    )
    lines.append(
        "2. For a reviewed version 2 spec, call apply_measurement_skill with "
        "dry_run=true first. Otherwise run analyze_element_geometry and read the "
        f"measurements matching the recorded intents ({intent_text}). Keep each "
        "value's source, frame, confidence and flags."
    )
    lines.append(
        "3. The intent survives a shape change: when a dimension key is "
        "missing, fall back to measure_directional_extent along the same "
        "model axis, or slice_element_mesh at mid element and read the "
        "section. Say which fallback was used."
    )
    lines.append(
        "4. Answer with one markdown table: GlobalId, name, one column per "
        "intent, plus source and flags. Do not silently skip an element."
    )
    lines.append("")
    lines.append("## Verify")
    lines.append(
        "Cross-check one element against a second method (profile_parameter "
        "vs mesh_section, or the recorded example itself). Report values that "
        "disagree beyond tolerance as deviations; never average them away."
    )
    lines.append("")
    lines.append("## Propose (optional)")
    lines.append(
        "Only after the user confirms the table: store values with "
        "measure__propose_measured_value (unit metres, method "
        "'recorded skill'), which writes to the IfcConsole_AI_ psets "
        "with provenance."
    )

    description = (
        f"Recorded viewer measurement pattern on {classes[0]}"
        if classes
        else "Recorded viewer measurement pattern"
    )
    if unique_intents:
        description += f": {', '.join(unique_intents[:4])}"
    applies_to = ", ".join(classes[:4]) if classes else None
    return RecordedSkill(
        content="\n".join(lines) + "\n",
        description=description[:160],
        applies_to=applies_to,
        spec=spec,
        classes=tuple(classes),
        intents=tuple(unique_intents),
        guids=tuple(guids),
        unresolved_intents=tuple(
            rule.intent.viewer_index for rule in spec.measurements if rule.unresolved
        ),
    )


__all__ = ["RecordedSkill", "build_recorded_skill", "measured_guids"]
