"""Model quality scorecard: how good is this file, and what would improve it.

validate_model checks the schema and check_model_health looks for modelling
accidents. Neither says whether the file is useful: whether elements are typed,
classified, named, contained, carry the property sets a downstream reader
expects, and have quantities and materials. This module scores those
dimensions deterministically so an agent explains and prioritises a scorecard
instead of inventing one.

Every score is a fraction of elements that pass, so the numbers stay
comparable between files and between revisions of one file.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ifc_console.ifc.info import COMMON_CLASSES, _classification_coverage, _header, _units
from ifc_console.ifc.property_audit import (
    PropertyIndex,
    applicable_templates,
    core_set_names,
    definition_values,
    load_templates,
    predefined_type_of,
    template_rows,
)

DIMENSIONS = (
    "identity",
    "units",
    "spatial",
    "naming",
    "typing",
    "classification",
    "properties",
    "quantities",
    "materials",
    "geometry",
)

WEIGHTS: dict[str, float] = {
    "identity": 1.0,
    "units": 1.0,
    "spatial": 2.0,
    "naming": 1.0,
    "typing": 1.0,
    "classification": 1.0,
    "properties": 3.0,
    "quantities": 2.0,
    "materials": 1.0,
    "geometry": 1.0,
}

TITLES: dict[str, str] = {
    "identity": "Project identity",
    "units": "Units and georeferencing",
    "spatial": "Spatial structure",
    "naming": "Naming",
    "typing": "Types and predefined types",
    "classification": "Classification",
    "properties": "Property sets",
    "quantities": "Quantities",
    "materials": "Materials",
    "geometry": "Geometry",
}

GRADES = ((90.0, "A"), (75.0, "B"), (60.0, "C"), (40.0, "D"), (0.0, "E"))

# The properties a downstream reader looks for first, when the class's common
# property set defines them.
KEY_PROPERTIES = (
    "Reference",
    "IsExternal",
    "LoadBearing",
    "FireRating",
    "ThermalTransmittance",
    "AcousticRating",
)

MAX_EXAMPLES = 20


def grade_for(score: float) -> str:
    for threshold, letter in GRADES:
        if score >= threshold:
            return letter
    return "E"


def _status(score: float | None) -> str:
    if score is None:
        return "n/a"
    if score >= 0.85:
        return "good"
    if score >= 0.5:
        return "fair"
    return "poor"


def _example(element: Any) -> dict[str, Any]:
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
    }


def _finding(
    severity: str,
    message: str,
    elements: list[Any],
    limit: int,
    *,
    count: int | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,
        "message": message,
        "count": len(elements) if count is None else count,
        "examples": [_example(e) for e in elements[:limit]],
        "global_ids": [
            gid for gid in (getattr(e, "GlobalId", None) for e in elements[:limit]) if gid
        ],
    }


def _rate(passed: int, total: int) -> float | None:
    return round(passed / total, 3) if total else None


def _severity(rate: float | None) -> str:
    if rate is None or rate >= 0.85:
        return "info"
    if rate >= 0.5:
        return "warning"
    return "error"


def _physical(ifc: Any) -> list[Any]:
    skip = ("IfcFeatureElement", "IfcVirtualElement")
    return [e for e in ifc.by_type("IfcElement") if not any(e.is_a(s) for s in skip)]


def _dimension(
    key: str,
    score: float | None,
    metrics: dict[str, Any],
    findings: list[dict[str, Any]],
    improvements: list[str],
) -> dict[str, Any]:
    return {
        "key": key,
        "title": TITLES[key],
        "weight": WEIGHTS[key],
        "score": None if score is None else round(max(0.0, min(1.0, score)), 3),
        "status": _status(score),
        "metrics": metrics,
        "findings": findings,
        "improvements": improvements,
    }


def _identity(ifc: Any, limit: int) -> dict[str, Any]:
    project = (ifc.by_type("IfcProject") or [None])[0]
    header = _header(ifc)
    checks: list[tuple[str, float, bool, str]] = []
    name = getattr(project, "Name", None) if project is not None else None
    checks.append(("project_named", 0.35, bool(name), "Name the IfcProject."))
    described = bool(
        project is not None
        and (getattr(project, "Description", None) or getattr(project, "Phase", None))
    )
    checks.append(
        ("project_described", 0.15, described, "Give the project a description or phase.")
    )
    checks.append(
        (
            "authoring_tool_recorded",
            0.2,
            bool(header.get("originating_system")),
            "Export with the authoring application recorded in the file header.",
        )
    )
    applications = 0
    with contextlib.suppress(Exception):
        applications = len(ifc.by_type("IfcApplication"))
    checks.append(
        ("owner_history", 0.1, applications > 0, "Record owner history with an IfcApplication.")
    )
    sites = list(ifc.by_type("IfcSite"))
    buildings = list(ifc.by_type("IfcBuilding"))
    named = all(getattr(o, "Name", None) for o in sites + buildings) and bool(sites or buildings)
    checks.append(("site_and_building_named", 0.2, named, "Name every site and building."))
    score = sum(weight for _, weight, passed, _ in checks if passed)
    findings = [
        {
            "severity": "warning",
            "message": f"{key.replace('_', ' ')} is missing",
            "count": 1,
            "examples": [],
            "global_ids": [],
        }
        for key, _, passed, _ in checks
        if not passed
    ]
    return _dimension(
        "identity",
        score,
        {
            "project": name,
            "schema": ifc.schema,
            "originating_system": header.get("originating_system"),
            "applications": applications,
            "checks": {key: passed for key, _, passed, _ in checks},
        },
        findings,
        [advice for _, _, passed, advice in checks if not passed],
    )


def _georeferenced(ifc: Any) -> tuple[bool, bool]:
    crs = False
    with contextlib.suppress(Exception):
        import ifcopenshell.util.geolocation as geo

        crs = geo.get_crs(ifc) is not None
    site_coords = False
    with contextlib.suppress(Exception):
        for site in ifc.by_type("IfcSite"):
            if site.RefLatitude and site.RefLongitude:
                site_coords = True
                break
    return crs, site_coords


def _units_dimension(ifc: Any, limit: int) -> dict[str, Any]:
    units = _units(ifc)
    crs, site_coords = _georeferenced(ifc)
    score = 0.0
    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if units.get("length"):
        score += 0.5
    else:
        findings.append(
            {
                "severity": "error",
                "message": "no length unit is declared",
                "count": 1,
                "examples": [],
                "global_ids": [],
            }
        )
        improvements.append(
            "Declare the project length unit; without it no dimension in the file has a meaning."
        )
    if units.get("area"):
        score += 0.15
    if units.get("volume"):
        score += 0.15
    if not units.get("area") or not units.get("volume"):
        improvements.append("Declare area and volume units so quantities are unambiguous.")
    if crs:
        score += 0.2
    elif site_coords:
        score += 0.1
        improvements.append(
            "Add a map conversion and projected CRS; site latitude and longitude alone do not place the model on a map."
        )
    else:
        findings.append(
            {
                "severity": "warning",
                "message": "the model is not georeferenced",
                "count": 1,
                "examples": [],
                "global_ids": [],
            }
        )
        improvements.append(
            "Georeference the model with IfcMapConversion or at least the site's latitude and longitude."
        )
    return _dimension(
        "units",
        score,
        {"units": units, "map_conversion": crs, "site_coordinates": site_coords},
        findings,
        improvements,
    )


def _spatial(ifc: Any, index: PropertyIndex, elements: list[Any], limit: int) -> dict[str, Any]:
    sites = len(ifc.by_type("IfcSite"))
    buildings = len(ifc.by_type("IfcBuilding"))
    storeys = list(ifc.by_type("IfcBuildingStorey"))
    spaces = len(ifc.by_type("IfcSpace"))
    orphans = [e for e in elements if not index.is_contained(e)]
    contained_rate = _rate(len(elements) - len(orphans), len(elements))
    elevations = [getattr(s, "Elevation", None) for s in storeys]
    elevations_set = bool(storeys) and all(v is not None for v in elevations)
    distinct = elevations_set and len({round(float(v), 4) for v in elevations}) == len(elevations)

    score = 0.0
    score += 0.25 * (min(sites, 1) + min(buildings, 1) + min(len(storeys), 1)) / 3
    score += 0.5 * (contained_rate or 0.0)
    score += 0.15 * (1.0 if distinct else 0.5 if elevations_set else 0.0)
    score += 0.1 * min(spaces, 1)

    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if orphans:
        findings.append(
            _finding(
                _severity(contained_rate),
                f"{len(orphans)} element(s) are in no storey, space, or site",
                orphans,
                limit,
            )
        )
        improvements.append(
            "Assign every element to a storey or space with IfcRelContainedInSpatialStructure."
        )
    if not storeys:
        findings.append(
            {
                "severity": "error",
                "message": "the model has no building storeys",
                "count": 1,
                "examples": [],
                "global_ids": [],
            }
        )
        improvements.append("Model the storeys; a flat file cannot be scheduled per level.")
    elif not elevations_set:
        findings.append(
            {
                "severity": "warning",
                "message": "some storeys have no elevation",
                "count": sum(v is None for v in elevations),
                "examples": [],
                "global_ids": [],
            }
        )
        improvements.append("Set an elevation on every storey so levels sort and stack correctly.")
    elif not distinct:
        findings.append(
            {
                "severity": "warning",
                "message": "two or more storeys share one elevation",
                "count": len(storeys),
                "examples": [],
                "global_ids": [],
            }
        )
        improvements.append("Give each storey a distinct elevation.")
    if not spaces:
        improvements.append(
            "Add spaces (rooms) so area schedules and room-based checks become possible."
        )
    return _dimension(
        "spatial",
        score,
        {
            "sites": sites,
            "buildings": buildings,
            "storeys": len(storeys),
            "spaces": spaces,
            "elements": len(elements),
            "contained_rate": contained_rate,
            "storey_elevations_set": elevations_set,
        },
        findings,
        improvements,
    )


def _naming(ifc: Any, elements: list[Any], limit: int) -> dict[str, Any]:
    unnamed = [e for e in elements if not getattr(e, "Name", None)]
    types = list(ifc.by_type("IfcTypeObject"))
    unnamed_types = [t for t in types if not getattr(t, "Name", None)]
    storeys = list(ifc.by_type("IfcBuildingStorey"))
    unnamed_storeys = [s for s in storeys if not getattr(s, "Name", None)]
    element_rate = _rate(len(elements) - len(unnamed), len(elements))
    type_rate = _rate(len(types) - len(unnamed_types), len(types))
    storey_rate = _rate(len(storeys) - len(unnamed_storeys), len(storeys))
    score = 0.6 * (element_rate if element_rate is not None else 1.0)
    score += 0.2 * (type_rate if type_rate is not None else 1.0)
    score += 0.2 * (storey_rate if storey_rate is not None else 1.0)
    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if unnamed:
        findings.append(
            _finding(
                _severity(element_rate), f"{len(unnamed)} element(s) have no Name", unnamed, limit
            )
        )
        improvements.append(
            "Name elements in the authoring tool; a schedule of blanks helps nobody."
        )
    if unnamed_types:
        findings.append(
            _finding("warning", f"{len(unnamed_types)} type(s) have no Name", unnamed_types, limit)
        )
        improvements.append("Name every type object.")
    if unnamed_storeys:
        findings.append(
            _finding(
                "warning", f"{len(unnamed_storeys)} storey(s) have no Name", unnamed_storeys, limit
            )
        )
        improvements.append("Name every storey.")
    return _dimension(
        "naming",
        score,
        {
            "elements_named_rate": element_rate,
            "types_named_rate": type_rate,
            "storeys_named_rate": storey_rate,
        },
        findings,
        improvements,
    )


def _typing(ifc: Any, index: PropertyIndex, elements: list[Any], limit: int) -> dict[str, Any]:
    untyped = [e for e in elements if index.type_object(e) is None]
    typed_rate = _rate(len(elements) - len(untyped), len(elements))
    with_attribute = [e for e in elements if hasattr(e, "PredefinedType")]
    undefined = [e for e in with_attribute if not predefined_type_of(e)]
    predefined_rate = _rate(len(with_attribute) - len(undefined), len(with_attribute))
    proxies = [e for e in elements if e.is_a("IfcBuildingElementProxy")]
    proxy_rate = _rate(len(elements) - len(proxies), len(elements))
    score = 0.5 * (typed_rate or 0.0)
    score += 0.3 * (predefined_rate if predefined_rate is not None else 1.0)
    score += 0.2 * (proxy_rate if proxy_rate is not None else 1.0)
    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if untyped:
        findings.append(
            _finding(
                _severity(typed_rate),
                f"{len(untyped)} element(s) have no type object",
                untyped,
                limit,
            )
        )
        improvements.append("Assign type objects so shared properties live once, on the type.")
    if undefined:
        findings.append(
            _finding(
                _severity(predefined_rate),
                f"{len(undefined)} element(s) have no PredefinedType",
                undefined,
                limit,
            )
        )
        improvements.append(
            "Set PredefinedType (or USERDEFINED with an ObjectType) on elements that leave it NOTDEFINED."
        )
    if proxies:
        findings.append(
            _finding(
                "warning",
                f"{len(proxies)} element(s) are generic IfcBuildingElementProxy",
                proxies,
                limit,
            )
        )
        improvements.append(
            "Re-export proxies as their real IFC class; a proxy carries no schema expectations."
        )
    return _dimension(
        "typing",
        score,
        {
            "typed_rate": typed_rate,
            "predefined_type_rate": predefined_rate,
            "proxies": len(proxies),
            "types": len(ifc.by_type("IfcTypeObject")),
        },
        findings,
        improvements,
    )


def _classification(
    ifc: Any, index: PropertyIndex, elements: list[Any], limit: int
) -> dict[str, Any]:
    coverage = _classification_coverage(ifc, len(elements))
    unclassified = [e for e in elements if not index.is_classified(e)]
    rate = _rate(len(elements) - len(unclassified), len(elements))
    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if not coverage["systems"]:
        findings.append(
            {
                "severity": "warning",
                "message": "no classification system is referenced",
                "count": 1,
                "examples": [],
                "global_ids": [],
            }
        )
        improvements.append(
            "Classify elements with the project's system (Uniclass, OmniClass, or a national table) so cost and specification links work."
        )
    elif unclassified:
        findings.append(
            _finding(
                _severity(rate),
                f"{len(unclassified)} element(s) carry no classification reference",
                unclassified,
                limit,
            )
        )
        improvements.append(
            "Extend the classification to the elements that still lack a reference."
        )
    return _dimension(
        "classification",
        rate if elements else None,
        {"systems": coverage["systems"], "classified_rate": rate},
        findings,
        improvements,
    )


def _class_families(ifc: Any) -> list[tuple[str, list[Any]]]:
    families: list[tuple[str, list[Any]]] = []
    seen: set[int] = set()
    for cls in COMMON_CLASSES:
        members = []
        with contextlib.suppress(Exception):
            members = [e for e in ifc.by_type(cls) if e.id() not in seen]
        if members:
            seen.update(e.id() for e in members)
            families.append((cls, members))
    return families


def _properties(
    ifc: Any, index: PropertyIndex, template: Any, families: list[tuple[str, list[Any]]], limit: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Common pset presence and key property fill per class family. Returns
    the properties dimension and the quantities dimension together, since both
    read the same templates."""
    rows: list[dict[str, Any]] = []
    qto_rows: list[dict[str, Any]] = []
    weighted = qto_weighted = 0.0
    counted = qto_counted = 0
    missing_props: dict[str, int] = {}
    pset_findings: list[dict[str, Any]] = []
    qto_findings: list[dict[str, Any]] = []
    for cls, members in families:
        sample = members[0]
        applicable = applicable_templates(template, sample.is_a(), "")
        names = [entry.Name for entry in applicable]
        common, base = core_set_names(sample.is_a(), names)
        if common is None and base is None:
            continue
        key_names: list[str] = []
        if common is not None:
            definition = next(entry for entry in applicable if entry.Name == common)
            defined = {row["name"] for row in template_rows(definition)}
            key_names = [name for name in KEY_PROPERTIES if name in defined]
        present = 0
        without: list[Any] = []
        key_filled = 0
        key_slots = 0
        qto_present = 0
        qto_without: list[Any] = []
        for element in members:
            own = index.own_sets(element)
            inherited = index.inherited_sets(element)
            if common is not None:
                values: dict[str, Any] = {}
                if common in inherited:
                    values.update(definition_values(inherited[common]))
                if common in own:
                    values.update(
                        {k: v for k, v in definition_values(own[common]).items() if v is not None}
                    )
                if common in own or common in inherited:
                    present += 1
                else:
                    without.append(element)
                for name in key_names:
                    key_slots += 1
                    if values.get(name) is not None:
                        key_filled += 1
                    else:
                        key = f"{common}.{name}"
                        missing_props[key] = missing_props.get(key, 0) + 1
            if base is not None:
                if base in own or base in inherited:
                    qto_present += 1
                else:
                    qto_without.append(element)
        total = len(members)
        if common is not None:
            present_rate = present / total
            key_rate = key_filled / key_slots if key_slots else present_rate
            family_score = 0.5 * present_rate + 0.5 * key_rate
            weighted += family_score * total
            counted += total
            rows.append(
                {
                    "class": cls,
                    "elements": total,
                    "pset": common,
                    "present_rate": round(present_rate, 3),
                    "key_properties": key_names,
                    "key_filled_rate": round(key_rate, 3),
                }
            )
            if without:
                pset_findings.append(
                    _finding(
                        _severity(present_rate),
                        f"{len(without)} of {total} {cls} lack {common}",
                        without,
                        limit,
                    )
                )
        if base is not None:
            qto_rate = qto_present / total
            qto_weighted += qto_rate * total
            qto_counted += total
            qto_rows.append(
                {"class": cls, "elements": total, "qto": base, "present_rate": round(qto_rate, 3)}
            )
            if qto_without:
                qto_findings.append(
                    _finding(
                        _severity(qto_rate),
                        f"{len(qto_without)} of {total} {cls} lack {base}",
                        qto_without,
                        limit,
                    )
                )

    score = weighted / counted if counted else None
    top_missing = [
        {"property": key, "elements": count}
        for key, count in sorted(missing_props.items(), key=lambda item: (-item[1], item[0]))[:12]
    ]
    improvements: list[str] = []
    if score is not None and score < 0.85:
        improvements.append(
            "Fill the common property sets: run the element parameters workflow on the "
            "worst classes to derive and propose the missing values with provenance."
        )
    if top_missing:
        improvements.append(
            "Start with " + ", ".join(item["property"] for item in top_missing[:4]) + "."
        )
    properties = _dimension(
        "properties",
        score,
        {"classes": rows, "most_missing": top_missing},
        pset_findings,
        improvements,
    )

    qto_score = qto_weighted / qto_counted if qto_counted else None
    qto_improvements: list[str] = []
    if qto_score is not None and qto_score < 0.85:
        qto_improvements.append(
            "Export base quantities from the authoring tool, or derive them here with "
            "compute_quantities source='derived' and propose them as AI-marked values."
        )
    quantities = _dimension(
        "quantities",
        qto_score,
        {"classes": qto_rows},
        qto_findings,
        qto_improvements,
    )
    return properties, quantities


