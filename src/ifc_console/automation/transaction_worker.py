"""Isolated worker for property previews, IFC writes, and verification."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from ifc_console.automation.files import source_matches
from ifc_console.core.changes import ChangeSet, IfcScalar, PropertyValueChange
from ifc_console.core.jobs import SourceFileRef
from ifc_console.core.results import ToolError
from ifc_console.sandbox.policy import SandboxPolicy


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _verify_source(source: SourceFileRef) -> None:
    if not source_matches(source):
        raise ToolError(
            "SOURCE_CHANGED",
            f"{Path(source.path).name} changed during the transaction.",
            "Create a new preview against the current model revision.",
        )


def _scalar(value: Any) -> IfcScalar:
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ToolError(
        "CHANGESET_INVALID",
        f"property value type {type(value).__name__} is not supported yet.",
        "The first structured editor supports string, number, boolean, and null values.",
    )


def _same(left: IfcScalar, right: IfcScalar) -> bool:
    return type(left) is type(right) and left == right


def _find_property(ifc: Any, global_id: str, pset_name: str, property_name: str) -> tuple:
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
    psets = []
    for relation in getattr(element, "IsDefinedBy", ()) or ():
        if not relation.is_a("IfcRelDefinesByProperties"):
            continue
        definition = relation.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == pset_name:
            psets.append(definition)
    if not psets:
        raise ToolError(
            "PROPERTY_NOT_FOUND",
            f"{global_id} has no occurrence property set {pset_name!r}.",
            "This editor does not create property sets or edit inherited type properties yet.",
        )
    if len(psets) != 1:
        raise ToolError(
            "CHANGESET_INVALID",
            f"{global_id} has more than one occurrence property set named {pset_name!r}.",
            "Resolve the duplicate property sets before using structured editing.",
        )
    pset = psets[0]
    properties = [item for item in pset.HasProperties if item.Name == property_name]
    if not properties:
        raise ToolError(
            "PROPERTY_NOT_FOUND",
            f"{pset_name}.{property_name} is missing on {global_id}.",
            "The first structured editor updates existing single-value properties only.",
        )
    if len(properties) != 1 or not properties[0].is_a("IfcPropertySingleValue"):
        raise ToolError(
            "CHANGESET_INVALID",
            f"{pset_name}.{property_name} is not one unambiguous IfcPropertySingleValue.",
            "Use an existing single-value occurrence property for this release slice.",
        )
    return element, pset, properties[0]


def _read_nominal(prop: Any) -> tuple[str, IfcScalar]:
    nominal = prop.NominalValue
    if nominal is None:
        raise ToolError(
            "CHANGESET_INVALID",
            f"{prop.Name} has no nominal IFC type to preserve.",
            "Null properties need template-aware type inference, which is not enabled yet.",
        )
    return str(nominal.is_a()), _scalar(nominal.wrappedValue)


def _assign(ifc: Any, prop: Any, nominal_type: str, value: IfcScalar) -> IfcScalar:
    try:
        prop.NominalValue = None if value is None else ifc.create_entity(nominal_type, value)
    except Exception as exc:
        raise ToolError(
            "CHANGESET_INVALID",
            f"{value!r} is not valid for {nominal_type}: {exc}",
            "Use a value compatible with the property's existing IFC type.",
        ) from exc
    if prop.NominalValue is None:
        return None
    return _scalar(prop.NominalValue.wrappedValue)


def _preview(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    source = SourceFileRef.model_validate(payload["source"])
    _verify_source(source)
    ifc = ifcopenshell.open(source.path)
    after = _scalar(payload.get("value"))
    global_ids = tuple(dict.fromkeys(str(item) for item in payload["global_ids"]))
    if not global_ids:
        raise ToolError(
            "INVALID_INPUT", "no GlobalIds were supplied.", "Select at least one element."
        )
    changes: list[PropertyValueChange] = []
    for global_id in global_ids:
        element, pset, prop = _find_property(
            ifc, global_id, str(payload["pset_name"]), str(payload["property_name"])
        )
        nominal_type, before = _read_nominal(prop)
        applied = _assign(ifc, prop, nominal_type, after)
        if _same(before, applied):
            raise ToolError(
                "CHANGESET_INVALID",
                f"{pset.Name}.{prop.Name} on {global_id} already has that value.",
                "Choose a different value or remove this element from the preview.",
            )
        changes.append(
            PropertyValueChange(
                global_id=global_id,
                entity_type=element.is_a(),
                entity_name=getattr(element, "Name", None),
                pset_name=pset.Name,
                property_name=prop.Name,
                pset_id=pset.id(),
                property_id=prop.id(),
                nominal_type=nominal_type,
                before=before,
                after=applied,
            )
        )
    _verify_source(source)
    return {"changes": [item.model_dump(mode="json") for item in changes]}


def _validate_file(path: Path, ifcopenshell: Any) -> dict[str, Any]:
    from ifc_console.ifc.validation import run_schema_validation

    reopened = ifcopenshell.open(str(path))
    report = run_schema_validation(reopened, express_rules=False, max_issues=20)
    return {
        "schema": getattr(reopened, "schema", None),
        "schema_valid": bool(report["valid"]),
        "schema_issue_count": int(report["issue_count"]),
    }


def _apply(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    change_set = ChangeSet.model_validate(payload["change_set"])
    _verify_source(change_set.source)
    ifc = ifcopenshell.open(change_set.source.path)
    for change in change_set.changes:
        _element, pset, prop = _find_property(
            ifc, change.global_id, change.pset_name, change.property_name
        )
        nominal_type, current = _read_nominal(prop)
        identity_matches = pset.id() == change.pset_id and prop.id() == change.property_id
        if (
            not identity_matches
            or nominal_type != change.nominal_type
            or not _same(current, change.before)
        ):
            raise ToolError(
                "REVISION_CONFLICT",
                f"{change.pset_name}.{change.property_name} changed after preview.",
                "Discard this ChangeSet and preview the edit again.",
            )
        applied = _assign(ifc, prop, nominal_type, change.after)
        if not _same(applied, change.after):
            raise ToolError(
                "COMMIT_FAILED",
                f"IFC value coercion changed {change.after!r} to {applied!r}.",
                "Use a value matching the property's existing IFC type.",
            )
    _verify_source(change_set.source)
    output = Path(payload["output_path"])
    ifc.write(str(output))
    with output.open("r+b") as handle:
        os.fsync(handle.fileno())
    verified = _validate_file(output, ifcopenshell)
    reopened = ifcopenshell.open(str(output))
    for change in change_set.changes:
        _element, pset, prop = _find_property(
            reopened, change.global_id, change.pset_name, change.property_name
        )
        nominal_type, current = _read_nominal(prop)
        if (
            pset.id() != change.pset_id
            or prop.id() != change.property_id
            or nominal_type != change.nominal_type
            or not _same(current, change.after)
        ):
            raise ToolError(
                "COMMIT_FAILED",
                f"the reopened IFC does not contain the previewed value for {change.global_id}.",
                "The candidate was discarded and the source model was not replaced.",
            )
    return {"output_path": str(output), **verified}


def _verify(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    source = SourceFileRef.model_validate(payload["source"])
    _verify_source(source)
    return _validate_file(Path(source.path), ifcopenshell)


def run(payload: dict[str, Any]) -> dict[str, Any]:
    import ifcopenshell

    from ifc_console.sandbox import hooks, limits

    policy = SandboxPolicy.from_dict(payload.get("policy") or {})
    applied = limits.apply_self_limits(policy.memory_mb)
    controls = [*applied, *hooks.install(policy)]
    action = payload.get("action")
    if action == "preview":
        result = _preview(payload, ifcopenshell)
    elif action == "apply":
        result = _apply(payload, ifcopenshell)
    elif action == "verify":
        result = _verify(payload, ifcopenshell)
    else:
        raise ToolError("INVALID_INPUT", f"unknown transaction action {action!r}.", "Use the SDK.")
    return {"ok": True, "controls": controls, **result}


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1:
        _emit({"ok": False, "code": "INVALID_INPUT", "message": "expected one input"})
        return 2
    try:
        payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        _emit(run(payload))
        return 0
    except ToolError as exc:
        _emit({"ok": False, "code": exc.code, "message": exc.message, "hint": exc.hint})
        return 1
    except Exception as exc:
        _emit(
            {
                "ok": False,
                "code": "COMMIT_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
                "hint": "The source model was not replaced.",
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
