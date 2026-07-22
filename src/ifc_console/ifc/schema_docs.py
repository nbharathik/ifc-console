"""get_schema_docs builder.

Combines ifcopenshell.util.doc (official documentation text) with wrapper
schema introspection (structural facts). Both sides degrade gracefully.
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
