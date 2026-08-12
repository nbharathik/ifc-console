"""get_spatial_structure builder: the decomposition tree with element counts."""

from __future__ import annotations

from typing import Any

from ifc_console.core.results import ToolError


def _node(obj: Any, include_counts: bool) -> dict[str, Any]:
    node: dict[str, Any] = {
        "class": obj.is_a(),
        "name": getattr(obj, "Name", None),
        "global_id": getattr(obj, "GlobalId", None),
    }
    if obj.is_a("IfcBuildingStorey"):
        node["elevation"] = getattr(obj, "Elevation", None)
    if include_counts:
        count = 0
        for rel in getattr(obj, "ContainsElements", ()) or ():
            count += len(rel.RelatedElements or ())
        node["element_count"] = count  # direct containment only, not recursive
    return node


def build_spatial_tree(
    ifc: Any,
    root_global_id: str | None,
    depth: int,
    include_counts: bool,
) -> dict[str, Any]:
    if root_global_id:
        try:
            root = ifc.by_guid(root_global_id)
        except Exception:
            root = None
        if root is None:
            raise ToolError(
                "NOT_FOUND",
                f"no entity with GlobalId {root_global_id!r}.",
                "Use query_elements or get_spatial_structure without root_global_id.",
            )
    else:
        projects = ifc.by_type("IfcProject")
        if not projects:
            raise ToolError(
                "NOT_FOUND",
                "the model has no IfcProject.",
                "The file may be a fragment; query entities directly instead.",
            )
        root = projects[0]

    visited: set[int] = set()

    def walk(obj: Any, remaining: int) -> dict[str, Any]:
        node = _node(obj, include_counts)
        if obj.id() in visited:
            node["children"] = "…[cycle]"
            return node
        visited.add(obj.id())
        if remaining <= 0:
            return node
        children = []
        for rel in getattr(obj, "IsDecomposedBy", ()) or ():
            for child in rel.RelatedObjects or ():
                children.append(walk(child, remaining - 1))
        if children:
            node["children"] = children
        return node

    return walk(root, depth)
