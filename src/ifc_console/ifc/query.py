"""query_elements builder: IfcOpenShell selector wrapper with pagination."""

from __future__ import annotations

from typing import Any

import ifcopenshell.util.element as element_util
import ifcopenshell.util.selector as selector_util

from ifc_console.mcp.envelope import ToolError

DEFAULT_FIELDS = ("name", "storey", "type_name")
ALLOWED_FIELDS = (
    "name", "predefined_type", "type_name", "storey", "description", "tag",
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


def run_query(
    ifc: Any,
    query: str,
    *,
    limit: int,
    offset: int,
    fields: tuple[str, ...],
    order_by: str,
) -> tuple[list[dict[str, Any]], int]:
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
