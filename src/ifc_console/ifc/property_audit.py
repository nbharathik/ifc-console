"""Property completeness per element: what the schema expects, what is there.

The property set templates shipped with ifcopenshell say which property and
quantity sets apply to an entity and its predefined type. Reading them against
what an element or its type actually carries gives an exact gap list, which
is what an agent needs before it can derive or propose a missing value.

The index is one pass over the relationship entities. get_psets per element
walks the inverse graph every time, which is too slow for a whole-model score.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable
from typing import Any

from ifc_console.core.results import ToolError

MAX_ELEMENTS = 50
PSET_SCOPES = ("core", "present", "all")
DETAILS = ("compact", "full")

_SUPPORTED = ("IFC4X3", "IFC2X3", "IFC4")

# Quantity names the console can derive from geometry, so a gap there is a
# compute_quantities or analyze_element_geometry call away.
GEOMETRY_QUANTITIES = frozenset(
    {
        "Length",
        "Width",
        "Height",
        "Depth",
        "Perimeter",
        "Thickness",
        "GrossFootprintArea",
        "NetFootprintArea",
        "GrossSideArea",
        "NetSideArea",
        "GrossVolume",
        "NetVolume",
        "GrossArea",
        "NetArea",
        "GrossSurfaceArea",
        "OuterSurfaceArea",
        "CrossSectionArea",
        "GrossFloorArea",
        "NetFloorArea",
        "GrossCeilingArea",
        "NetCeilingArea",
        "GrossWallArea",
        "NetWallArea",
        "GrossPerimeter",
        "NetPerimeter",
        "GrossTopArea",
        "NetTopArea",
        "TotalSurfaceArea",
        "FinishFloorHeight",
        "FinishCeilingHeight",
        "Area",
    }
)

# Where a missing property is usually filled from. Guidance for choosing the
# cheapest evidence first, not a promise that it can be filled.
PROPERTY_HINTS: dict[str, tuple[str, str]] = {
    "IsExternal": (
        "spatial",
        "position on the envelope: space boundaries, footprint edge, or query_spatial",
    ),
    "LoadBearing": (
        "type",
        "type name, material, and the structural model; an inference unless a document states it",
    ),
    "FireRating": (
        "document",
        "fire certificate, product data sheet, or the specification for this type",
    ),
    "AcousticRating": ("document", "acoustic test report or the product data sheet"),
    "ThermalTransmittance": (
        "document",
        "U-value from the product data sheet or a layer calculation",
    ),
    "Combustible": ("document", "material classification or the product data sheet"),
    "SurfaceSpreadOfFlame": ("document", "fire classification of the finish"),
    "Compartmentation": ("document", "the fire strategy drawing"),
    "ExtendToStructure": ("geometry", "compare the element top with the slab or storey above"),
    "Reference": ("type", "the type name, Tag, or classification code"),
    "Status": ("project", "the project phase: NEW, EXISTING, DEMOLISH, or TEMPORARY"),
    "Span": ("geometry", "the clear length between supports"),
    "Slope": ("geometry", "the inclination of the analysis axis"),
    "Roll": ("geometry", "rotation about the longitudinal axis"),
    "PitchAngle": ("geometry", "the inclination of the roof surface"),
    "GlazingAreaFraction": ("geometry", "glazed area over the opening area"),
    "FireExit": ("document", "the fire strategy drawing"),
    "HandicapAccessible": ("document", "the accessibility schedule"),
    "SelfClosing": ("document", "the door schedule or hardware set"),
    "SecurityRating": ("document", "the door schedule"),
    "SmokeStop": ("document", "the fire strategy drawing"),
    "Infiltration": ("document", "the product data sheet"),
    "GrossWeight": ("material", "volume times the material density from Pset_MaterialCommon"),
    "NetWeight": ("material", "volume times the material density from Pset_MaterialCommon"),
    "Weight": ("material", "volume times the material density from Pset_MaterialCommon"),
    "ConcreteCover": ("document", "the structural specification"),
    "StrengthClass": ("document", "the structural specification or material name"),
}

_MANUFACTURER_PSETS = ("Pset_ManufacturerTypeInformation", "Pset_ManufacturerOccurrence")


def template_schema(schema: str) -> str:
    text = (schema or "IFC4").upper()
    for known in _SUPPORTED:
        if text.startswith(known):
            return known
    return "IFC4"


def load_templates(schema: str) -> Any:
    try:
        import ifcopenshell.util.pset as pset_util

        return pset_util.get_template(template_schema(schema))
    except Exception:
        return None


def _wrapped(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "wrappedValue"):
        return value.wrappedValue
    return value


def property_value(prop: Any) -> Any:
    """The scalar or list a property or quantity carries, None when empty."""
    for attr in (
        "NominalValue",
        "LengthValue",
        "AreaValue",
        "VolumeValue",
        "CountValue",
        "WeightValue",
        "TimeValue",
        "NumberValue",
    ):
        if hasattr(prop, attr):
            return _wrapped(getattr(prop, attr))
    if hasattr(prop, "EnumerationValues"):
        values = [_wrapped(v) for v in (prop.EnumerationValues or ())]
        return values or None
    if hasattr(prop, "ListValues"):
        values = [_wrapped(v) for v in (prop.ListValues or ())]
        return values or None
    if hasattr(prop, "UpperBoundValue") or hasattr(prop, "LowerBoundValue"):
        upper = _wrapped(getattr(prop, "UpperBoundValue", None))
        lower = _wrapped(getattr(prop, "LowerBoundValue", None))
        if upper is None and lower is None:
            return None
        return {"lower": lower, "upper": upper}
    if hasattr(prop, "PropertyReference"):
        ref = prop.PropertyReference
        return ref.is_a() if ref is not None else None
    return None


def definition_values(definition: Any) -> dict[str, Any]:
    """Property name to value for one property set or quantity set."""
    out: dict[str, Any] = {}
    members: Iterable[Any] = ()
    if definition.is_a("IfcElementQuantity"):
        members = definition.Quantities or ()
    elif definition.is_a("IfcPropertySet"):
        members = definition.HasProperties or ()
    for member in members:
        name = getattr(member, "Name", None)
        if not name:
            continue
        try:
            out[name] = property_value(member)
        except Exception:
            out[name] = None
    return out


def material_label(material: Any, depth: int = 0) -> str | None:
    if material is None or depth > 3:
        return None
    if material.is_a("IfcMaterial"):
        return material.Name
    if material.is_a("IfcMaterialLayerSetUsage"):
        return material_label(material.ForLayerSet, depth + 1)
    if material.is_a("IfcMaterialProfileSetUsage"):
        return material_label(material.ForProfileSet, depth + 1)
    for attr in ("LayerSetName", "Name"):
        with contextlib.suppress(Exception):
            name = getattr(material, attr)
            if name:
                return str(name)
    for attr in ("MaterialLayers", "MaterialProfiles", "MaterialConstituents", "Materials"):
        with contextlib.suppress(Exception):
            names = []
            for part in getattr(material, attr) or ():
                inner = getattr(part, "Material", part)
                label = material_label(inner, depth + 1)
                if label:
                    names.append(label)
            if names:
                return " / ".join(dict.fromkeys(names))
    return None


class PropertyIndex:
    """Element and type property sets, materials, classification, and
    containment, built from one pass over the relationship entities."""

    def __init__(self, ifc: Any) -> None:
        self.ifc = ifc
        self.own: dict[int, dict[str, Any]] = {}
        self.type_of: dict[int, Any] = {}
        self.type_sets: dict[int, dict[str, Any]] = {}
        self.material_of: dict[int, Any] = {}
        self.classified: set[int] = set()
        self.container_of: dict[int, Any] = {}
        self.parent_of: dict[int, Any] = {}
        self._build()

    @staticmethod
    def _add(target: dict[int, dict[str, Any]], obj: Any, definition: Any) -> None:
        name = getattr(definition, "Name", None)
        if name:
            target.setdefault(obj.id(), {})[name] = definition

    def _build(self) -> None:
        ifc = self.ifc
        for rel in ifc.by_type("IfcRelDefinesByProperties"):
            definitions = rel.RelatingPropertyDefinition
            if definitions is None:
                continue
            if not isinstance(definitions, (list, tuple)):
                definitions = (definitions,)
            for obj in rel.RelatedObjects or ():
                for definition in definitions:
                    self._add(self.own, obj, definition)
        for rel in ifc.by_type("IfcRelDefinesByType"):
            if rel.RelatingType is None:
                continue
            for obj in rel.RelatedObjects or ():
                self.type_of[obj.id()] = rel.RelatingType
        for type_object in ifc.by_type("IfcTypeObject"):
            for definition in getattr(type_object, "HasPropertySets", None) or ():
                self._add(self.type_sets, type_object, definition)
        for rel in ifc.by_type("IfcRelAssociatesMaterial"):
            for obj in rel.RelatedObjects or ():
                self.material_of[obj.id()] = rel.RelatingMaterial
        for rel in ifc.by_type("IfcRelAssociatesClassification"):
            for obj in rel.RelatedObjects or ():
                self.classified.add(obj.id())
        for rel in ifc.by_type("IfcRelContainedInSpatialStructure"):
            for obj in rel.RelatedElements or ():
                self.container_of[obj.id()] = rel.RelatingStructure
        for rel in ifc.by_type("IfcRelAggregates"):
            for obj in rel.RelatedObjects or ():
                self.parent_of[obj.id()] = rel.RelatingObject

    def type_object(self, element: Any) -> Any:
        return self.type_of.get(element.id())

    def own_sets(self, element: Any) -> dict[str, Any]:
        return self.own.get(element.id(), {})

    def inherited_sets(self, element: Any) -> dict[str, Any]:
        type_object = self.type_object(element)
        if type_object is None:
            return {}
        return self.type_sets.get(type_object.id(), {})

    def material(self, element: Any) -> Any:
        found = self.material_of.get(element.id())
        if found is not None:
            return found
        type_object = self.type_object(element)
        if type_object is not None:
            return self.material_of.get(type_object.id())
        return None

    def is_classified(self, element: Any) -> bool:
        if element.id() in self.classified:
            return True
        type_object = self.type_object(element)
        return type_object is not None and type_object.id() in self.classified

    @staticmethod
    def _is_spatial(obj: Any) -> bool:
        return obj.is_a("IfcSpatialStructureElement") or obj.is_a("IfcSpatialElement")

    def _spatial_parent(self, element: Any) -> Any:
        """The first spatial element above an element, through containment or
        an aggregate whose root is contained."""
        current = element
        for _ in range(12):
            container = self.container_of.get(current.id())
            if container is not None:
                return container
            parent = self.parent_of.get(current.id())
            if parent is None:
                return None
            if self._is_spatial(parent):
                return parent
            current = parent
        return None

    def storey(self, element: Any) -> Any:
        current = self._spatial_parent(element)
        for _ in range(8):
            if current is None:
                return None
            if current.is_a("IfcBuildingStorey"):
                return current
            current = self.parent_of.get(current.id())
        return None

    def is_contained(self, element: Any) -> bool:
        return self._spatial_parent(element) is not None


def predefined_type_of(element: Any) -> str:
    value = getattr(element, "PredefinedType", None)
    if value is None or value == "NOTDEFINED":
        return ""
    if value == "USERDEFINED":
        return str(getattr(element, "ObjectType", None) or "")
    return str(value)


def _stem(ifc_class: str) -> str:
    stem = ifc_class[3:] if ifc_class.startswith("Ifc") else ifc_class
    for suffix in ("StandardCase", "ElementedCase"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def core_set_names(ifc_class: str, applicable: Iterable[str]) -> tuple[str | None, str | None]:
    """The common property set and base quantity set for a class, when the
    templates define them."""
    names = set(applicable)
    stem = _stem(ifc_class)
    pset = f"Pset_{stem}Common"
    qto = f"Qto_{stem}BaseQuantities"
    return (pset if pset in names else None, qto if qto in names else None)


def applicable_templates(template: Any, ifc_class: str, predefined: str = "") -> list[Any]:
    if template is None:
        return []
    try:
        found = list(template.get_applicable(ifc_class, predefined, pset_only=False))
    except Exception:
        found = []
    if predefined:
        # Templates keyed only on the class still apply to every predefined type.
        with contextlib.suppress(Exception):
            for entry in template.get_applicable(ifc_class, "", pset_only=False):
                if entry not in found:
                    found.append(entry)
    return found


def template_rows(definition: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for prop in definition.HasPropertyTemplates or []:
        entry: dict[str, Any] = {"name": prop.Name}
        with contextlib.suppress(Exception):
            entry["data_type"] = prop.PrimaryMeasureType
        with contextlib.suppress(Exception):
            enum = prop.Enumerators
            if enum is not None and enum.EnumerationValues:
                entry["enumeration"] = [_wrapped(v) for v in enum.EnumerationValues]
        rows.append(entry)
    return rows


def _applies_to(template_type: str | None) -> str:
    text = template_type or ""
    if "TYPEDRIVENONLY" in text:
        return "type"
    if "OCCURRENCEDRIVEN" in text:
        return "occurrence"
    return "either"


def derivation_hint(pset_name: str, property_name: str, kind: str) -> tuple[str, str]:
    if property_name in PROPERTY_HINTS:
        return PROPERTY_HINTS[property_name]
    if kind == "qto" and property_name in GEOMETRY_QUANTITIES:
        return ("geometry", "compute_quantities source='derived' or analyze_element_geometry")
    if kind == "qto":
        return ("geometry", "a geometry probe, if the quantity has a geometric meaning")
    if pset_name in _MANUFACTURER_PSETS:
        return ("document", "manufacturer data, product code, or the type name")
    return ("manual", "no deterministic source; needs a document or a person")


def audit_element(
    element: Any,
    index: PropertyIndex,
    template: Any,
    *,
    psets: str = "core",
    detail: str = "compact",
    pset_names: Iterable[str] | None = None,
) -> dict[str, Any]:
    ifc_class = element.is_a()
    predefined = predefined_type_of(element)
    own = index.own_sets(element)
    inherited = index.inherited_sets(element)
    type_object = index.type_object(element)
    applicable = applicable_templates(template, ifc_class, predefined)
    applicable_names = [entry.Name for entry in applicable]
    common, base = core_set_names(ifc_class, applicable_names)

    wanted: set[str] | None
    if pset_names:
        wanted = set(pset_names)
    elif psets == "all":
        wanted = None
    elif psets == "present":
        wanted = set(own) | set(inherited)
    else:
        wanted = {name for name in (common, base) if name} | set(own) | set(inherited)

    rows: list[dict[str, Any]] = []
    expected = filled = 0
    derivable: dict[str, list[str]] = {}
    for definition in applicable:
        name = definition.Name
        if wanted is not None and name not in wanted:
            continue
        kind = "qto" if str(definition.TemplateType or "").startswith("QTO") else "pset"
        occurrence_values = definition_values(own[name]) if name in own else None
        type_values = definition_values(inherited[name]) if name in inherited else None
        if occurrence_values is not None and type_values is not None:
            defined_on = "both"
        elif occurrence_values is not None:
            defined_on = "occurrence"
        elif type_values is not None:
            defined_on = "type"
        else:
            defined_on = "none"
        properties: list[dict[str, Any]] = []
        filled_names: list[str] = []
        missing_names: list[str] = []
        for row in template_rows(definition):
            prop = row["name"]
            value = source = None
            if occurrence_values is not None and occurrence_values.get(prop) is not None:
                value, source = occurrence_values[prop], "occurrence"
            elif type_values is not None and type_values.get(prop) is not None:
                value, source = type_values[prop], "type"
            expected += 1
            if value is not None:
                filled += 1
                filled_names.append(prop)
                status = "filled"
            else:
                present_empty = (occurrence_values is not None and prop in occurrence_values) or (
                    type_values is not None and prop in type_values
                )
                status = "empty" if present_empty else "missing"
                missing_names.append(prop)
                source_kind, _ = derivation_hint(name, prop, kind)
                derivable.setdefault(source_kind, []).append(f"{name}.{prop}")
            if detail == "full":
                entry = {**row, "status": status}
                if value is not None:
                    entry["value"] = value
                    entry["source"] = source
                else:
                    entry["derivable"] = derivation_hint(name, prop, kind)[0]
                properties.append(entry)
        row_out: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "priority": "core" if name in (common, base) else "standard",
            "applies_to": _applies_to(definition.TemplateType),
            "defined_on": defined_on,
            "filled": filled_names,
            "missing": missing_names,
        }
        if detail == "full":
            row_out["properties"] = properties
        rows.append(row_out)

    # Sets no template describes: project or vendor sets, and the AI namespace.
    from ifc_console.ifc.ai_provenance import is_ai_authored

    custom = sorted((set(own) | set(inherited)) - set(applicable_names))
    ai_marked = [name for name in custom if is_ai_authored(name)]
    custom = [name for name in custom if name not in ai_marked]

    storey = index.storey(element)
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": ifc_class,
        "name": getattr(element, "Name", None),
        "predefined_type": predefined or None,
        "type": (
            {
                "class": type_object.is_a(),
                "name": getattr(type_object, "Name", None),
                "global_id": getattr(type_object, "GlobalId", None),
            }
            if type_object is not None
            else None
        ),
        "storey": getattr(storey, "Name", None) if storey is not None else None,
        "material": material_label(index.material(element)),
        "has_representation": getattr(element, "Representation", None) is not None,
        "core_sets": {"pset": common, "qto": base},
        "applicable_psets": applicable_names,
        "psets": rows,
        "custom_psets": custom,
        "ai_authored_psets": ai_marked,
        "derivable": derivable,
        "summary": {
            "expected": expected,
            "filled": filled,
            "missing": expected - filled,
            "completeness": round(filled / expected, 3) if expected else None,
        },
    }


def _resolve_elements(
    ifc: Any,
    *,
    selector: str | None,
    global_ids: Iterable[str] | None,
    max_elements: int,
) -> tuple[list[Any], list[str], int]:
    found: list[Any] = []
    missing: list[str] = []
    if global_ids:
        for gid in dict.fromkeys(global_ids):
            try:
                element = ifc.by_guid(gid)
            except Exception:
                element = None
            if element is None:
                missing.append(gid)
            else:
                found.append(element)
    elif selector:
        import ifcopenshell.util.selector as selector_util

        try:
            found = list(selector_util.filter_elements(ifc, selector))
        except Exception as exc:
            raise ToolError(
                "INVALID_QUERY",
                f"selector parse/evaluation failed: {exc}",
                "Pass a selector such as `IfcWall` or explicit global_ids.",
            ) from exc
        found.sort(key=lambda e: (e.is_a(), getattr(e, "Name", None) or "", e.id()))
    else:
        raise ToolError(
            "INVALID_INPUT",
            "pass a selector or global_ids",
            "Use the viewer selection GlobalIds, or a selector such as `IfcWall`.",
        )
    return found[:max_elements], missing, len(found)


def audit_element_properties(
    ifc: Any,
    *,
    selector: str | None = None,
    global_ids: Iterable[str] | None = None,
    psets: str = "core",
    pset_names: Iterable[str] | None = None,
    detail: str = "compact",
    max_elements: int = 10,
) -> dict[str, Any]:
    """Expected versus present properties for a few elements."""
    if psets not in PSET_SCOPES:
        raise ToolError(
            "INVALID_INPUT", f"unknown psets scope {psets!r}", f"Allowed: {list(PSET_SCOPES)}."
        )
    if detail not in DETAILS:
        raise ToolError("INVALID_INPUT", f"unknown detail {detail!r}", f"Allowed: {list(DETAILS)}.")
    max_elements = max(1, min(int(max_elements), MAX_ELEMENTS))
    elements, missing, total = _resolve_elements(
        ifc, selector=selector, global_ids=global_ids, max_elements=max_elements
    )
    template = load_templates(ifc.schema)
    index = PropertyIndex(ifc)
    wanted_names = list(pset_names) if pset_names else None
    rows = [
        audit_element(element, index, template, psets=psets, detail=detail, pset_names=wanted_names)
        for element in elements
    ]

    gap_counts: dict[str, int] = {}
    expected = filled = 0
    # Hints are stated once per property name, not once per element and set.
    hints: dict[str, str] = {}
    for row in rows:
        expected += row["summary"]["expected"]
        filled += row["summary"]["filled"]
        for pset in row["psets"]:
            for prop in pset["missing"]:
                key = f"{pset['name']}.{prop}"
                gap_counts[key] = gap_counts.get(key, 0) + 1
                hints.setdefault(prop, derivation_hint(pset["name"], prop, pset["kind"])[1])
    most_missing = [
        {"property": key, "elements": count}
        for key, count in sorted(gap_counts.items(), key=lambda item: (-item[1], item[0]))[:25]
    ]
    return {
        "schema": ifc.schema,
        "templates": template_schema(ifc.schema) if template is not None else None,
        "scope": {"psets": psets, "pset_names": sorted(wanted_names) if wanted_names else None},
        "elements": rows,
        "hints": dict(sorted(hints.items())),
        "missing": missing,
        "returned": len(rows),
        "total": total,
        "truncated": total > len(rows),
        "summary": {
            "elements": len(rows),
            "expected": expected,
            "filled": filled,
            "missing": expected - filled,
            "completeness": round(filled / expected, 3) if expected else None,
            "most_missing": most_missing,
        },
    }


__all__ = [
    "DETAILS",
    "GEOMETRY_QUANTITIES",
    "MAX_ELEMENTS",
    "PROPERTY_HINTS",
    "PSET_SCOPES",
    "PropertyIndex",
    "applicable_templates",
    "audit_element",
    "audit_element_properties",
    "core_set_names",
    "definition_values",
    "derivation_hint",
    "load_templates",
    "material_label",
    "predefined_type_of",
    "property_value",
    "template_rows",
    "template_schema",
]
