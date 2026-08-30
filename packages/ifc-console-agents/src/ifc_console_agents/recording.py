"""Turn viewer measurements into a saved agent skill.

A recording stores intent, not coordinates. Each measured value is matched
against the exemplar element's analyzed dimensions so the skill names what
was measured, and the generated steps tell an agent how to compute the same
intent on similar elements, including ones with a different shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_MAX_ROWS = 40
_MAX_ELEMENTS = 12
# a measured value equals an analyzed dimension within 5 mm or 2 percent
_ABS_TOL = 0.005
_REL_TOL = 0.02

_AXIS_NAMES = ("x", "y", "z")


@dataclass
class RecordedSkill:
    content: str
    description: str
    applies_to: str | None
    classes: tuple[str, ...] = ()
    intents: tuple[str, ...] = ()
    guids: tuple[str, ...] = field(default=())


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


def _element_index(analysis: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if not isinstance(analysis, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for record in analysis.get("elements") or []:
        if isinstance(record, dict) and isinstance(record.get("global_id"), str):
            out[record["global_id"]] = record
    return out


def _describe(record: dict[str, Any] | None, guid: str) -> str:
    if not record:
        return guid
    label = record.get("class") or "element"
    name = record.get("name")
    return f"{label} '{name}' ({guid})" if name else f"{label} ({guid})"


def _match_dimension(value: float, record: dict[str, Any] | None) -> tuple[str, str] | None:
    """The analyzed dimension a measured value equals, if any."""
    dims = (record or {}).get("dimensions")
    if not isinstance(dims, dict):
        return None
    best: tuple[float, str, str] | None = None
    for key, dim in dims.items():
        si = dim.get("si") if isinstance(dim, dict) else None
        if not isinstance(si, (int, float)):
            continue
        gap = abs(value - float(si))
        if gap <= max(_ABS_TOL, _REL_TOL * abs(float(si))):
            if best is None or gap < best[0]:
                best = (gap, key, str(dim.get("source", "")))
    return (best[1], best[2]) if best else None


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
        if record and record.get("class") and record["class"] not in classes:
            classes.append(record["class"])

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

    lines: list[str] = []
    lines.append("## When to use")
    lines.append(
        "The user asks to repeat this measurement pattern on similar elements, "
        f"or names this skill. Recorded in the 3D viewer from {len(items)} "
        f"measurement(s) on '{model_name}'"
        + (f" at {measured_at}." if measured_at else ".")
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
            type_info = (record or {}).get("type")
            if isinstance(type_info, dict) and type_info.get("name"):
                entry += f", type '{type_info['name']}'"
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
        "All values are metres, model axes (z up), as reported by "
        "get_viewer_measurements."
    )
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
        "2. For each target run analyze_element_geometry and read the "
        f"dimensions matching the recorded intents ({intent_text}). Keep each "
        "value's source."
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
        classes=tuple(classes),
        intents=tuple(unique_intents),
        guids=tuple(guids),
    )


__all__ = ["RecordedSkill", "build_recorded_skill", "measured_guids"]
