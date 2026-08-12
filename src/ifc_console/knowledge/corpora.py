"""Extractors that turn what ships with IfcOpenShell into knowledge records.

Nothing here downloads anything. The IFC documentation database and the
`ifcopenshell.api` source are already on disk in the installed wheel; these
functions read them and normalize them into one record shape.
"""

from __future__ import annotations

import ast
import contextlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ifc_console.knowledge.records import Record

SCHEMAS = ("IFC2X3", "IFC4", "IFC4X3")


def _first_paragraph(text: str, limit: int = 240) -> str:
    para = (text or "").strip().split("\n\n")[0].replace("\n", " ").strip()
    return para[: limit - 1] + "…" if len(para) > limit else para


# ------------------------------------------------------------------ IFC schema


def _declaration(schema: str, name: str) -> Any:
    try:
        import ifcopenshell.ifcopenshell_wrapper as wrapper

        return wrapper.schema_by_name(schema).declaration_by_name(name)
    except Exception:
        return None


def _entity_facts(schema: str, name: str) -> dict[str, Any]:
    """Supertype chain and attribute list, straight from the parsed schema."""
    decl = _declaration(schema, name)
    if decl is None:
        return {}
    facts: dict[str, Any] = {}
    supertypes: list[str] = []
    with contextlib.suppress(Exception):
        parent = decl.supertype()
        while parent is not None:
            supertypes.append(parent.name())
            parent = parent.supertype()
    if supertypes:
        facts["supertypes"] = supertypes
    attributes: list[dict[str, Any]] = []
    with contextlib.suppress(Exception):
        for attr in decl.all_attributes():
            entry: dict[str, Any] = {"name": attr.name()}
            with contextlib.suppress(Exception):
                entry["type"] = str(attr.type_of_attribute())
            with contextlib.suppress(Exception):
                entry["optional"] = bool(attr.optional())
            attributes.append(entry)
    if attributes:
        facts["attributes"] = attributes
    return facts


def entities(schema: str) -> Iterator[Record]:
    import ifcopenshell.util.doc as doc_util

    db = doc_util.get_db(schema)
    for name, entry in (db.get("entities") or {}).items():
        description = entry.get("description") or ""
        facts = _entity_facts(schema, name)
        predefined = sorted((entry.get("predefined_types") or {}).keys())
        lines = [description]
        if facts.get("supertypes"):
            lines.append("Supertypes: " + " > ".join(facts["supertypes"]))
        if facts.get("attributes"):
            lines.append(
                "Attributes: "
                + ", ".join(
                    f"{a['name']}{'?' if a.get('optional') else ''}"
                    for a in facts["attributes"]
                )
            )
        if predefined:
            lines.append("PredefinedType values: " + ", ".join(predefined))
        yield Record(
            kind="entity",
            key=f"entity:{schema}:{name}",
            name=name,
            schema=schema,
            summary=_first_paragraph(description) or f"{name} ({schema} entity)",
            body="\n\n".join(part for part in lines if part),
            meta={
                "spec_url": entry.get("spec_url"),
                "predefined_types": predefined,
                **facts,
            },
        )


def types(schema: str) -> Iterator[Record]:
    import ifcopenshell.util.doc as doc_util

    db = doc_util.get_db(schema)
    for name, entry in (db.get("types") or {}).items():
        description = entry.get("description") or ""
        yield Record(
            kind="type",
            key=f"type:{schema}:{name}",
            name=name,
            schema=schema,
            summary=_first_paragraph(description) or f"{name} ({schema} defined type)",
            body=description,
            meta={"spec_url": entry.get("spec_url")},
        )


def _pset_templates(schema: str) -> dict[str, dict[str, Any]]:
    """Applicability and data types, which the documentation database lacks."""
    out: dict[str, dict[str, Any]] = {}
    try:
        import ifcopenshell.util.pset as pset_util

        template = pset_util.get_template(schema)
    except Exception:
        return out
    for template_file in getattr(template, "templates", []):
        for definition in template_file.by_type("IfcPropertySetTemplate"):
            props: dict[str, dict[str, Any]] = {}
            for prop in definition.HasPropertyTemplates or []:
                entry: dict[str, Any] = {}
                with contextlib.suppress(Exception):
                    entry["data_type"] = prop.PrimaryMeasureType
                with contextlib.suppress(Exception):
                    if prop.Enumerators is not None:
                        entry["values"] = [
                            v.wrappedValue for v in prop.Enumerators.EnumerationValues or []
                        ]
                props[prop.Name] = entry
            out[definition.Name] = {
                "applicable_to": definition.ApplicableEntity,
                "properties": props,
                "template_type": definition.TemplateType,
            }
    return out


