"""Markdown measurement reports built from analyze_elements records.

Pure string building: the caller measures, this renders. Values appear in the
file's units and SI, each with its named source, so a report stands on its own
as an auditable document.
"""

from __future__ import annotations

from typing import Any

_DIM_LABELS = (
    ("width", "Width (b)"),
    ("height", "Height (h)"),
    ("length", "Length (L)"),
    ("flange_thickness", "Flange thickness (t_f)"),
    ("web_thickness", "Web thickness (t_w)"),
    ("wall_thickness", "Wall thickness (t)"),
)

_MEASUREMENT_GROUPS = {
    "envelope": "Envelope",
    "mass": "Mass geometry",
    "profile": "Profile",
    "longitudinal": "Longitudinal form",
    "section": "Sections",
    "material": "Material",
    "opening": "Openings and voids",
    "topology": "Topology",
    "stored": "Stored values",
}

_SI_UNITS = {
    "length": "m",
    "area": "m^2",
    "volume": "m^3",
    "angle": "deg",
    "count": "",
    "ratio": "",
}


def _escape(value: Any) -> str:
    text = str(value if value is not None else "")
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _fmt(value: Any, digits: int = 4) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return _escape(value)
    text = f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    return text or "0"


def _dimension_rows(dimensions: dict[str, Any], length_unit: str | None) -> list[str]:
    unit = length_unit or "file units"
    rows = ["| Dimension | File value | SI | Source |", "| --- | --- | --- | --- |"]
    seen = set()
    ordered = [key for key, _ in _DIM_LABELS if key in dimensions]
    ordered += [key for key in dimensions if key not in {k for k, _ in _DIM_LABELS}]
    labels = dict(_DIM_LABELS)
    for key in ordered:
        if key in seen or dimensions.get(key) is None:
            continue
        seen.add(key)
        dim = dimensions[key]
        rows.append(
            f"| {labels.get(key, key)} | {_fmt(dim['file'])} {unit} "
            f"| {_fmt(dim['si'])} m | {dim['source']} |"
        )
    return rows if len(rows) > 2 else []


def _identity(analysis: dict[str, Any]) -> dict[str, Any]:
    """The v2 object envelope is additive; legacy records stay flat."""
    obj = analysis.get("object")
    return obj if isinstance(obj, dict) else analysis


def _measurement_group(record: dict[str, Any]) -> str:
    identifier = str(record.get("id") or "other")
    prefix = identifier.partition(".")[0]
    return _MEASUREMENT_GROUPS.get(prefix, prefix.replace("_", " ").title() or "Other")


def _measurement_label(record: dict[str, Any]) -> str:
    identifier = str(record.get("id") or "measurement")
    return str(record.get("label") or identifier).strip()


def _si_value(record: dict[str, Any]) -> str:
    unit = str(record.get("si_unit") or _SI_UNITS.get(record.get("quantity_kind"), ""))
    value = record.get("value_si")
    if value is not None:
        return f"{_fmt(value, 6)}{f' {unit}' if unit else ''}"
    value_range = record.get("range_si")
    if isinstance(value_range, dict):
        low = value_range.get("minimum", value_range.get("min"))
        high = value_range.get("maximum", value_range.get("max"))
        if low is not None or high is not None:
            return f"{_fmt(low, 6)} to {_fmt(high, 6)}{f' {unit}' if unit else ''}"
    if isinstance(value_range, (list, tuple)) and len(value_range) >= 2:
        return f"{_fmt(value_range[0], 6)} to {_fmt(value_range[1], 6)}{f' {unit}' if unit else ''}"
    return "unavailable"


def _file_value(record: dict[str, Any], length_unit: str | None) -> str:
    value = record.get("value_file")
    if value is None:
        return ""
    unit = record.get("file_unit") or length_unit or "file units"
    return f"{_fmt(value, 6)} {unit}"


def _measurement_method(record: dict[str, Any]) -> str:
    source = str(record.get("source") or "unspecified")
    method = str(record.get("method") or "")
    return source if not method or method == source else f"{source}; {method}"


def _measurement_context(record: dict[str, Any]) -> str:
    parts = []
    frame = record.get("frame")
    direction = record.get("direction")
    station = record.get("station")
    component = record.get("component")
    if frame:
        parts.append(str(frame))
    if direction:
        parts.append(str(direction))
    if isinstance(station, (int, float)) and not isinstance(station, bool):
        parts.append(f"station {_fmt(100 * float(station), 1)}%")
    elif station is not None:
        parts.append(f"station {station}")
    if component and component != "whole_object":
        parts.append(str(component))
    return ", ".join(parts) or "whole object"


