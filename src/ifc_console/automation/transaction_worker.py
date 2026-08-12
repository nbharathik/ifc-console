"""Isolated worker for property previews, IFC writes, and verification."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from ifc_console.automation.files import source_matches
from ifc_console.core.changes import (
    ChangeSet,
    ClassificationAssignmentChange,
    IfcScalar,
    PropertyCreateChange,
    PropertyValueChange,
)
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


def _find_element(ifc: Any, global_id: str) -> Any:
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


def _find_pset(element: Any, pset_name: str) -> Any | None:
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
            "CHANGESET_INVALID",
            f"{element.GlobalId} has more than one occurrence property set named {pset_name!r}.",
            "Resolve the duplicate property sets before using structured editing.",
        )
    return psets[0]


def _find_property(pset: Any, property_name: str) -> Any | None:
    properties = [item for item in pset.HasProperties if item.Name == property_name]
    if not properties:
        return None
    if len(properties) != 1 or not properties[0].is_a("IfcPropertySingleValue"):
        raise ToolError(
            "CHANGESET_INVALID",
            f"{pset.Name}.{property_name} is not one unambiguous IfcPropertySingleValue.",
            "Resolve the duplicate or non-single-value property before structured editing.",
        )
    return properties[0]


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


def _infer_nominal_type(value: IfcScalar) -> str:
    if type(value) is str:
        return "IfcLabel"
    if type(value) is bool:
        return "IfcBoolean"
    if type(value) is int:
        return "IfcInteger"
    if type(value) is float:
        return "IfcReal"
    raise ToolError(
        "CHANGESET_INVALID",
        "a null property cannot be created without a persisted IFC nominal value.",
        "Pass a non-null scalar value and, for domain measures, an explicit nominal_type.",
    )


def _new_nominal(ifc: Any, nominal_type: str, value: IfcScalar) -> Any:
    if value is None:
        _infer_nominal_type(value)
    try:
        nominal = ifc.create_entity(nominal_type, value)
    except Exception as exc:
        raise ToolError(
            "CHANGESET_INVALID",
            f"{value!r} is not valid for {nominal_type}: {exc}",
            "Use an IFC value type such as IfcLabel or IfcLengthMeasure and a compatible value.",
        ) from exc
    if nominal.id() != 0:
        ifc.remove(nominal)
        raise ToolError(
            "CHANGESET_INVALID",
            f"{nominal_type} is an IFC entity, not a nominal value type.",
            "Use an IFC value type such as IfcLabel, IfcBoolean, IfcReal, or an IFC measure.",
        )
    return nominal


def _create_property(
    ifc: Any,
    element: Any,
    pset: Any | None,
    pset_name: str,
    property_name: str,
    nominal_type: str,
    value: IfcScalar,
) -> tuple[Any, Any, IfcScalar]:
    nominal = _new_nominal(ifc, nominal_type, value)
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
    return pset, prop, _scalar(prop.NominalValue.wrappedValue)


def _find_classification(ifc: Any, name: str) -> Any | None:
    matches = [item for item in ifc.by_type("IfcClassification") if item.Name == name]
    if len(matches) > 1:
        raise ToolError(
            "CHANGESET_INVALID",
            f"the model has more than one classification system named {name!r}.",
            "Resolve the duplicate classification systems before structured editing.",
        )
    return matches[0] if matches else None


def _reference_identification(reference: Any) -> str | None:
    return getattr(reference, "ItemReference", None) or getattr(
        reference, "Identification", None
    )


def _find_classification_reference(
    ifc: Any, classification: Any | None, identification: str
) -> Any | None:
    if classification is None:
        return None
    matches = [
        item
        for item in ifc.by_type("IfcClassificationReference")
        if item.ReferencedSource == classification
        and _reference_identification(item) == identification
    ]
    if len(matches) > 1:
        raise ToolError(
            "CHANGESET_INVALID",
            f"classification {classification.Name!r} has duplicate reference {identification!r}.",
            "Resolve the duplicate classification references before structured editing.",
        )
    return matches[0] if matches else None


def _is_directly_classified(element: Any, reference: Any) -> bool:
    return any(
        relation.is_a("IfcRelAssociatesClassification")
        and relation.RelatingClassification == reference
        for relation in (getattr(element, "HasAssociations", ()) or ())
    )


def _create_classification_relation(
    ifc: Any, ifcopenshell: Any, objects: list[Any], relating: Any
) -> Any:
    owner_history = None
    if ifc.schema == "IFC2X3":
        histories = ifc.by_type("IfcOwnerHistory")
        if not histories:
            raise ToolError(
                "CHANGESET_INVALID",
                "IFC2X3 classification editing requires an existing IfcOwnerHistory.",
                "Add valid IFC2X3 ownership metadata before assigning classifications.",
            )
        owner_history = histories[0]
    return ifc.create_entity(
        "IfcRelAssociatesClassification",
        GlobalId=ifcopenshell.guid.new(),
        OwnerHistory=owner_history,
        Name=None,
        Description=None,
        RelatedObjects=objects,
        RelatingClassification=relating,
    )


def _create_classification_assignment(
    ifc: Any,
    ifcopenshell: Any,
    element: Any,
    classification: Any | None,
    reference: Any | None,
    classification_name: str,
    identification: str,
    reference_name: str,
) -> tuple[Any, Any]:
    if classification is None:
        attributes = {"Name": classification_name}
        if ifc.schema == "IFC2X3":
            attributes.update({"Source": classification_name, "Edition": "unspecified"})
        classification = ifc.create_entity("IfcClassification", **attributes)
        if ifc.schema != "IFC2X3":
            projects = ifc.by_type("IfcProject")
            if not projects:
                raise ToolError(
                    "CHANGESET_INVALID",
                    "the model has no IfcProject to associate with a classification system.",
                    "Repair the IFC project structure before assigning classifications.",
                )
            _create_classification_relation(ifc, ifcopenshell, [projects[0]], classification)
    if reference is None:
        attributes = {
            "Name": reference_name,
            "ReferencedSource": classification,
        }
        if ifc.schema == "IFC2X3":
            attributes["ItemReference"] = identification
        else:
            attributes["Identification"] = identification
        reference = ifc.create_entity("IfcClassificationReference", **attributes)
    relations = [
        relation
        for relation in ifc.by_type("IfcRelAssociatesClassification")
        if relation.RelatingClassification == reference
    ]
    if relations:
        relation = relations[0]
        relation.RelatedObjects = tuple(dict.fromkeys([*relation.RelatedObjects, element]))
    else:
        _create_classification_relation(ifc, ifcopenshell, [element], reference)
    return classification, reference


def _preview_classification(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    source = SourceFileRef.model_validate(payload["source"])
    _verify_source(source)
    ifc = ifcopenshell.open(source.path)
    classification_name = str(payload["classification_name"])
    identification = str(payload["identification"])
    reference_name = str(payload["reference_name"])
    global_ids = tuple(dict.fromkeys(str(item) for item in payload["global_ids"]))
    if not global_ids:
        raise ToolError(
            "INVALID_INPUT", "no GlobalIds were supplied.", "Select at least one element."
        )
    classification = _find_classification(ifc, classification_name)
    reference = _find_classification_reference(ifc, classification, identification)
    if reference is not None and reference.Name != reference_name:
        raise ToolError(
            "CHANGESET_INVALID",
            f"{classification_name}.{identification} is named {reference.Name!r}, "
            f"not {reference_name!r}.",
            "Use the existing reference name or choose another identification.",
        )
    classification_id = classification.id() if classification is not None else None
    reference_id = reference.id() if reference is not None else None
    changes: list[ClassificationAssignmentChange] = []
    for global_id in global_ids:
        element = _find_element(ifc, global_id)
        if reference is not None and _is_directly_classified(element, reference):
            raise ToolError(
                "CHANGESET_INVALID",
                f"{global_id} already has {classification_name}.{identification}.",
                "Remove this element from the preview or choose another reference.",
            )
        classification, reference = _create_classification_assignment(
            ifc,
            ifcopenshell,
            element,
            classification,
            reference,
            classification_name,
            identification,
            reference_name,
        )
        changes.append(
            ClassificationAssignmentChange(
                global_id=global_id,
                entity_type=element.is_a(),
                entity_name=getattr(element, "Name", None),
                classification_name=classification_name,
                identification=identification,
                reference_name=reference_name,
                classification_id=classification_id,
                reference_id=reference_id,
                after=identification,
            )
        )
    _verify_source(source)
    return {"changes": [item.model_dump(mode="json") for item in changes]}


def _preview(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    source = SourceFileRef.model_validate(payload["source"])
    _verify_source(source)
    ifc = ifcopenshell.open(source.path)
    after = _scalar(payload.get("value"))
    create_missing = bool(payload.get("create_missing", False))
    requested_nominal_type = payload.get("nominal_type")
    global_ids = tuple(dict.fromkeys(str(item) for item in payload["global_ids"]))
    if not global_ids:
        raise ToolError(
            "INVALID_INPUT", "no GlobalIds were supplied.", "Select at least one element."
        )
    changes: list[PropertyValueChange | PropertyCreateChange] = []
    for global_id in global_ids:
        pset_name = str(payload["pset_name"])
        property_name = str(payload["property_name"])
        element = _find_element(ifc, global_id)
        pset = _find_pset(element, pset_name)
        prop = _find_property(pset, property_name) if pset is not None else None
        if prop is None:
            if not create_missing:
                location = (
                    f"no occurrence property set {pset_name!r}"
                    if pset is None
                    else f"no property {pset_name}.{property_name}"
                )
                raise ToolError(
                    "PROPERTY_NOT_FOUND",
                    f"{global_id} has {location}.",
                    "Pass create_missing=true to preview occurrence-level creation.",
                )
            nominal_type = str(requested_nominal_type or _infer_nominal_type(after))
            existing_pset_id = pset.id() if pset is not None else None
            _created_pset, _created_prop, applied = _create_property(
                ifc,
                element,
                pset,
                pset_name,
                property_name,
                nominal_type,
                after,
            )
            changes.append(
                PropertyCreateChange(
                    global_id=global_id,
                    entity_type=element.is_a(),
                    entity_name=getattr(element, "Name", None),
                    pset_name=pset_name,
                    property_name=property_name,
                    pset_id=existing_pset_id,
                    nominal_type=nominal_type,
                    after=applied,
                )
            )
            continue
        nominal_type, before = _read_nominal(prop)
        if requested_nominal_type is not None and requested_nominal_type != nominal_type:
            raise ToolError(
                "CHANGESET_INVALID",
                f"{pset.Name}.{prop.Name} uses {nominal_type}, not {requested_nominal_type}.",
                "Omit nominal_type for existing properties or pass its exact current IFC type.",
            )
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
    return _validate_opened(reopened, run_schema_validation)


def _validate_opened(ifc: Any, validator: Any | None = None) -> dict[str, Any]:
    if validator is None:
        from ifc_console.ifc.validation import run_schema_validation

        validator = run_schema_validation
    report = validator(ifc, express_rules=False, max_issues=2000)
    fingerprints = []
    for issue in report["issues"]:
        stable = {
            key: issue.get(key)
            for key in ("severity", "message", "attribute", "class", "global_id")
        }
        fingerprints.append(
            hashlib.sha256(
                json.dumps(stable, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()
        )
    return {
        "schema": getattr(ifc, "schema", None),
        "schema_valid": bool(report["valid"]),
        "schema_issue_count": int(report["issue_count"]),
        "schema_issue_fingerprints": fingerprints,
    }


def _apply(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    change_set = ChangeSet.model_validate(payload["change_set"])
    _verify_source(change_set.source)
    ifc = ifcopenshell.open(change_set.source.path)
    created_classifications: set[str] = set()
    created_references: set[tuple[str, str]] = set()
    for change in change_set.changes:
        element = _find_element(ifc, change.global_id)
        if isinstance(change, ClassificationAssignmentChange):
            classification = _find_classification(ifc, change.classification_name)
            reference = _find_classification_reference(
                ifc, classification, change.identification
            )
            classification_conflict = (
                classification is not None
                and change.classification_name not in created_classifications
                if change.classification_id is None
                else classification is None or classification.id() != change.classification_id
            )
            reference_key = (change.classification_name, change.identification)
            reference_conflict = (
                reference is not None and reference_key not in created_references
                if change.reference_id is None
                else reference is None or reference.id() != change.reference_id
            )
            if (
                classification_conflict
                or reference_conflict
                or (reference is not None and reference.Name != change.reference_name)
                or (reference is not None and _is_directly_classified(element, reference))
            ):
                raise ToolError(
                    "REVISION_CONFLICT",
                    f"classification {change.classification_name}.{change.identification} "
                    "changed after preview.",
                    "Discard this ChangeSet and preview the assignment again.",
                )
            classification_was_missing = classification is None
            reference_was_missing = reference is None
            _create_classification_assignment(
                ifc,
                ifcopenshell,
                element,
                classification,
                reference,
                change.classification_name,
                change.identification,
                change.reference_name,
            )
            if classification_was_missing:
                created_classifications.add(change.classification_name)
            if reference_was_missing:
                created_references.add(reference_key)
            continue
        pset = _find_pset(element, change.pset_name)
        if isinstance(change, PropertyCreateChange):
            if change.pset_id is None:
                conflict = pset is not None
            else:
                conflict = pset is None or pset.id() != change.pset_id
            prop = _find_property(pset, change.property_name) if pset is not None else None
            if conflict or prop is not None:
                raise ToolError(
                    "REVISION_CONFLICT",
                    f"{change.pset_name}.{change.property_name} changed after preview.",
                    "Discard this ChangeSet and preview the creation again.",
                )
            _create_property(
                ifc,
                element,
                pset,
                change.pset_name,
                change.property_name,
                change.nominal_type,
                change.after,
            )
            continue
        if pset is None:
            raise ToolError(
                "REVISION_CONFLICT",
                f"{change.pset_name}.{change.property_name} changed after preview.",
                "Discard this ChangeSet and preview the edit again.",
            )
        prop = _find_property(pset, change.property_name)
        if prop is None:
            raise ToolError(
                "REVISION_CONFLICT",
                f"{change.pset_name}.{change.property_name} changed after preview.",
                "Discard this ChangeSet and preview the edit again.",
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
    candidate_fingerprints = verified.pop("schema_issue_fingerprints")
    source_issue_count = 0
    regression_count = 0
    if not verified["schema_valid"]:
        source_validation = _validate_file(Path(change_set.source.path), ifcopenshell)
        source_fingerprints = source_validation.pop("schema_issue_fingerprints")
        source_issue_count = source_validation["schema_issue_count"]
        regressions = Counter(candidate_fingerprints) - Counter(source_fingerprints)
        regression_count = max(
            sum(regressions.values()),
            verified["schema_issue_count"] - source_issue_count,
        )
    reopened = ifcopenshell.open(str(output))
    for change in change_set.changes:
        element = _find_element(reopened, change.global_id)
        if isinstance(change, ClassificationAssignmentChange):
            classification = _find_classification(reopened, change.classification_name)
            reference = _find_classification_reference(
                reopened, classification, change.identification
            )
            if (
                classification is None
                or reference is None
                or reference.Name != change.reference_name
                or not _is_directly_classified(element, reference)
                or (
                    change.classification_id is not None
                    and classification.id() != change.classification_id
                )
                or (change.reference_id is not None and reference.id() != change.reference_id)
            ):
                raise ToolError(
                    "COMMIT_FAILED",
                    f"the reopened IFC does not contain the previewed classification "
                    f"for {change.global_id}.",
                    "The candidate was discarded and the source model was not replaced.",
                )
            continue
        pset = _find_pset(element, change.pset_name)
        prop = _find_property(pset, change.property_name) if pset is not None else None
        if pset is None or prop is None:
            raise ToolError(
                "COMMIT_FAILED",
                f"the reopened IFC is missing the previewed property for {change.global_id}.",
                "The candidate was discarded and the source model was not replaced.",
            )
        nominal_type, current = _read_nominal(prop)
        identity_mismatch = (
            pset.id() != change.pset_id or prop.id() != change.property_id
            if isinstance(change, PropertyValueChange)
            else change.pset_id is not None and pset.id() != change.pset_id
        )
        if (
            identity_mismatch
            or nominal_type != change.nominal_type
            or not _same(current, change.after)
        ):
            raise ToolError(
                "COMMIT_FAILED",
                f"the reopened IFC does not contain the previewed value for {change.global_id}.",
                "The candidate was discarded and the source model was not replaced.",
            )
    return {
        "output_path": str(output),
        **verified,
        "source_schema_issue_count": source_issue_count,
        "schema_regression_count": regression_count,
    }


def _verify(payload: dict[str, Any], ifcopenshell: Any) -> dict[str, Any]:
    source = SourceFileRef.model_validate(payload["source"])
    _verify_source(source)
    result = _validate_file(Path(source.path), ifcopenshell)
    result.pop("schema_issue_fingerprints")
    return result


def run(payload: dict[str, Any]) -> dict[str, Any]:
    import ifcopenshell
    import ifcopenshell.api.pset

    from ifc_console.sandbox import hooks, limits

    policy = SandboxPolicy.from_dict(payload.get("policy") or {})
    applied = limits.apply_self_limits(policy.memory_mb)
    controls = [*applied, *hooks.install(policy)]
    action = payload.get("action")
    if action == "preview":
        result = _preview(payload, ifcopenshell)
    elif action == "preview_classification":
        result = _preview_classification(payload, ifcopenshell)
    elif action == "apply":
        result = _apply(payload, ifcopenshell)
    elif action == "verify":
        result = _verify(payload, ifcopenshell)
    else:
        raise ToolError("INVALID_INPUT", f"unknown transaction action {action!r}.", "Use the SDK.")
    context = payload.get("context") or {}
    return {
        "ok": True,
        "controls": controls,
        "correlation_id": context.get("correlation_id"),
        **result,
    }


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
