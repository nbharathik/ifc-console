"""get_schema_docs builder.

Combines ifcopenshell.util.doc (official documentation text) with wrapper
schema introspection (structural facts) and the property set templates. All
three sides degrade gracefully.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ifc_console.mcp.envelope import ToolError

# Longest prefixes first: IFC4X3 must not fall into the IFC4 bucket.
_SUPPORTED = ("IFC4X3", "IFC2X3", "IFC4")


def _doc_schema_name(schema: str) -> str:
    s = (schema or "IFC4").upper()
    for known in _SUPPORTED:
        if s.startswith(known):
            return known
    return "IFC4"


def _doc_db(schema: str) -> dict[str, Any]:
    try:
        import ifcopenshell.util.doc as doc_util

        return doc_util.get_db(schema) or {}
    except Exception:
        return {}


def _templates(schema: str) -> Any:
    try:
        import ifcopenshell.util.pset as pset_util

        return pset_util.get_template(schema)
    except Exception:
        return None


def _property_types(schema: str, pset: str) -> dict[str, dict[str, Any]]:
    """Data types and enumerated values, which the doc text does not carry."""
    template = _templates(schema)
    if template is None:
        return {}
    try:
        definition = template.get_by_name(pset)
    except Exception:
        definition = None
    if definition is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for prop in definition.HasPropertyTemplates or []:
        entry: dict[str, Any] = {}
        with contextlib.suppress(Exception):
            entry["data_type"] = prop.PrimaryMeasureType
        with contextlib.suppress(Exception):
            if prop.Enumerators is not None:
                entry["values"] = [
                    v.wrappedValue for v in prop.Enumerators.EnumerationValues or []
                ]
        out[prop.Name] = entry
    out["__applicable__"] = {"applicable_to": definition.ApplicableEntity}
    return out


def build_pset_docs(schema: str, pset: str) -> dict[str, Any]:
    """Everything known about one property set: text, types, applicability."""
    schema_name = _doc_schema_name(schema)
    entry = (_doc_db(schema_name).get("properties") or {}).get(pset) or {}
    types = _property_types(schema_name, pset)
    applicable = types.pop("__applicable__", {}).get("applicable_to")
    doc_props = entry.get("properties") or {}
    if not entry and not types:
        raise ToolError(
            "NOT_FOUND",
            f"{pset!r} is not a property set of schema {schema_name}.",
            "Property set names look like Pset_WallCommon or Qto_WallBaseQuantities; "
            "search_ifc_knowledge finds them by keyword.",
        )
    properties = []
    for name in sorted(set(doc_props) | set(types)):
        doc_entry = doc_props.get(name)
        text = doc_entry.get("description") if isinstance(doc_entry, dict) else doc_entry
        properties.append(
            {
                "name": name,
                "selector": f"{pset}.{name}",
                **{k: v for k, v in (types.get(name) or {}).items() if v},
                **({"description": text} if text else {}),
            }
        )
    return {
        "schema": schema_name,
        "pset": pset,
        "kind": "qto" if pset.startswith("Qto_") else "pset",
        "definition": entry.get("description"),
        "spec_url": entry.get("spec_url"),
        "applicable_to": applicable,
        "properties": properties,
    }


def find_property(schema: str, name: str, limit: int = 20) -> dict[str, Any]:
    """Reverse lookup: which property sets define a property of this name."""
    schema_name = _doc_schema_name(schema)
    wanted = name.split(".")[-1].lower()
    matches: list[dict[str, Any]] = []
    for pset, entry in (_doc_db(schema_name).get("properties") or {}).items():
        for prop, doc_entry in (entry.get("properties") or {}).items():
            if prop.lower() != wanted:
                continue
            text = doc_entry.get("description") if isinstance(doc_entry, dict) else doc_entry
            matches.append(
                {
                    "pset": pset,
                    "property": prop,
                    "selector": f"{pset}.{prop}",
                    "description": text,
                }
            )
    exact = [m for m in matches if m["property"].lower() == wanted]
    return {
        "schema": schema_name,
        "property": name,
        "found": len(exact),
        "defined_in": exact[:limit],
    }


def applicable_psets(schema: str, entity: str) -> list[str]:
    template = _templates(_doc_schema_name(schema))
    if template is None:
        return []
    try:
        return sorted(template.get_applicable_names(entity, pset_only=False))
    except Exception:
        return []


def build_schema_docs(schema: str, entity: str, attribute: str | None) -> dict[str, Any]:
    schema_name = _doc_schema_name(schema)
    out: dict[str, Any] = {"schema": schema_name, "entity": entity}

    doc_data: dict[str, Any] | None = None
    try:
        import ifcopenshell.util.doc as doc_util

        doc_data = doc_util.get_entity_doc(schema_name, entity)
    except Exception:
        doc_data = None
    if doc_data:
        out["definition"] = doc_data.get("description")
        out["spec_url"] = doc_data.get("spec_url")

    decl = None
    try:
        import ifcopenshell.ifcopenshell_wrapper as wrapper

        decl = wrapper.schema_by_name(schema_name).declaration_by_name(entity)
    except Exception:
        decl = None

    if decl is None and not doc_data:
        raise ToolError(
            "NOT_FOUND",
            f"{entity!r} is not an entity of schema {schema_name}.",
            "Check the spelling (e.g. IfcWall, IfcBuildingStorey); entity names are "
            "case-sensitive in some lookups.",
        )

    attr_docs: dict[str, Any] = (doc_data or {}).get("attributes") or {}

    if decl is not None:
        supertypes: list[str] = []
        try:
            parent = decl.supertype()
            while parent is not None:
                supertypes.append(parent.name())
                parent = parent.supertype()
        except Exception:
            pass
        out["supertypes"] = supertypes

        attributes: list[dict[str, Any]] = []
        try:
            for attr in decl.all_attributes():
                name = attr.name()
                entry: dict[str, Any] = {"name": name}
                with contextlib.suppress(Exception):
                    entry["type"] = str(attr.type_of_attribute())
                with contextlib.suppress(Exception):
                    entry["optional"] = bool(attr.optional())
                doc_text = attr_docs.get(name)
                if isinstance(doc_text, dict):
                    doc_text = doc_text.get("description")
                if doc_text:
                    entry["description"] = doc_text
                attributes.append(entry)
        except Exception:
            pass
        out["attributes"] = attributes

        predefined: list[str] = []
        try:
            for attr in decl.all_attributes():
                if attr.name() != "PredefinedType":
                    continue
                t = attr.type_of_attribute()
                declared = t.declared_type() if hasattr(t, "declared_type") else t
                if hasattr(declared, "enumeration_items"):
                    predefined = list(declared.enumeration_items())
        except Exception:
            predefined = []
        if predefined:
            out["predefined_types"] = predefined

    psets = applicable_psets(schema_name, entity)
    if psets:
        out["applicable_psets"] = psets

    if attribute:
        block: dict[str, Any] = {"name": attribute}
        try:
            import ifcopenshell.util.doc as doc_util

            block["description"] = doc_util.get_attribute_doc(schema_name, entity, attribute)
        except Exception:
            block["description"] = None
        doc_entry = attr_docs.get(attribute)
        if isinstance(doc_entry, dict) and not block.get("description"):
            block["description"] = doc_entry.get("description")
        out["attribute"] = block

    return out