def property_sets(schema: str) -> Iterator[Record]:
    """One record per property set, plus one per property for reverse lookup."""
    import ifcopenshell.util.doc as doc_util

    db = doc_util.get_db(schema)
    templates = _pset_templates(schema)
    documented = db.get("properties") or {}
    names = sorted(set(documented) | set(templates))

    for name in names:
        entry = documented.get(name) or {}
        template = templates.get(name) or {}
        description = entry.get("description") or ""
        doc_props = entry.get("properties") or {}
        tmpl_props = template.get("properties") or {}
        applicable = template.get("applicable_to")
        prop_names = sorted(set(doc_props) | set(tmpl_props))

        lines = [description]
        if applicable:
            lines.append(f"Applies to: {applicable}")
        for prop in prop_names:
            doc_entry = doc_props.get(prop)
            text = doc_entry.get("description") if isinstance(doc_entry, dict) else doc_entry
            data_type = (tmpl_props.get(prop) or {}).get("data_type")
            suffix = f" ({data_type})" if data_type else ""
            lines.append(f"{prop}{suffix}: {text or ''}".rstrip())

        yield Record(
            kind="pset",
            key=f"pset:{schema}:{name}",
            name=name,
            schema=schema,
            summary=_first_paragraph(description) or f"{name} property set",
            body="\n".join(part for part in lines if part),
            meta={
                "spec_url": entry.get("spec_url"),
                "applicable_to": applicable,
                "properties": prop_names,
                "kind": "qto" if name.startswith("Qto_") else "pset",
            },
        )

        for prop in prop_names:
            doc_entry = doc_props.get(prop)
            text = doc_entry.get("description") if isinstance(doc_entry, dict) else doc_entry
            data_type = (tmpl_props.get(prop) or {}).get("data_type")
            values = (tmpl_props.get(prop) or {}).get("values")
            summary = _first_paragraph(text or "") or f"{prop} in {name}"
            yield Record(
                kind="property",
                key=f"property:{schema}:{name}.{prop}",
                name=f"{name}.{prop}",
                schema=schema,
                summary=summary,
                body=(text or ""),
                meta={
                    "pset": name,
                    "property": prop,
                    "data_type": data_type,
                    "values": values,
                    "applicable_to": applicable,
                    "selector": f"{name}.{prop}",
                    "aliases": [prop],
                },
            )


# ------------------------------------------------------- IfcOpenShell API docs


def _signature(node: ast.FunctionDef) -> str:
    args = node.args
    parts: list[str] = []
    defaults: list[Any] = [None] * (len(args.args) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(args.args, defaults, strict=False):
        text = arg.arg
        if arg.annotation is not None:
            with contextlib.suppress(Exception):
                text += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            with contextlib.suppress(Exception):
                text += f" = {ast.unparse(default)}"
        parts.append(text)
    if args.vararg:
        parts.append(f"*{args.vararg.arg}")
    for arg in args.kwonlyargs:
        parts.append(arg.arg)
    if args.kwarg:
        parts.append(f"**{args.kwarg.arg}")
    return f"{node.name}({', '.join(parts)})"


def api_functions() -> Iterator[Record]:
    import ifcopenshell

    root = Path(ifcopenshell.__file__).parent / "api"
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*.py")):
        if path.name.startswith("_") or path.name == "__init__.py":
            continue
        module = f"{path.parent.name}.{path.stem}"
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
                continue
            doc = ast.get_docstring(node) or ""
            signature = _signature(node)
            call = f"ifc_api.{module}("
            body = (
                f"Call: ifc_api.{module}({signature.split('(', 1)[1]}\n"
                f"Signature: {signature}\n\n{doc}"
            )
            yield Record(
                kind="api",
                key=f"api:{module}",
                name=module,
                summary=_first_paragraph(doc) or f"ifcopenshell.api.{module}",
                body=body,
                meta={
                    "signature": signature,
                    "call": call,
                    "module": f"ifcopenshell.api.{module}",
                    "aliases": [node.name, path.parent.name],
                },
            )
