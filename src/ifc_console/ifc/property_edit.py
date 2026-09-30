"""Editing single-value properties on occurrences, shared by every structured editor.

Callers run these on the model worker, inside whatever transaction they own.
"""

from __future__ import annotations

from typing import Any

from ifc_console.core.changes import IfcScalar
from ifc_console.core.results import ToolError

INVALID = "CHANGESET_INVALID"


def scalar(value: Any) -> IfcScalar:
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ToolError(
        INVALID,
        f"property value type {type(value).__name__} is not supported yet.",
        "The first structured editor supports string, number, boolean, and null values.",
    )


def same(left: IfcScalar, right: IfcScalar) -> bool:
    return type(left) is type(right) and left == right


def find_element(ifc: Any, global_id: str) -> Any:
    try:
        element = ifc.by_guid(global_id)
    except Exception:
        element = None
    if element is None:
        raise ToolError(
            "PROPERTY_NOT_FOUND",
            f"no element has GlobalId {global_id!r}.",
            "Query the model and use a current occurrence GlobalId.",
        )
    return element


def find_pset(element: Any, pset_name: str) -> Any | None:
    psets = []
    for relation in getattr(element, "IsDefinedBy", ()) or ():
        if not relation.is_a("IfcRelDefinesByProperties"):
            continue
        definition = relation.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == pset_name:
            psets.append(definition)
    if not psets:
        return None
    if len(psets) != 1:
        raise ToolError(
            INVALID,
            f"{element.GlobalId} has more than one occurrence property set named {pset_name!r}.",
            "Resolve the duplicate property sets before using structured editing.",
        )
    return psets[0]


def find_property(pset: Any, property_name: str) -> Any | None:
    properties = [item for item in pset.HasProperties if item.Name == property_name]
    if not properties:
        return None
    if len(properties) != 1 or not properties[0].is_a("IfcPropertySingleValue"):
        raise ToolError(
            INVALID,
            f"{pset.Name}.{property_name} is not one unambiguous IfcPropertySingleValue.",
            "Resolve the duplicate or non-single-value property before structured editing.",
        )
    return properties[0]


def read_nominal(prop: Any) -> tuple[str, IfcScalar]:
    nominal = prop.NominalValue
    if nominal is None:
        raise ToolError(
            INVALID,
            f"{prop.Name} has no nominal IFC type to preserve.",
            "Null properties need template-aware type inference, which is not enabled yet.",
        )
    return str(nominal.is_a()), scalar(nominal.wrappedValue)


def assign(ifc: Any, prop: Any, nominal_type: str, value: IfcScalar) -> IfcScalar:
    try:
        prop.NominalValue = None if value is None else ifc.create_entity(nominal_type, value)
    except Exception as exc:
        raise ToolError(
            INVALID,
            f"{value!r} is not valid for {nominal_type}: {exc}",
            "Use a value compatible with the property's existing IFC type.",
        ) from exc
    if prop.NominalValue is None:
        return None
    return scalar(prop.NominalValue.wrappedValue)


def infer_nominal_type(value: IfcScalar) -> str:
    if type(value) is str:
        return "IfcLabel"
    if type(value) is bool:
        return "IfcBoolean"
    if type(value) is int:
        return "IfcInteger"
    if type(value) is float:
        return "IfcReal"
    raise ToolError(
        INVALID,
        "a null property cannot be created without a persisted IFC nominal value.",
        "Pass a non-null scalar value and, for domain measures, an explicit nominal_type.",
    )


def new_nominal(ifc: Any, nominal_type: str, value: IfcScalar) -> Any:
    if value is None:
        infer_nominal_type(value)
    try:
        nominal = ifc.create_entity(nominal_type, value)
    except Exception as exc:
        raise ToolError(
            INVALID,
            f"{value!r} is not valid for {nominal_type}: {exc}",
            "Use an IFC value type such as IfcLabel or IfcLengthMeasure and a compatible value.",
        ) from exc
    if nominal.id() != 0:
        ifc.remove(nominal)
        raise ToolError(
            INVALID,
            f"{nominal_type} is an IFC entity, not a nominal value type.",
            "Use an IFC value type such as IfcLabel, IfcBoolean, IfcReal, or an IFC measure.",
        )
    return nominal


def create_property(
    ifc: Any,
    element: Any,
    pset: Any | None,
    pset_name: str,
    property_name: str,
    nominal_type: str,
    value: IfcScalar,
) -> tuple[Any, Any, IfcScalar]:
    nominal = new_nominal(ifc, nominal_type, value)
    if pset is None:
        import ifcopenshell.api.pset

        pset = ifcopenshell.api.pset.add_pset(ifc, product=element, name=pset_name)
    prop = ifc.create_entity(
        "IfcPropertySingleValue",
        Name=property_name,
        Description=None,
        NominalValue=nominal,
        Unit=None,
    )
    pset.HasProperties = tuple([*(pset.HasProperties or ()), prop])
    return pset, prop, scalar(prop.NominalValue.wrappedValue)
