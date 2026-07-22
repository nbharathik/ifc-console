"""get_element detail builder: attributes, psets, materials, containment."""

from __future__ import annotations

from typing import Any

import ifcopenshell
import ifcopenshell.util.element as element_util

INCLUDE_DEFAULT = ("attributes", "psets", "type", "container")
INCLUDE_ALLOWED = (
    "attributes", "psets", "qtos", "materials", "type", "container",
    "openings", "decomposition",
)

_SKIP_ATTRS = {"OwnerHistory", "Representation", "ObjectPlacement"}
_LIST_CAP = 20


def _sanitize(value: Any, depth: int = 0) -> Any:
    if isinstance(value, ifcopenshell.entity_instance):
        name = getattr(value, "Name", None)
        label = f"#{value.id()}={value.is_a()}"
        return f"{label}({name})" if name else label
    if isinstance(value, (list, tuple)):
        items = [_sanitize(v, depth + 1) for v in value[:_LIST_CAP]]
        if len(value) > _LIST_CAP:
            items.append(f"…+{len(value) - _LIST_CAP} more")
        return items
    return value


def _attributes(e: Any) -> dict[str, Any]:
    info = e.get_info()
    out: dict[str, Any] = {}
    for key, value in info.items():
        if key in ("id", "type") or key in _SKIP_ATTRS:
            continue
        out[key] = _sanitize(value)
    if getattr(e, "Representation", None) is not None:
        out["representation"] = "present"
    return out


def _material_info(material: Any) -> Any:
    if material is None:
        return None
    try:
        cls = material.is_a()
        if cls == "IfcMaterial":
            return {"kind": "material", "name": material.Name}
        if cls == "IfcMaterialLayerSetUsage":
            return _material_info(material.ForLayerSet)
        if cls == "IfcMaterialLayerSet":
            return {
                "kind": "layer_set",
                "name": getattr(material, "LayerSetName", None),
                "layers": [
                    {
                        "name": getattr(getattr(layer, "Material", None), "Name", None),
                        "thickness": getattr(layer, "LayerThickness", None),
                    }
                    for layer in material.MaterialLayers or ()
                ],
            }
        if cls == "IfcMaterialProfileSetUsage":
            return _material_info(material.ForProfileSet)
        if cls == "IfcMaterialProfileSet":
            return {
                "kind": "profile_set",
                "profiles": [
                    getattr(getattr(p, "Material", None), "Name", None)
                    for p in material.MaterialProfiles or ()
                ],
            }
        if cls == "IfcMaterialConstituentSet":
            return {
                "kind": "constituent_set",
                "constituents": [
                    getattr(getattr(c, "Material", None), "Name", None)
                    for c in material.MaterialConstituents or ()
                ],
            }
        if cls == "IfcMaterialList":
            return {
                "kind": "list",
                "materials": [getattr(m, "Name", None) for m in material.Materials or ()],
            }
        return {"kind": cls}
    except Exception:
        return {"kind": "unknown"}


def _container_chain(e: Any) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    try:
        node = element_util.get_container(e)
        seen: set[int] = set()
        while node is not None and node.id() not in seen:
            seen.add(node.id())
            chain.append(
                {
                    "class": node.is_a(),
                    "name": getattr(node, "Name", None),
                    "global_id": getattr(node, "GlobalId", None),
                }
            )
            node = element_util.get_aggregate(node)
    except Exception:
        pass
    return chain


def element_detail(e: Any, include: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "global_id": getattr(e, "GlobalId", None),
        "class": e.is_a(),
    }
    if "attributes" in include:
        out["attributes"] = _attributes(e)
    if "psets" in include:
        try:
            out["psets"] = element_util.get_psets(e, psets_only=True)
        except Exception:
            out["psets"] = {}
    if "qtos" in include:
        try:
            out["qtos"] = element_util.get_psets(e, qtos_only=True)
        except Exception:
            out["qtos"] = {}
    if "materials" in include:
        try:
            out["materials"] = _material_info(element_util.get_material(e))
        except Exception:
            out["materials"] = None
    if "type" in include:
        try:
            etype = element_util.get_type(e)
        except Exception:
            etype = None
        out["type"] = (
            {
                "class": etype.is_a(),
                "name": getattr(etype, "Name", None),
                "global_id": getattr(etype, "GlobalId", None),
            }
            if etype is not None and etype != e
            else None
        )
    if "container" in include:
        out["container"] = _container_chain(e)
    if "openings" in include:
        openings = []
        for rel in getattr(e, "HasOpenings", ()) or ():
            op = rel.RelatedOpeningElement
            openings.append(
                {"class": op.is_a(), "global_id": getattr(op, "GlobalId", None)}
            )
        out["openings"] = openings
    if "decomposition" in include:
        try:
            parts = element_util.get_decomposition(e)
        except Exception:
            parts = []
        out["decomposition"] = [
            {
                "class": p.is_a(),
                "name": getattr(p, "Name", None),
                "global_id": getattr(p, "GlobalId", None),
            }
            for p in list(parts)[:50]
        ]
    return out
