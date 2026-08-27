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
    name = analysis.get("name") or analysis.get("class", "element")
    lines = [f"## {index}. {_escape(name)}", ""]
    lines.append(f"- GlobalId: `{analysis.get('global_id')}`")
    lines.append(f"- Class: {analysis.get('class')}")
    etype = analysis.get("type")
    if etype:
        lines.append(f"- Type: {_escape(etype.get('name'))} ({etype.get('class')})")
    material = analysis.get("material")
    if isinstance(material, dict):
        label = material.get("name") or ", ".join(
            str(item) for item in material.get("profiles", []) if item
        )
        lines.append(f"- Material: {_escape(label) or material.get('kind')}")

    dims = _dimension_rows(analysis.get("dimensions") or {}, units.get("length_unit"))
    if dims:
        lines += ["", "### Dimensions", ""] + dims

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
    lines.append(f"- Elements: {len(entries)}")
    if notes:
        lines += ["", _escape(notes)]
    lines.append("")
    for index, entry in enumerate(entries, start=1):
        lines += element_section(entry, units, index)
    lines.append(
        "Values marked profile_parameter and extrusion_depth are exact from the "
        "IFC definition; mesh_section and mesh_axis values are measured from the "
        "tessellated geometry."
    )
    lines.append("")
    return "\n".join(lines)


__all__ = ["build_measurement_report", "element_section"]