def _materials(index: PropertyIndex, elements: list[Any], limit: int) -> dict[str, Any]:
    without = [e for e in elements if index.material(e) is None]
    rate = _rate(len(elements) - len(without), len(elements))
    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if without:
        findings.append(
            _finding(_severity(rate), f"{len(without)} element(s) have no material", without, limit)
        )
        improvements.append(
            "Assign materials, on the type where elements share one, so takeoffs by material and thermal calculations become possible."
        )
    return _dimension(
        "materials",
        rate if elements else None,
        {"with_material_rate": rate},
        findings,
        improvements,
    )


def _geometry(elements: list[Any], limit: int) -> dict[str, Any]:
    without = [e for e in elements if getattr(e, "Representation", None) is None]
    rate = _rate(len(elements) - len(without), len(elements))
    findings: list[dict[str, Any]] = []
    improvements: list[str] = []
    if without:
        findings.append(
            _finding(
                _severity(rate),
                f"{len(without)} element(s) have no shape representation",
                without,
                limit,
            )
        )
        improvements.append(
            "Give every physical element a body representation, or remove placeholders that were never modelled."
        )
    improvements.append(
        "Run check_model_health for duplicate, orphaned, degenerate, or misplaced geometry; this score only counts representations."
    )
    return _dimension(
        "geometry",
        rate if elements else None,
        {"with_representation_rate": rate},
        findings,
        improvements,
    )