def _measurement_quality(record: dict[str, Any]) -> str:
    parts = [str(record.get("confidence") or "unknown")]
    uncertainty = record.get("uncertainty_si")
    if isinstance(uncertainty, (int, float)) and not isinstance(uncertainty, bool):
        unit = str(record.get("si_unit") or _SI_UNITS.get(record.get("quantity_kind"), ""))
        parts.append(f"+/- {_fmt(uncertainty, 6)}{f' {unit}' if unit else ''}")
    flags = record.get("flags") or []
    if flags:
        parts.append(", ".join(_escape(flag) for flag in flags))
    return "; ".join(parts)


def _measurement_rows(measurements: list[dict[str, Any]], length_unit: str | None) -> list[str]:
    lines: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in measurements:
        if isinstance(record, dict):
            groups.setdefault(_measurement_group(record), []).append(record)
    for group, records in groups.items():
        lines += [f"#### {_escape(group)}", ""]
        lines += [
            "| Measurement | SI value | File value | Source and method | Frame / domain | Confidence |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for record in records:
            identifier = str(record.get("id") or "")
            label = _measurement_label(record)
            display = (
                f"{_escape(label)} (`{_escape(identifier)}`)" if identifier else _escape(label)
            )
            lines.append(
                f"| {display} | {_escape(_si_value(record))} "
                f"| {_escape(_file_value(record, length_unit))} "
                f"| {_escape(_measurement_method(record))} "
                f"| {_escape(_measurement_context(record))} "
                f"| {_escape(_measurement_quality(record))} |"
            )
        lines.append("")
    return lines


def _alternative_rows(measurements: list[dict[str, Any]]) -> list[str]:
    rows = [
        "| Measurement | Alternative | Source and method | Delta | Confidence / status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in measurements:
        if not isinstance(record, dict):
            continue
        for alternative in record.get("alternatives") or []:
            if not isinstance(alternative, dict):
                continue
            delta = alternative.get("delta_si", alternative.get("absolute_delta_si"))
            relative = alternative.get("relative_delta", alternative.get("delta_relative"))
            delta_parts = []
            if delta is not None:
                delta_parts.append(f"{_fmt(delta, 6)} SI")
            if relative is not None:
                try:
                    delta_parts.append(f"{100 * float(relative):.2f}%")
                except (TypeError, ValueError):
                    delta_parts.append(str(relative))
            quality = str(alternative.get("confidence") or "unknown")
            status = alternative.get("status")
            flags = alternative.get("flags") or []
            if status:
                quality += f"; {status}"
            if flags:
                quality += "; " + ", ".join(str(flag) for flag in flags)
            rows.append(
                f"| `{_escape(record.get('id'))}` | {_escape(_si_value(alternative))} "
                f"| {_escape(_measurement_method(alternative))} "
                f"| {_escape(', '.join(delta_parts))} | {_escape(quality)} |"
            )
    return rows if len(rows) > 2 else []


def _coverage_item(item: Any) -> str:
    if not isinstance(item, dict):
        return _escape(item)
    identifier = item.get("id") or item.get("measurement_id") or item.get("output") or "item"
    reason = item.get("reason") or item.get("detail") or item.get("status")
    return f"`{_escape(identifier)}`" + (f": {_escape(reason)}" if reason else "")


def _coverage_lines(coverage: dict[str, Any]) -> list[str]:
    categories = ("requested", "extracted", "unavailable", "ambiguous", "conflicting")
    rows = ["| Status | Count | Measurements |", "| --- | ---: | --- |"]
    for category in categories:
        value = coverage.get(category) or []
        items = value if isinstance(value, list) else [value]
        details = ", ".join(_coverage_item(item) for item in items)
        rows.append(f"| {category.title()} | {len(items)} | {details} |")
    return rows


def _section_lines(cut: dict[str, Any], stations: list[dict[str, Any]] | None) -> list[str]:
    lines = [
        f"Cut at {int(round(cut.get('at', 0.5) * 100))}% of the long axis, values in SI metres:",
        "",
        f"- Width x height: {_fmt(cut.get('width'))} x {_fmt(cut.get('height'))} m",
        f"- Perimeter: {_fmt(cut.get('perimeter'))} m",
    ]
    if cut.get("area") is not None:
        lines.append(f"- Section area: {_fmt(cut['area'], 6)} m^2")
    thickness = cut.get("thickness") or {}
    if thickness:
        lines.append(
            "- Wall thickness: median "
            f"{_fmt(thickness.get('median'))} m "
            f"(p25 {_fmt(thickness.get('p25'))}, p75 {_fmt(thickness.get('p75'))}, "
            f"{thickness.get('samples')} samples)"
        )
        pair = thickness.get("pair")
        if pair:
            lines.append(
                "- Two plate groups detected: "
                f"{_fmt(pair['lower']['value'])} m ({int(pair['lower']['share'] * 100)}%) and "
                f"{_fmt(pair['upper']['value'])} m ({int(pair['upper']['share'] * 100)}%)"
            )
    if not cut.get("closed"):
        lines.append("- Section outline did not close; treat area and thickness as estimates")
    if stations and len(stations) > 1:
        lines += ["", "| Station | Width | Height | Perimeter |", "| --- | --- | --- | --- |"]
        for station in stations:
            lines.append(
                f"| {int(round(station['at'] * 100))}% | {_fmt(station['width'])} "
                f"| {_fmt(station['height'])} | {_fmt(station['perimeter'])} |"
            )
    return lines


def _profile_lines(record: dict[str, Any], indent: str = "") -> list[str]:
    label = record.get("class", "profile")
    name = record.get("name")
    head = f"{indent}- {label}" + (f" `{_escape(name)}`" if name else "")
    lines = [head]
    params = record.get("parameters") or {}
    if params:
        pairs = ", ".join(f"{key}={_fmt(value)}" for key, value in sorted(params.items()))
        lines.append(f"{indent}  - parameters (file units): {pairs}")
    curve = record.get("curve") or {}
    if curve:
        pairs = ", ".join(
            f"{key}={_fmt(value)}"
            for key, value in curve.items()
            if isinstance(value, (int, float))
        )
        lines.append(f"{indent}  - outline (file units): {pairs}")
    for child in record.get("profiles") or []:
        lines += _profile_lines(child, indent + "  ")
    if record.get("parent"):
        lines += _profile_lines(record["parent"], indent + "  ")
    return lines


def _quantity_lines(qtos: dict[str, Any]) -> list[str]:
    lines = []
    for set_name, values in sorted(qtos.items()):
        rows = [
            f"| {_escape(name)} | {_fmt(value)} |"
            for name, value in sorted(values.items())
            if name != "id" and isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if rows:
            lines += [f"**{_escape(set_name)}**", "", "| Quantity | Value |", "| --- | --- |"]
            lines += rows
            lines.append("")
    return lines


def element_section(entry: dict[str, Any], units: dict[str, Any], index: int) -> list[str]:
    analysis = entry["analysis"]
    identity = _identity(analysis)
    name = identity.get("name") or identity.get("class", "element")
    lines = [f"## {index}. {_escape(name)}", ""]
    lines.append(f"- GlobalId: `{identity.get('global_id')}`")
    lines.append(f"- Class: {identity.get('class')}")
    etype = identity.get("type")
    if etype:
        if isinstance(etype, dict):
            lines.append(f"- Type: {_escape(etype.get('name'))} ({etype.get('class')})")
        else:
            lines.append(f"- Type: {_escape(etype)}")
    version = analysis.get("analysis_version") or (
        "2.0" if isinstance(analysis.get("measurements"), list) else None
    )
    if version:
        lines.append(f"- Analysis contract: {_escape(version)}")
    family = analysis.get("geometry_family")
    if family:
        lines.append(f"- Geometry family: {_escape(family)}")
    material = analysis.get("material")
    if isinstance(material, dict):
        label = material.get("name") or ", ".join(
            str(item) for item in material.get("profiles", []) if item
        )
        lines.append(f"- Material: {_escape(label) or material.get('kind')}")

    measurements = [item for item in (analysis.get("measurements") or []) if isinstance(item, dict)]
    # The normalized inventory is additive. Keep this compatibility table in
    # every report so existing readers do not change during the v2 migration.
    dims = _dimension_rows(analysis.get("dimensions") or {}, units.get("length_unit"))
    if dims:
        lines += ["", "### Dimensions", ""] + dims
    if measurements:
        lines += ["", "### Parametric measurements", ""]
        lines += _measurement_rows(measurements, units.get("length_unit"))
        alternatives = _alternative_rows(measurements)
        if alternatives:
            lines += ["### Alternative evidence and source deltas", ""] + alternatives
        coverage = analysis.get("coverage")
        if isinstance(coverage, dict):
            lines += ["", "### Measurement coverage", ""] + _coverage_lines(coverage)

    cut = analysis.get("cross_section")
    if cut:
        lines += ["", "### Measured cross section", ""]
        lines += _section_lines(cut, analysis.get("section_stations"))

    profile_lines: list[str] = []
    for solid in analysis.get("swept_solids") or []:
        if solid.get("profile"):
            profile_lines += _profile_lines(solid["profile"])
            if solid.get("depth") is not None:
                profile_lines.append(f"  - extrusion depth (file units): {_fmt(solid['depth'])}")
    for item in analysis.get("material_profiles") or []:
        if item.get("profile"):
            profile_lines += _profile_lines(item["profile"])
    if profile_lines:
        lines += ["", "### Profile definition (from the IFC file)", ""] + profile_lines

    box = analysis.get("box")
    if box:
        extents = box.get("local_extents", {})
        lines += [
            "",
            "### Bounding box (SI metres)",
            "",
            f"- Local extents: {_fmt(extents.get('x'))} x {_fmt(extents.get('y'))} "
            f"x {_fmt(extents.get('z'))} m",
            f"- Volume: {_fmt(box.get('volume'), 6)} m^3, "
            f"footprint: {_fmt(box.get('footprint_area'), 6)} m^2",
        ]
        if box.get("surface_area") is not None:
            lines.append(
                f"- Surface area: {_fmt(box['surface_area'], 6)} m^2 "
                f"(top {_fmt(box.get('top_area'), 6)}, "
                f"bottom {_fmt(box.get('bottom_area'), 6)}, "
                f"sides x {_fmt(box.get('side_area_x'), 6)}, "
                f"y {_fmt(box.get('side_area_y'), 6)})"
            )
        lines.append(
            f"- Prismatic confidence: {box.get('confidence')} "
            f"(ratio {_fmt(box.get('prismatic_ratio'))})"
        )

    qtos = entry.get("qtos") or {}
    if qtos:
        lines += ["", "### Stored quantities (file units)", ""] + _quantity_lines(qtos)

    flags = analysis.get("flags") or []
    if flags:
        lines += ["", f"Flags: {', '.join(flags)}"]
    lines.append("")
    return lines


def build_measurement_report(
    *,
    title: str,
    model: dict[str, Any],
    units: dict[str, Any],
    entries: list[dict[str, Any]],
    notes: str | None = None,
    generated_at: str | None = None,
    analysis_version: str | None = None,
    model_revision: dict[str, Any] | None = None,
) -> str:
    lines = [f"# {_escape(title)}", ""]
    if model.get("project"):
        lines.append(f"- Project: {_escape(model['project'])}")
    if model.get("file"):
        lines.append(f"- Model: {_escape(model['file'])} ({model.get('schema', '?')})")
    lines.append(
        f"- Units: {units.get('length_unit') or 'unknown'} "
        f"(1 unit = {_fmt(units.get('to_si_factor', 1.0), 6)} m); SI values in metres"
    )
    if generated_at:
        lines.append(f"- Generated: {generated_at} by ifc-console")
    if analysis_version:
        lines.append(f"- Analysis contract: {_escape(analysis_version)}")
    if model_revision:
        revision_parts = []
        if model_revision.get("model_id"):
            revision_parts.append(f"model_id={_escape(model_revision['model_id'])}")
        if model_revision.get("fingerprint"):
            revision_parts.append(f"fingerprint={_escape(model_revision['fingerprint'])}")
        if model_revision.get("revision") is not None:
            revision_parts.append(f"revision={_escape(model_revision['revision'])}")
        if revision_parts:
            lines.append("- Model revision: " + ", ".join(revision_parts))
    lines.append(f"- Elements: {len(entries)}")
    if notes:
        lines += ["", _escape(notes)]
    lines.append("")
    for index, entry in enumerate(entries, start=1):
        lines += element_section(entry, units, index)
    lines.append(
        "Direct IFC representation parameters are exact only when their evidence "
        "says transforms and Boolean modifications preserve them. Mesh-derived "
        "values are estimates from tessellated geometry. Alternative sources and "
        "conflicts remain separate; they are never silently averaged."
    )
    lines.append("")
    return "\n".join(lines)


__all__ = ["build_measurement_report", "element_section"]
