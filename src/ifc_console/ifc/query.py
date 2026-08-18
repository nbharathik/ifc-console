"""query_elements builder: IfcOpenShell selector wrapper with pagination."""

from __future__ import annotations

from typing import Any

import ifcopenshell.util.element as element_util
import ifcopenshell.util.selector as selector_util

from ifc_console.core.results import ToolError

DEFAULT_FIELDS = ("name", "storey", "type_name")
ALLOWED_FIELDS = (
    "name",
    "predefined_type",
    "type_name",
    "storey",
    "description",
    "tag",
)

SYNTAX_HELP = """IfcOpenShell selector syntax (ifcopenshell.util.selector), by example:
  IfcWall                                all walls (subclasses included)
  IfcWall, IfcSlab                       union of classes
  IfcWall, material=concrete             walls AND material name contains value
  IfcWall, Name=Basic Wall:Interior      attribute equality
  IfcElement, Name=/W.*1/                regex match
  IfcWall, Pset_WallCommon.FireRating=F30    property facet
  IfcWall, Pset_WallCommon.IsExternal=TRUE   boolean property
  IfcWall, type=WAL01                    filter by type name
Combine facets with commas (AND within a class group). See the IfcOpenShell
selector docs for the full grammar."""


def element_row(element: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
    }
    if "name" in fields:
        row["name"] = getattr(element, "Name", None)
    if "predefined_type" in fields:
        pt = getattr(element, "PredefinedType", None)
        row["predefined_type"] = str(pt) if pt is not None else None
    if "type_name" in fields:
        try:
            etype = element_util.get_type(element)
            row["type_name"] = getattr(etype, "Name", None) if etype is not None else None
        except Exception:
            row["type_name"] = None
    if "storey" in fields:
        row["storey"] = _storey_name(element)
    if "description" in fields:
        row["description"] = getattr(element, "Description", None)
    if "tag" in fields:
        row["tag"] = getattr(element, "Tag", None)
    return row


def _storey_name(element: Any) -> str | None:
    try:
        container = element_util.get_container(element)
        while container is not None and not container.is_a("IfcBuildingStorey"):
            container = element_util.get_aggregate(container)
        return getattr(container, "Name", None) if container is not None else None
    except Exception:
        return None


def search_elements(ifc: Any, term: str) -> tuple[list[Any], str]:
    """Free-text element search for the viewer panel.

    Two behaviours behind one box. A term that reads like selector syntax
    (an IFC class, or any facet with `=`) runs through the selector, so the
    full grammar stays available. Anything else is a case-insensitive
    substring match over class, name, and type name, which is what someone
    typing "level 1" into a search box actually means.
    """
    if _looks_like_selector(term):
        try:
            return sorted(
                selector_util.filter_elements(ifc, term),
                key=lambda e: (e.is_a(), getattr(e, "Name", None) or "", e.id()),
            ), "selector"
        except Exception:
            pass  # not valid selector syntax after all; treat it as text
    needle = term.casefold()
    matches = []
    for entity in ifc.by_type("IfcProduct"):
        if needle in entity.is_a().casefold():
            matches.append(entity)
            continue
        for value in (
            getattr(entity, "GlobalId", None),
            getattr(entity, "Name", None),
            getattr(entity, "ObjectType", None),
        ):
            if value and needle in str(value).casefold():
                matches.append(entity)
                break
    matches.sort(key=lambda e: (e.is_a(), getattr(e, "Name", None) or "", e.id()))
    return matches, "text"


def _looks_like_selector(term: str) -> bool:
    stripped = term.strip()
    if "=" in stripped:
        return True
    # A bare class name only: "IfcWall" is a selector, "wall panel" is text.
    return stripped.casefold().startswith("ifc") and " " not in stripped


def run_query(
    ifc: Any,
    query: str,
    *,
    limit: int,
    offset: int,
    fields: tuple[str, ...],
    order_by: str,
) -> tuple[list[dict[str, Any]], int]:
    if not query.strip():
        raise ToolError(
            "INVALID_QUERY",
            "the selector is empty.",
            "Pass a selector such as `IfcWall`; see the syntax_help examples in data.",
            data={"syntax_help": SYNTAX_HELP},
        )
    try:
        elements = selector_util.filter_elements(ifc, query)
    except Exception as exc:
        raise ToolError(
            "INVALID_QUERY",
            f"selector parse/evaluation failed: {exc}",
            "Fix the query using the syntax_help examples in data.",
            data={"syntax_help": SYNTAX_HELP},
        ) from exc

    items = list(elements)
    if order_by == "name":
        items.sort(key=lambda e: (getattr(e, "Name", None) or "", e.id()))
    elif order_by == "storey":
        items.sort(key=lambda e: (_storey_name(e) or "", e.id()))
    else:
        items.sort(key=lambda e: (e.is_a(), getattr(e, "Name", None) or "", e.id()))

    total = len(items)
    page = items[offset : offset + limit]
    return [element_row(e, fields) for e in page], total


def unknown_classes(ifc: Any, query: str) -> list[str]:
    """Class names in the selector that this schema does not define.

    A typo and a class that is simply absent both return zero rows, so the
    caller names the typo rather than reporting an empty result.
    """
    import re

    import ifcopenshell

    schema = ifcopenshell.ifcopenshell_wrapper.schema_by_name(ifc.schema)
    unknown = []
    for token in re.findall(r"\bIfc\w+\b", query):
        try:
            schema.declaration_by_name(token)
        except Exception:
            if token not in unknown:
                unknown.append(token)
    return unknown