def _ai_authored(index: PropertyIndex) -> dict[str, Any]:
    from ifc_console.ifc.ai_provenance import is_ai_authored

    marked = 0
    sets: dict[str, int] = {}
    for definitions in index.own.values():
        names = [name for name in definitions if is_ai_authored(name)]
        if names:
            marked += 1
            for name in names:
                sets[name] = sets.get(name, 0) + 1
    return {"elements": marked, "property_sets": sets}


def _digest(report: dict[str, Any]) -> str:
    lines = [
        f"Model quality {report['score']}/100, grade {report['grade']}. "
        f"{report['counts']['elements']} elements, schema {report['schema']}.",
        "",
        "Dimensions (score, status):",
    ]
    for dim in report["dimensions"]:
        score = "n/a" if dim["score"] is None else f"{round(dim['score'] * 100)}%"
        lines.append(f"- {dim['title']}: {score}, {dim['status']}")
    if report["top_improvements"]:
        lines.append("")
        lines.append("Improvements, worst first:")
        for i, item in enumerate(report["top_improvements"], 1):
            lines.append(f"{i}. [{item['dimension']}] {item['action']}")
    worst = [
        f"- {f['message']}"
        for dim in report["dimensions"]
        for f in dim["findings"]
        if f["severity"] == "error"
    ]
    if worst:
        lines.append("")
        lines.append("Errors:")
        lines.extend(worst[:10])
    if report["ai_authored"]["elements"]:
        lines.append("")
        lines.append(
            f"{report['ai_authored']['elements']} element(s) already carry AI-authored "
            "property sets; review them with list_ai_authored_properties."
        )
    text = "\n".join(lines)
    return text[:3500]


