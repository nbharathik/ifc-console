"""Prompt-level contracts for the selected-object geometry workflow."""

from __future__ import annotations

from ifc_console_agents.presets import (
    GENERAL_ROLE,
    MEASUREMENT,
    PARAMETERS,
    PRESET_BY_NAME,
    PRESETS,
    REVIEW_ROLE,
)


def test_general_agent_pins_selection_and_keeps_proposals_separate():
    role = " ".join(GENERAL_ROLE.split())
    assert "get_viewer_selection" in role
    assert "pass its model_id to every later tool" in role
    assert "extracted, unavailable, ambiguous, and conflicting" in role
    assert "Applying a skill does not write properties" in role
    assert "preview_measurement_skill_migration" in role


def test_measurement_agent_has_all_required_geometry_examples():
    titles = {example.title for example in MEASUREMENT.examples}
    assert titles == {
        "Analyze one selected wall",
        "Analyze a rotated profile",
        "Learn web and flange thickness",
        "Preview genuinely similar members",
        "Refuse unsafe hollow thickness",
        "Report a tapered object",
    }
    assert "detail='compact'" in MEASUREMENT.role
    assert "apply_measurement_skill with dry_run=true" in MEASUREMENT.role
    assert "preview_measurement_skill_migration" in MEASUREMENT.role
    assert "A taper is a range or station function" in MEASUREMENT.role


def test_general_and_review_agents_route_gap_and_quality_questions_to_tools():
    general = " ".join(GENERAL_ROLE.split())
    assert "audit_element_properties first" in general
    assert "assess_model_quality for the scorecard" in general
    assert general.index("assess_model_quality") < general.index("check_model_health")
    review = " ".join(REVIEW_ROLE.split())
    assert "assess_model_quality" in review
    assert review.index("assess_model_quality") < review.index("validate_model")


def test_parameters_agent_audits_then_gathers_evidence_then_proposes_last():
    """Gap list before evidence, evidence before candidates, proposals only on
    request: the order is the safety property."""
    assert PRESET_BY_NAME["parameters"] is PARAMETERS
    assert [preset.name for preset in PRESETS] == [
        "general",
        "measurement",
        "parameters",
        "docs",
        "review",
    ]
    role = " ".join(PARAMETERS.role.split())
    audit = role.index("audit_element_properties")
    geometry = role.index("analyze_element_geometry")
    documents = role.index("list_project_documents")
    propose = role.index("propose_property_value")
    assert audit < geometry < documents < propose
    assert "detail='compact', frame='semantic', and station_strategy='auto'" in role
    assert "source='derived'" in role
    assert "as pixels" in role
    assert "uncalibrated image" in role
    assert "Leave a value out rather than guess" in role
    assert "Proposal candidates" in role
    assert "Propose only when the user asks or a workflow gate approved" in role
    assert {"ifc-context", "documents", "measurements", "viewer", "property-proposals", "code"} <= set(
        PARAMETERS.blocks
    )
    assert "vision" in PARAMETERS.info().features
    assert "proposals" in PARAMETERS.info().features
