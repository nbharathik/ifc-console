"""Agent skill tools: list, load, and record reusable measurement procedures.

Skills are project-local markdown files (see agents/skills.py). Reading them
is free; saving one is a workspace file write, so it carries FILE_WRITE and
therefore needs approval in surfaces that gate writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from ifc_console.application.operations import enveloped
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.ifc.similarity import build_geometry_signature, compare_geometry_signatures
from pydantic import Field

from ifc_console_agents.skills import AgentSkillStore, MeasurementRule, MeasurementSkillSpec

if TYPE_CHECKING:
    from ifc_console.app import AppCore

SKILL_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
SKILL_WRITE_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

TOOL_NAMES = (
    "list_agent_skills",
    "get_agent_skill",
    "preview_measurement_skill_migration",
    "save_agent_skill",
    "apply_measurement_skill",
)

_CONFIDENCE = {"unknown": 0, "low": 1, "medium": 2, "high": 3, "exact": 4}
_SUBSTANTIVE_SIGNALS = {
    "type_key",
    "geometry_family",
    "profile_family",
    "material_key",
    "components",
    "closed_shells",
    "through_holes",
    "section_variation",
    "normalized_extents",
    "normalized_section_bounds",
}
_SUPPORTED_RULE_TYPES = {"object_measurement", "element_size", "area"}
_INTRINSIC_PREFIXES = ("profile.", "material.", "topology.")
_SUPPORTED_FALLBACKS = {
    "directional_extent": {
        "sources": {"analysis_mesh"},
        "methods": {"directional_extent", "vertex_support_projection"},
    },
    "adaptive_section": {
        "sources": {"mesh_section", "analysis_mesh"},
        "methods": {"adaptive_section"},
    },
    "adaptive_section.thickness_modes": {
        "sources": {"mesh_section", "analysis_mesh"},
        "methods": {"adaptive_section", "adaptive_section.thickness_modes"},
    },
    "material_layer_analysis": {
        "sources": {"material_layer_parameter"},
        "methods": {"ifc_material_layer_set", "material_layer_analysis"},
    },
}


def _object_info(record: dict[str, Any]) -> dict[str, Any]:
    nested = record.get("object")
    return nested if isinstance(nested, dict) else record


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    source = _object_info(record)
    return {
        "global_id": source.get("global_id") or record.get("global_id"),
        "class": source.get("class") or record.get("class"),
        "name": source.get("name") or record.get("name"),
        "type": source.get("type") or record.get("type"),
    }


def _signature(record: dict[str, Any]) -> dict[str, Any]:
    supplied = record.get("geometry_signature")
    if isinstance(supplied, dict) and supplied:
        return supplied
    try:
        return build_geometry_signature(record)
    except (TypeError, ValueError):
        return {}


def _hard_requirements(
    spec: MeasurementSkillSpec, signature: dict[str, Any], record: dict[str, Any]
) -> tuple[list[str], list[str]]:
    reasons: list[str] = []
    mismatches: list[str] = []
    geometry_family = record.get("geometry_family") or signature.get("geometry_family")
    for requirement in spec.applicability.hard_requirements:
        if requirement == "constant_or_piecewise_profile":
            variation = signature.get("section_variation")
            passed = bool(
                isinstance(geometry_family, str)
                and any(token in geometry_family for token in ("constant", "piecewise", "profile"))
            ) or variation in {"constant", "piecewise"}
        else:
            value = signature.get(requirement)
            passed = value is True or (isinstance(value, (int, float)) and value > 0)
        if passed:
            reasons.append(f"hard requirement {requirement} passed")
        else:
            mismatches.append(f"hard requirement {requirement} is not demonstrated")
    return reasons, mismatches


def _applicability(
    spec: MeasurementSkillSpec,
    record: dict[str, Any],
    *,
    current_revision: dict[str, Any] | None,
    include_evidence: bool,
) -> dict[str, Any]:
    identity = _identity(record)
    signature = _signature(record)
    reasons: list[str] = []
    mismatches: list[str] = []
    hard_passed = True

    if spec.applicability.ifc_classes:
        ifc_class = identity.get("class")
        allowed_families = {
            item.geometry_signature.get("class_family")
            for item in (spec.exemplar.objects if spec.exemplar else ())
            if item.ifc_class in spec.applicability.ifc_classes
        }
        same_family = bool(
            signature.get("class_family") and signature.get("class_family") in allowed_families
        )
        if ifc_class in spec.applicability.ifc_classes or same_family:
            reasons.append(f"compatible IFC class {ifc_class}")
        else:
            hard_passed = False
            mismatches.append(
                f"IFC class {ifc_class or 'unknown'} is outside "
                f"{', '.join(spec.applicability.ifc_classes)}"
            )
    if spec.applicability.profile_families:
        profile = signature.get("profile_family")
        if profile in spec.applicability.profile_families:
            reasons.append(f"profile family {profile}")
        else:
            hard_passed = False
            mismatches.append(f"profile family {profile or 'unknown'} is incompatible")
    if spec.applicability.geometry_families:
        family = record.get("geometry_family") or signature.get("geometry_family")
        if family in spec.applicability.geometry_families:
            reasons.append(f"geometry family {family}")
        else:
            hard_passed = False
            mismatches.append(f"geometry family {family or 'unknown'} is incompatible")

    requirement_reasons, requirement_mismatches = _hard_requirements(spec, signature, record)
    reasons.extend(requirement_reasons)
    mismatches.extend(requirement_mismatches)
    hard_passed = hard_passed and not requirement_mismatches

    target_guid = identity.get("global_id")
    exemplar_objects = spec.exemplar.objects if spec.exemplar else ()
    exact = next((item for item in exemplar_objects if item.global_id == target_guid), None)
    best: dict[str, Any] | None = None
    substantive = False
    geometry_compared = False
    comparison_exemplar_global_id: str | None = None
    comparison_basis = "insufficient_evidence"
    pinned = spec.exemplar.model_revision if spec.exemplar else None
    pinned_revision_match = bool(
        exact is not None
        and pinned is not None
        and pinned.fingerprint
        and pinned.revision is not None
        and isinstance(current_revision, dict)
        and current_revision.get("fingerprint") == pinned.fingerprint
        and current_revision.get("revision") == pinned.revision
    )
    if exact is not None:
        reasons.append("GlobalId matches the recorded exemplar")
    if pinned_revision_match:
        best = {
            "score": 1.0,
            "matched": True,
            "threshold": spec.applicability.similarity_threshold,
            "hard_filters_passed": True,
            "reasons": ["recorded exemplar identity and pinned model revision match"],
            "mismatches": [],
            "signals": [],
        }
        substantive = True
        comparison_exemplar_global_id = exact.global_id
        comparison_basis = "pinned_exemplar_revision"
    else:
        if exact is not None:
            reasons.append(
                "exemplar GlobalId is only an identity signal because the pinned "
                "fingerprint and revision do not both match; geometry was compared"
            )
        for exemplar in exemplar_objects:
            if not exemplar.geometry_signature or not signature:
                continue
            try:
                comparison = compare_geometry_signatures(
                    exemplar.geometry_signature,
                    signature,
                    threshold=spec.applicability.similarity_threshold,
                )
            except ValueError:
                continue
            geometry_compared = True
            if best is None or float(comparison.get("score", 0.0)) > float(best.get("score", 0.0)):
                best = comparison
                comparison_exemplar_global_id = exemplar.global_id
        if best is not None:
            comparison_basis = "geometry_signature"
            substantive = any(
                signal.get("signal") in _SUBSTANTIVE_SIGNALS
                for signal in best.get("signals") or ()
                if isinstance(signal, dict)
            )
    if best is None:
        applicability_signals = bool(
            spec.applicability.profile_families
            or spec.applicability.geometry_families
            or spec.applicability.hard_requirements
        )
        substantive = applicability_signals and not requirement_mismatches
        if substantive:
            comparison_basis = "applicability_constraints"
        best = {
            "score": 1.0 if substantive and hard_passed else 0.0,
            "matched": substantive and hard_passed,
            "threshold": spec.applicability.similarity_threshold,
            "hard_filters_passed": hard_passed,
            "reasons": [],
            "mismatches": [],
            "signals": [],
        }

    reasons.extend(str(value) for value in best.get("reasons") or () if value)
    mismatches.extend(str(value) for value in best.get("mismatches") or () if value)
    if not substantive:
        mismatches.append(
            "same-class membership alone is insufficient; no type, profile, "
            "representation, topology, or normalized-geometry evidence was available"
        )
    score = float(best.get("score", 0.0))
    applicable = bool(
        hard_passed
        and substantive
        and best.get("hard_filters_passed", True)
        and score >= spec.applicability.similarity_threshold
    )
    result: dict[str, Any] = {
        "applicable": applicable,
        "score": round(score, 6),
        "threshold": spec.applicability.similarity_threshold,
        "hard_filters_passed": bool(hard_passed and best.get("hard_filters_passed", True)),
        "reasons": list(dict.fromkeys(reasons)),
        "mismatches": list(dict.fromkeys(mismatches)),
        "candidate_global_id": target_guid,
        "exemplar_identity_match": exact is not None,
        "pinned_revision_match": pinned_revision_match,
        "geometry_compared": geometry_compared,
        "comparison_basis": comparison_basis,
    }
    if comparison_exemplar_global_id is not None:
        result["comparison_exemplar_global_id"] = comparison_exemplar_global_id
    if include_evidence:
        result["signals"] = best.get("signals") or []
        result["candidate_signature"] = signature
    return result


def _coverage_ids(record: dict[str, Any], key: str) -> set[str]:
    coverage = record.get("coverage")
    values = coverage.get(key) if isinstance(coverage, dict) else ()
    out: set[str] = set()
    for value in values or ():
        if isinstance(value, str):
            out.add(value)
        elif isinstance(value, dict) and isinstance(value.get("id"), str):
            out.add(value["id"])
    return out


def _confidence_rank(value: Any) -> int:
    return _CONFIDENCE.get(str(value or "unknown").lower(), 0)


def _expected_quantity(rule: MeasurementRule) -> str:
    assert rule.output is not None
    if rule.rule_type == "area":
        return "area"
    if rule.rule_type == "element_size":
        return "length"
    output = rule.output
    if output == "mass.volume":
        return "volume"
    if output in {"mass.surface_area", "mass.footprint_area", "section.area"}:
        return "area"
    if output.startswith("section.second_moment_"):
        return "second_moment"
    if output.endswith(".count") or output.startswith("topology."):
        return "count"
    if "angle" in output or "slope" in output:
        return "angle"
    return "length"


def _intrinsic_scalar(output: str) -> bool:
    return output.startswith(_INTRINSIC_PREFIXES) or output in {
        "mass.volume",
        "mass.surface_area",
        "opening.count",
    }


def _measurement_candidates(record: dict[str, Any], output: str) -> list[dict[str, Any]]:
    """Flatten preferred records and their bounded independent alternatives."""
    candidates: list[dict[str, Any]] = []
    inherited = (
        "id",
        "label",
        "quantity_kind",
        "si_unit",
        "frame",
        "direction",
        "station",
        "component",
    )
    for measurement in record.get("measurements") or ():
        if not isinstance(measurement, dict) or measurement.get("id") != output:
            continue
        primary = {key: value for key, value in measurement.items() if key != "alternatives"}
        primary["_candidate_role"] = "primary"
        if isinstance(primary.get("value_si"), (int, float)):
            candidates.append(primary)
        for raw in measurement.get("alternatives") or ():
            if not isinstance(raw, dict):
                continue
            alternative = {
                key: measurement.get(key) for key in inherited if measurement.get(key) is not None
            }
            alternative.update(raw)
            alternative.setdefault("id", output)
            alternative["_candidate_role"] = "alternative"
            if alternative.get("id") == output and isinstance(
                alternative.get("value_si"), (int, float)
            ):
                candidates.append(alternative)
    unique: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for candidate in candidates:
        key = (
            candidate.get("source"),
            candidate.get("method"),
            candidate.get("value_si"),
            candidate.get("frame"),
            candidate.get("direction"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def _candidate_mismatches(rule: MeasurementRule, candidate: dict[str, Any]) -> list[str]:
    assert rule.output is not None
    mismatches: list[str] = []
    expected_quantity = _expected_quantity(rule)
    quantity = candidate.get("quantity_kind")
    if quantity != expected_quantity:
        mismatches.append(
            f"quantity {quantity or 'unknown'} does not match required {expected_quantity}"
        )
    intrinsic = _intrinsic_scalar(rule.output)
    frame = candidate.get("frame")
    if frame is None:
        if not intrinsic:
            mismatches.append(f"frame is missing; required {rule.frame}")
    elif frame != rule.frame:
        mismatches.append(f"frame {frame} does not match required {rule.frame}")
    direction = candidate.get("direction")
    if rule.direction:
        if direction is None:
            if not intrinsic:
                mismatches.append(f"direction is missing; required {rule.direction}")
        elif direction != rule.direction:
            mismatches.append(f"direction {direction} does not match required {rule.direction}")
    return mismatches


def _fallback_for(rule: MeasurementRule, candidate: dict[str, Any]) -> str | None:
    source = str(candidate.get("source") or "")
    method = str(candidate.get("method") or "")
    for fallback in rule.fallbacks:
        support = _SUPPORTED_FALLBACKS.get(fallback)
        if support is None:
            continue
        if method in support["methods"] or (
            source in support["sources"] and source != "analysis_mesh"
        ):
            return fallback
    return None


def _candidate_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "value_si",
            "source",
            "method",
            "frame",
            "direction",
            "quantity_kind",
            "confidence",
            "uncertainty_si",
        )
        if candidate.get(key) is not None
    }


def _comparison_tolerance(
    rule: MeasurementRule, left: dict[str, Any], right: dict[str, Any]
) -> tuple[float, float]:
    left_value = float(left["value_si"])
    right_value = float(right["value_si"])
    delta = abs(left_value - right_value)
    limit = float(rule.tolerance.absolute_si or 0.0) + float(rule.tolerance.relative or 0.0) * max(
        abs(left_value), abs(right_value)
    )
    return delta, limit


def _rule_result(
    rule: MeasurementRule,
    record: dict[str, Any],
    *,
    cross_check: str,
    on_conflict: str,
    minimum_confidence: str | None,
    include_evidence: bool,
) -> tuple[str, dict[str, Any]]:
    assert rule.output is not None
    output = rule.output
    if rule.rule_type not in _SUPPORTED_RULE_TYPES:
        return "skipped", {
            "output": output,
            "reason": (
                f"rule_type {rule.rule_type} is not supported by deterministic "
                "object measurement replay"
            ),
            "supported_rule_types": sorted(_SUPPORTED_RULE_TYPES),
        }
    ambiguous_ids = _coverage_ids(record, "ambiguous")
    conflicting_ids = _coverage_ids(record, "conflicting")
    if output in ambiguous_ids:
        return "ambiguous", {
            "output": output,
            "reason": "analysis marked the measurement ambiguous",
        }

    candidates = _measurement_candidates(record, output)
    if not candidates:
        reason = "not returned by geometry analysis v2"
        if output in _coverage_ids(record, "unavailable"):
            reason = "reported unavailable by geometry analysis"
        if output in conflicting_ids:
            return "ambiguous", {
                "output": output,
                "reason": "analysis reported a source conflict without a usable candidate",
            }
        return "skipped", {"output": output, "reason": reason}

    compatible = [
        candidate for candidate in candidates if not _candidate_mismatches(rule, candidate)
    ]
    if not compatible:
        return "skipped", {
            "output": output,
            "reason": "no candidate matches the rule frame, direction, and quantity semantics",
            "candidate_mismatches": [
                {
                    **_candidate_summary(candidate),
                    "mismatches": _candidate_mismatches(rule, candidate),
                }
                for candidate in candidates[:8]
            ],
        }

    fallback_used: str | None = None
    eligible: list[dict[str, Any]] = []
    if rule.preferred_sources:
        for preferred in rule.preferred_sources:
            eligible = [
                candidate
                for candidate in compatible
                if str(candidate.get("source") or "") == preferred
            ]
            if eligible:
                break
        if not eligible:
            fallback_candidates = [
                (candidate, _fallback_for(rule, candidate)) for candidate in compatible
            ]
            fallback_candidates = [
                (candidate, fallback)
                for candidate, fallback in fallback_candidates
                if fallback is not None
            ]
            if fallback_candidates:
                fallback_used = fallback_candidates[0][1]
                eligible = [
                    candidate
                    for candidate, fallback in fallback_candidates
                    if fallback == fallback_used
                ]
            else:
                return "skipped", {
                    "output": output,
                    "reason": "no preferred source or explicitly supported fallback is available",
                    "preferred_sources": list(rule.preferred_sources),
                    "fallbacks": list(rule.fallbacks),
                    "available_sources": list(
                        dict.fromkeys(
                            str(candidate.get("source") or "unknown") for candidate in compatible
                        )
                    ),
                }
    else:
        eligible = compatible
    eligible.sort(
        key=lambda item: (
            -_confidence_rank(item.get("confidence")),
            0 if item.get("_candidate_role") == "primary" else 1,
            str(item.get("source") or ""),
        )
    )
    selected = eligible[0]
    required = max(
        _confidence_rank(rule.minimum_confidence),
        _confidence_rank(minimum_confidence) if minimum_confidence else 0,
    )
    actual = _confidence_rank(selected.get("confidence"))
    if actual < required:
        return "skipped", {
            "output": output,
            "reason": (
                f"confidence {selected.get('confidence') or 'unknown'} is below "
                f"the required {minimum_confidence or rule.minimum_confidence}"
            ),
        }

    verification: dict[str, Any]
    conflict: dict[str, Any] | None = None
    second_source = next(
        (
            candidate
            for candidate in compatible
            if candidate is not selected
            and (
                candidate.get("source") != selected.get("source")
                or candidate.get("method") != selected.get("method")
            )
        ),
        None,
    )
    if cross_check == "second_source_when_available" and second_source is not None:
        delta, tolerance = _comparison_tolerance(rule, selected, second_source)
        verification = {
            "cross_check": cross_check,
            "status": "passed" if delta <= tolerance else "conflict",
            "selected_source": selected.get("source"),
            "second_source": second_source.get("source"),
            "absolute_delta_si": round(delta, 12),
            "tolerance_si": round(tolerance, 12),
        }
        if delta > tolerance:
            conflict = {
                "reason": "independent compatible sources disagree beyond rule tolerance",
                "selected": _candidate_summary(selected),
                "second_source": _candidate_summary(second_source),
                "absolute_delta_si": round(delta, 12),
                "tolerance_si": round(tolerance, 12),
            }
    elif cross_check == "second_source_when_available":
        verification = {"cross_check": cross_check, "status": "second_source_unavailable"}
    else:
        verification = {"cross_check": cross_check, "status": "not_requested"}
    if output in conflicting_ids and conflict is None:
        conflict = {"reason": "geometry analysis reported a source conflict"}
        verification["status"] = "conflict"
    if conflict is not None:
        verification["on_conflict"] = on_conflict
        verification["conflict"] = conflict
        if on_conflict == "report_and_refuse_property_proposal":
            verification["property_proposal_allowed"] = False
            return "ambiguous", {
                "output": output,
                "reason": conflict["reason"],
                "preferred_candidate": _candidate_summary(selected),
                "verification": verification,
            }
        verification["status"] = "conflict_reported"

    result = {
        key: selected.get(key)
        for key in (
            "id",
            "label",
            "quantity_kind",
            "value_si",
            "value_file",
            "si_unit",
            "source",
            "method",
            "frame",
            "direction",
            "station",
            "component",
            "confidence",
            "uncertainty_si",
            "flags",
        )
        if selected.get(key) is not None
    }
    result["output"] = output
    result["selected_from"] = selected.get("_candidate_role")
    if fallback_used:
        result["fallback_used"] = fallback_used
    result["verification"] = verification
    exemplar_value = rule.intent.value_si
    if exemplar_value is not None:
        delta = abs(float(selected["value_si"]) - exemplar_value)
        limit = float(rule.tolerance.absolute_si or 0.0) + float(
            rule.tolerance.relative or 0.0
        ) * max(abs(float(selected["value_si"])), abs(exemplar_value))
        result["exemplar_comparison"] = {
            "absolute_delta_si": round(delta, 12),
            "relative_delta": round(delta / abs(exemplar_value), 9) if exemplar_value else None,
            "within_recorded_tolerance": delta <= limit,
            "tolerance_si": round(limit, 12),
        }
    if include_evidence:
        result["alternatives"] = [
            _candidate_summary(candidate) for candidate in compatible if candidate is not selected
        ][:8]
        result["recorded_intent"] = rule.intent.model_dump(mode="json", exclude_none=True)
    return "extracted", result


def _target_result(
    spec: MeasurementSkillSpec,
    record: dict[str, Any],
    *,
    current_revision: dict[str, Any] | None,
    minimum_confidence: str | None,
    include_evidence: bool,
) -> dict[str, Any]:
    applicability = _applicability(
        spec,
        record,
        current_revision=current_revision,
        include_evidence=include_evidence,
    )
    extracted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    if applicability["applicable"]:
        for rule in spec.measurements:
            category, result = _rule_result(
                rule,
                record,
                cross_check=spec.verification.cross_check,
                on_conflict=spec.verification.on_conflict,
                minimum_confidence=minimum_confidence,
                include_evidence=include_evidence,
            )
            {"extracted": extracted, "skipped": skipped, "ambiguous": ambiguous}[category].append(
                result
            )
    else:
        skipped.extend(
            {"output": output, "reason": "target is not applicable to this skill"}
            for output in spec.measurement_ids
        )
    if not applicability["applicable"]:
        status = "skipped"
    elif ambiguous:
        status = "ambiguous"
    elif extracted and skipped:
        status = "partial"
    elif extracted:
        status = "extracted"
    else:
        status = "skipped"
    result = {
        "object": _identity(record),
        "status": status,
        "applicability": applicability,
        "extracted": extracted,
        "skipped": skipped,
        "ambiguous": ambiguous,
        "flags": list(record.get("flags") or ()),
    }
    if include_evidence:
        result["coverage"] = record.get("coverage") or {}
    return result


def _failed_target_result(
    spec: MeasurementSkillSpec,
    global_id: str,
    *,
    reason: str,
    code: str = "GEOMETRY_ANALYSIS_FAILED",
) -> dict[str, Any]:
    return {
        "object": {"global_id": global_id, "class": None, "name": None, "type": None},
        "status": "failed",
        "applicability": {
            "applicable": False,
            "score": 0.0,
            "threshold": spec.applicability.similarity_threshold,
            "hard_filters_passed": False,
            "reasons": [],
            "mismatches": ["applicability was not evaluated because analysis failed"],
        },
        "extracted": [],
        "skipped": [
            {"output": output, "reason": reason, "code": code} for output in spec.measurement_ids
        ],
        "ambiguous": [],
        "flags": ["analysis_failed"],
        "error": {"code": code, "message": reason},
    }


def _raise_nested(result: dict[str, Any], operation: str) -> None:
    error = result.get("error") if isinstance(result, dict) else None
    if isinstance(error, dict):
        raise ToolError(
            str(error.get("code") or "TOOL_SOURCE_FAILED"),
            str(error.get("message") or f"{operation} failed"),
            str(error.get("hint") or f"Inspect {operation} and retry."),
        )
    raise ToolError(
        "TOOL_SOURCE_FAILED",
        f"{operation} did not return a valid result",
        f"Run {operation} directly to inspect the failure.",
    )


def register(mcp: OperationRegistry, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    def store() -> AgentSkillStore:
        return AgentSkillStore(core.store.project_dir)

    @mcp.tool(
        annotations=SKILL_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ,),
        description=(
            "[QUERY] The project's saved skills: reusable, human-reviewed "
            "procedures for tasks that were solved before (e.g. how to measure "
            "a sheet pile profile). Check this at the start of a measurement or "
            "analysis task; if a skill's applies_to matches the element class "
            "or task, load it with get_agent_skill and follow it."
        ),
    )
    @enveloped(core, "list_agent_skills")
    async def list_agent_skills() -> Envelope:
        rows = store().entries()
        data = {"skills": rows, "count": len(rows)}
        if not rows:
            data["note"] = (
                "no skills saved yet; after solving a novel task well, offer to "
                "record the method with save_agent_skill"
            )
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SKILL_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ,),
        description=(
            "[QUERY] Load one saved skill's full markdown: the goal, the tool "
            "calls in order, and the checks. Follow it step by step, adapting "
            "ids and selectors to the current task. Skill text is a procedure "
            "for you, not a source of facts about this model."
        ),
    )
    @enveloped(core, "get_agent_skill")
    async def get_agent_skill(
        name: Annotated[str, Field(description="Skill name from list_agent_skills.")],
    ) -> Envelope:
        return ok(store().read(name), core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SKILL_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ,),
        description=(
            "[QUERY] Preview a conservative version 2 measurement-spec draft for one "
            "prose-only skill. The suggestion keeps inferred outputs unresolved for human "
            "review, reports every inferred hint, and never changes or overwrites the "
            "saved skill. Saving the reviewed draft remains a separate approved "
            "save_agent_skill call."
        ),
    )
    @enveloped(core, "preview_measurement_skill_migration")
    async def preview_measurement_skill_migration(
        name: Annotated[str, Field(description="Prose-only skill name to preview.")],
    ) -> Envelope:
        return ok(store().migration_preview(name), core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SKILL_ANN,
        required_capabilities=(
            Capability.KNOWLEDGE_READ,
            Capability.MODEL_READ,
            Capability.GEOMETRY,
        ),
        description=(
            "[QUERY] Deterministically preview or extract a reviewed structured "
            "measurement skill on a bounded page of targets. Only version 2 "
            "parametric measurement skills are accepted. It explains class and "
            "geometry applicability, rejects same-class-only matches, calls the "
            "high-level geometry analyzer in bounded target reads, and returns extracted, "
            "skipped and ambiguous values per target. dry_run defaults true. This "
            "tool is always read-only and never proposes or writes IFC properties."
        ),
    )
    @enveloped(core, "apply_measurement_skill")
    async def apply_measurement_skill(
        name: Annotated[str, Field(description="Structured skill name.")],
        selector: Annotated[
            str | None,
            Field(description="IfcOpenShell selector; pass exactly one target form."),
        ] = None,
        global_ids: Annotated[
            list[str] | None,
            Field(
                max_length=200,
                description="Explicit GlobalIds; pass exactly one target form.",
            ),
        ] = None,
        model: Annotated[
            str | None,
            Field(description="model_id from the selection or attached-model list."),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(description="Preview applicability and extraction; defaults true."),
        ] = True,
        offset: Annotated[int, Field(ge=0, description="Targets to skip for paging.")] = 0,
        limit: Annotated[int, Field(ge=1, le=25, description="Targets in this page.")] = 10,
        max_matches: Annotated[
            int,
            Field(ge=1, le=200, description="Hard cap across pages for this application."),
        ] = 100,
        include_evidence: Annotated[
            bool,
            Field(description="Include bounded signatures, signals and analysis evidence."),
        ] = False,
        minimum_confidence: Annotated[
            Literal["low", "medium", "high", "exact"] | None,
            Field(description="Optional confidence floor across every skill rule."),
        ] = None,
    ) -> Envelope:
        has_selector = isinstance(selector, str) and bool(selector.strip())
        has_ids = isinstance(global_ids, list) and bool(global_ids)
        if has_selector == has_ids:
            raise ToolError(
                "INVALID_INPUT",
                "pass exactly one of selector or global_ids",
                "Use selector for a similar-object set or global_ids for a reviewed list.",
            )
        if offset >= max_matches:
            raise ToolError(
                "INVALID_INPUT",
                f"offset {offset} reaches the max_matches cap {max_matches}",
                "Increase max_matches deliberately, or restart at a lower offset.",
            )
        spec = store().measurement_spec(name)
        unresolved = [rule.intent.viewer_index for rule in spec.measurements if rule.unresolved]
        if unresolved:
            raise ToolError(
                "VALIDATION_FAILED",
                f"skill {name!r} has {len(unresolved)} unresolved recorded intent(s)",
                "Review and name or remove the unresolved viewer rows before applying it.",
            )
        if not spec.measurement_ids:
            raise ToolError(
                "VALIDATION_FAILED",
                f"skill {name!r} has no executable measurement outputs",
                "Review the skill and add at least one stable geometry-analysis v2 output.",
            )

        from ifc_console.sdk import AsyncWorkbench

        workbench = AsyncWorkbench(core)
        total = 0
        target_ids: list[str] = []
        requested_selector = selector.strip() if has_selector and selector else None
        if requested_selector is not None:
            query = await workbench.call(
                "query_elements",
                query=requested_selector,
                limit=min(limit, max_matches - offset),
                offset=offset,
                fields=[],
                model=model,
            )
            if not query.get("ok"):
                _raise_nested(query, "query_elements")
            rows = (query.get("data") or {}).get("rows") or []
            target_ids = [
                row["global_id"]
                for row in rows
                if isinstance(row, dict) and isinstance(row.get("global_id"), str)
            ]
            total_value = (query.get("meta") or {}).get("total")
            total = int(total_value) if isinstance(total_value, int) else len(target_ids)
        else:
            assert global_ids is not None
            unique_ids = list(dict.fromkeys(value for value in global_ids if value))
            if len(unique_ids) != len(dict.fromkeys(global_ids)):
                raise ToolError(
                    "INVALID_INPUT",
                    "global_ids contain empty values",
                    "Pass non-empty IFC GlobalIds only.",
                )
            total = len(unique_ids)
            target_ids = unique_ids[offset : min(offset + limit, max_matches)]

        capped_total = min(total, max_matches)
        results: list[dict[str, Any]] = []
        current_revision: dict[str, Any] = {}
        revision_notes: list[str] = []
        analysis_frames = {
            rule.frame
            for rule in spec.measurements
            if rule.rule_type in _SUPPORTED_RULE_TYPES
            and rule.frame in {"semantic", "placement", "principal", "world"}
        }
        analysis_frame = next(iter(analysis_frames)) if len(analysis_frames) == 1 else "semantic"
        # Analyze one object per nested call. The analyzer's own envelope is
        # size-bounded; batching here could truncate its elements list and make
        # a target disappear before this operation can report it.
        for target_id in target_ids:
            analysis = await workbench.call(
                "analyze_element_geometry",
                global_ids=[target_id],
                detail="compact",
                measurement_ids=list(spec.measurement_ids),
                frame=analysis_frame,
                station_strategy="auto",
                # Alternatives are execution input for source preference and
                # cross-checks even when the caller hides evidence in the response.
                include_alternatives=True,
                include_sections=False,
                include_outline=False,
                physical_only=True,
                max_elements=1,
                model=model,
            )
            if not analysis.get("ok"):
                try:
                    _raise_nested(analysis, "analyze_element_geometry")
                except ToolError as exc:
                    results.append(
                        _failed_target_result(spec, target_id, reason=exc.message, code=exc.code)
                    )
                continue
            raw_data = analysis.get("data")
            meta = analysis.get("meta") or {}
            if not isinstance(raw_data, dict):
                results.append(
                    _failed_target_result(
                        spec,
                        target_id,
                        reason="geometry analysis returned an invalid payload",
                        code="INVALID_OUTPUT",
                    )
                )
                continue
            if raw_data.get("truncation") or (
                isinstance(meta, dict) and meta.get("truncated") is True
            ):
                results.append(
                    _failed_target_result(
                        spec,
                        target_id,
                        reason=(
                            "geometry analysis for this target was truncated; "
                            "retry with fewer measurement outputs or without evidence"
                        ),
                        code="RESULT_TOO_LARGE",
                    )
                )
                continue
            revision = raw_data.get("model_revision")
            if isinstance(revision, dict) and revision:
                if not current_revision:
                    current_revision = dict(revision)
                elif revision != current_revision and (
                    "model revision changed during skill application" not in revision_notes
                ):
                    revision_notes.append("model revision changed during skill application")
            records = [
                record for record in raw_data.get("elements") or () if isinstance(record, dict)
            ]
            matching = [
                record for record in records if _identity(record).get("global_id") == target_id
            ]
            if len(matching) != 1:
                reason = (
                    "geometry analysis omitted the requested target"
                    if not matching
                    else "geometry analysis returned the requested target more than once"
                )
                results.append(
                    _failed_target_result(spec, target_id, reason=reason, code="INVALID_OUTPUT")
                )
                continue
            results.append(
                _target_result(
                    spec,
                    matching[0],
                    current_revision=revision if isinstance(revision, dict) else None,
                    minimum_confidence=minimum_confidence,
                    include_evidence=include_evidence,
                )
            )

        status_counts: dict[str, int] = {}
        extracted_count = skipped_count = ambiguous_count = 0
        for result in results:
            status = str(result["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
            extracted_count += len(result["extracted"])
            skipped_count += len(result["skipped"])
            ambiguous_count += len(result["ambiguous"])

        exemplar_revision = spec.exemplar.model_revision if spec.exemplar else None
        exemplar_objects = spec.exemplar.objects if spec.exemplar else ()
        exemplar_ids = {item.global_id for item in exemplar_objects}
        exemplar_results = [
            result
            for result in results
            if result.get("object", {}).get("global_id") in exemplar_ids
        ]
        if exemplar_revision and exemplar_revision.fingerprint:
            if current_revision.get("fingerprint") != exemplar_revision.fingerprint:
                revision_notes.append("target model differs from the recorded exemplar revision")
            elif (
                exemplar_revision.revision is not None
                and current_revision.get("revision") != exemplar_revision.revision
            ):
                revision_notes.append("target model revision changed since recording")
        data = {
            "skill": {
                "name": name,
                "kind": spec.kind,
                "schema_version": spec.schema_version,
                "measurement_ids": list(spec.measurement_ids),
                "similarity_threshold": spec.applicability.similarity_threshold,
                "exemplar": {
                    "object_count": len(exemplar_objects),
                    "objects": [
                        {
                            "global_id": item.global_id,
                            "ifc_class": item.ifc_class,
                            "type_name": item.type_name,
                            "geometry_family": item.geometry_family,
                            "has_geometry_signature": bool(item.geometry_signature),
                        }
                        for item in exemplar_objects
                    ],
                    "model_revision": (
                        exemplar_revision.model_dump(mode="json", exclude_none=True)
                        if exemplar_revision
                        else {}
                    ),
                },
            },
            "dry_run": dry_run,
            "execution_mode": "preview" if dry_run else "read_only_extract",
            "read_only": True,
            "side_effects": {"property_writes": 0, "proposals": 0, "file_writes": 0},
            "model_revision": current_revision,
            "revision_notes": revision_notes,
            "targets": {
                "selector": requested_selector,
                "requested_global_ids": None if requested_selector else len(global_ids or ()),
                "matched": total,
                "capped_total": capped_total,
                "offset": offset,
                "limit": limit,
                "returned": len(results),
                "all_page_targets_reported": len(results) == len(target_ids),
                "max_matches": max_matches,
                "has_more": offset + len(target_ids) < capped_total,
                "truncated_by_max_matches": total > max_matches,
            },
            "summary": {
                "target_statuses": status_counts,
                "extracted": extracted_count,
                "skipped": skipped_count,
                "ambiguous": ambiguous_count,
                "exemplar_targets_returned": len(exemplar_results),
                "exemplar_targets_applicable": sum(
                    1
                    for result in exemplar_results
                    if result.get("applicability", {}).get("applicable") is True
                ),
            },
            "results": results,
        }
        return ok(
            data,
            core.session_meta(),
            char_limit=limit_,
            total=capped_total,
            returned=len(results),
            offset=offset,
        )

    @mcp.tool(
        annotations=SKILL_WRITE_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ, Capability.FILE_WRITE),
        description=(
            "[ARTIFACT] Record a reusable skill as markdown in the project "
            "workspace, for future runs of any agent. Save one after solving a "
            "task the hard way: state when it applies, the exact tool calls in "
            "order with the arguments that worked, and how to verify the "
            "result. Keep it under a page; write the procedure, not this "
            "session's values."
        ),
    )
    @enveloped(core, "save_agent_skill")
    async def save_agent_skill(
        name: Annotated[
            str,
            Field(description="Lowercase slug, e.g. 'sheet-pile-profile'.", max_length=64),
        ],
        description: Annotated[
            str,
            Field(description="One line: what the skill does.", max_length=200),
        ],
        content: Annotated[
            str,
            Field(description="Markdown body: when to use, steps, checks.", max_length=60_000),
        ],
        applies_to: Annotated[
            str | None,
            Field(
                description="Classes or tasks it fits, e.g. 'IfcMember sheet piles'.",
                max_length=200,
            ),
        ] = None,
        kind: Annotated[
            Literal["parametric_measurement"] | None,
            Field(description="Structured skill kind; omit for prose skills."),
        ] = None,
        schema_version: Annotated[
            Literal[2] | None,
            Field(description="Structured schema version; omit for prose skills."),
        ] = None,
        overwrite: bool = False,
    ) -> Envelope:
        row = store().save(
            name,
            content,
            description=description,
            applies_to=applies_to,
            kind=kind,
            schema_version=schema_version,
            overwrite=overwrite,
        )
        core.audit.record("skill_write", name=name, path=row["path"], overwrite=overwrite)
        return ok({"saved": True, **row}, core.session_meta(), char_limit=limit_)