def assess_model_quality(ifc: Any, *, max_examples: int = 5) -> dict[str, Any]:
    """Score the open model on ten dimensions and list what to improve."""
    limit = max(1, min(int(max_examples), MAX_EXAMPLES))
    index = PropertyIndex(ifc)
    template = load_templates(ifc.schema)
    elements = _physical(ifc)
    families = _class_families(ifc)
    properties, quantities = _properties(ifc, index, template, families, limit)
    dimensions = [
        _identity(ifc, limit),
        _units_dimension(ifc, limit),
        _spatial(ifc, index, elements, limit),
        _naming(ifc, elements, limit),
        _typing(ifc, index, elements, limit),
        _classification(ifc, index, elements, limit),
        properties,
        quantities,
        _materials(index, elements, limit),
        _geometry(elements, limit),
    ]
    scored = [d for d in dimensions if d["score"] is not None]
    total_weight = sum(d["weight"] for d in scored)
    overall = sum(d["score"] * d["weight"] for d in scored) / total_weight if total_weight else 0.0
    score = round(overall * 100, 1)

    ranked = sorted(scored, key=lambda d: (-(1.0 - d["score"]) * d["weight"], d["key"]))
    top: list[dict[str, Any]] = []
    for dim in ranked:
        if dim["score"] >= 0.85:
            continue
        for action in dim["improvements"][:2]:
            top.append({"dimension": dim["key"], "score": dim["score"], "action": action})
        if len(top) >= 8:
            break

    severities = {"error": 0, "warning": 0, "info": 0}
    for dim in dimensions:
        for finding in dim["findings"]:
            severities[finding["severity"]] = severities.get(finding["severity"], 0) + 1

    report = {
        "schema": ifc.schema,
        "templates": template is not None,
        "score": score,
        "grade": grade_for(score),
        "counts": {
            "elements": len(elements),
            "types": len(ifc.by_type("IfcTypeObject")),
            "storeys": len(ifc.by_type("IfcBuildingStorey")),
            "spaces": len(ifc.by_type("IfcSpace")),
            "class_families": [
                {"class": cls, "elements": len(members)} for cls, members in families
            ],
        },
        "summary": severities,
        "dimensions": dimensions,
        "top_improvements": top[:8],
        "ai_authored": _ai_authored(index),
    }
    report["text"] = _digest(report)
    return report


__all__ = [
    "DIMENSIONS",
    "GRADES",
    "KEY_PROPERTIES",
    "MAX_EXAMPLES",
    "TITLES",
    "WEIGHTS",
    "assess_model_quality",
    "grade_for",
]
