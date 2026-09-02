"""Workflow specs, the registry, and template rendering."""

from __future__ import annotations

import pytest
from ifc_console.core.results import ToolError

from ifc_console_agents.workflows import (
    CHAT_INSTRUCTIONS_CHARS,
    AgentStep,
    ExportStep,
    ToolStep,
    WorkflowRegistry,
    chat_instructions,
    chat_task_prompt,
    parse_workflow,
    render,
    render_arguments,
    validate_inputs,
    validate_settings,
)

MINIMAL = """
version: "1"
name: sample
title: Sample workflow
steps:
  - id: check
    kind: tool
    tool: validate_model
"""


def test_parses_a_minimal_workflow() -> None:
    spec = parse_workflow(MINIMAL)
    assert spec.name == "sample"
    assert len(spec.steps) == 1
    assert isinstance(spec.steps[0], ToolStep)


def test_step_kinds_are_discriminated() -> None:
    spec = parse_workflow(
        """
version: "1"
name: mixed
title: Mixed
steps:
  - id: one
    kind: tool
    tool: validate_model
  - id: two
    kind: agent
    preset: review
    prompt: Explain {{ steps.one.text }}
    needs: [one]
  - id: three
    kind: export
    body: "# Report"
    needs: [two]
"""
    )
    kinds = [type(step) for step in spec.steps]
    assert kinds == [ToolStep, AgentStep, ExportStep]


def test_rejects_a_dependency_that_runs_later() -> None:
    with pytest.raises(ToolError) as excinfo:
        parse_workflow(
            """
version: "1"
name: backwards
title: Backwards
steps:
  - id: first
    kind: tool
    tool: validate_model
    needs: [second]
  - id: second
    kind: tool
    tool: check_model_health
"""
        )
    assert "do not run before it" in str(excinfo.value)


def test_rejects_duplicate_step_ids() -> None:
    with pytest.raises(ToolError):
        parse_workflow(
            """
version: "1"
name: dupes
title: Dupes
steps:
  - id: same
    kind: tool
    tool: validate_model
  - id: same
    kind: tool
    tool: check_model_health
"""
        )


def test_agent_step_needs_a_preset_or_blocks() -> None:
    with pytest.raises(ToolError):
        parse_workflow(
            """
version: "1"
name: bare
title: Bare
steps:
  - id: think
    kind: agent
    prompt: Do something
"""
        )


def test_agent_step_can_use_an_installed_agent() -> None:
    spec = parse_workflow(
        """
version: "1"
name: named-agent
title: Named agent
scope: either
steps:
  - id: review
    kind: agent
    agent: custom-reviewer
    prompt: Review the current scope
"""
    )
    step = spec.steps[0]
    assert isinstance(step, AgentStep)
    assert step.agent == "custom-reviewer"
    assert spec.summary()["scope"] == "either"
    assert spec.summary()["agents"] == ["custom-reviewer"]


def test_workflow_system_prompt_and_settings_are_listed() -> None:
    spec = parse_workflow(
        """
version: "1"
name: prompt-workflow
title: Prompt workflow
system_prompt: Review {{ settings.topic }} with evidence.
additional_instructions: Use concise project language.
settings:
  topic: doors
steps:
  - id: review
    kind: agent
    preset: review
    prompt: Carry out the workflow.
"""
    )

    summary = spec.summary()
    assert summary["system_prompt"] == "Review {{ settings.topic }} with evidence."
    assert summary["additional_instructions"] == "Use concise project language."
    assert summary["settings"] == {"topic": "doors"}
    assert validate_settings(spec, {"topic": "walls", "audience": "client"}) == {
        "topic": "walls",
        "audience": "client",
    }


def test_free_form_settings_are_bounded_and_can_remove_defaults() -> None:
    spec = parse_workflow(
        """
version: "1"
name: settings
title: Settings
settings:
  tolerance: 2 percent
steps:
  - id: check
    kind: tool
    tool: validate_model
"""
    )

    assert validate_settings(spec, None) == {"tolerance": "2 percent"}
    assert validate_settings(spec, {}) == {}
    assert validate_settings(spec, {"tolerance": ""}) == {}
    with pytest.raises(ToolError):
        validate_settings(spec, {"not/a/key": "value"})
    with pytest.raises(ToolError):
        validate_settings(spec, {f"key{index}": "value" for index in range(33)})


def test_render_substitutes_inputs_and_step_results() -> None:
    context = {
        "inputs": {"selector": "IfcWall"},
        "steps": {"scope": {"text": "12 walls", "data": {"count": 12}}},
    }
    assert render("Audit {{ inputs.selector }}", context) == "Audit IfcWall"
    assert render("Found {{ steps.scope.text }}", context) == "Found 12 walls"
    assert render("Count {{ steps.scope.data.count }}", context) == "Count 12"


def test_render_leaves_unknown_references_empty() -> None:
    assert render("value={{ inputs.missing }}", {"inputs": {}}) == "value="


def test_render_does_not_execute_anything() -> None:
    context = {"inputs": {"x": "{{ inputs.x }}"}}
    # A substituted value is inserted literally, never re-scanned.
    assert render("{{ inputs.x }}", context) == "{{ inputs.x }}"


def test_render_arguments_walks_nested_structures() -> None:
    context = {"inputs": {"name": "IfcDoor", "n": 3}}
    rendered = render_arguments(
        {"selector": "{{ inputs.name }}", "nested": ["{{ inputs.n }}", 7]}, context
    )
    assert rendered == {"selector": "IfcDoor", "nested": ["3", 7]}


def test_validate_inputs_applies_defaults_and_types() -> None:
    spec = parse_workflow(
        """
version: "1"
name: typed
title: Typed
inputs:
  - id: count
    label: Count
    type: number
    default: 5
  - id: flag
    label: Flag
    type: boolean
    default: false
  - id: pick
    label: Pick
    type: choice
    choices: [a, b]
    default: a
steps:
  - id: check
    kind: tool
    tool: validate_model
"""
    )
    values = validate_inputs(spec, {})
    assert values == {"count": 5.0, "flag": False, "pick": "a"}
    values = validate_inputs(spec, {"count": "12", "flag": "true", "pick": "b"})
    assert values == {"count": 12.0, "flag": True, "pick": "b"}


def test_validate_inputs_rejects_a_bad_choice_and_missing_required() -> None:
    spec = parse_workflow(
        """
version: "1"
name: strict
title: Strict
inputs:
  - id: pick
    label: Pick
    type: choice
    choices: [a, b]
  - id: needed
    label: Needed
    type: text
    required: true
steps:
  - id: check
    kind: tool
    tool: validate_model
"""
    )
    with pytest.raises(ToolError):
        validate_inputs(spec, {"needed": "x", "pick": "z"})
    with pytest.raises(ToolError):
        validate_inputs(spec, {})


def test_builtin_workflows_all_parse() -> None:
    registry = WorkflowRegistry(project_dir=__import__("pathlib").Path("."))
    specs = registry._load_dir(WorkflowRegistry.builtin_dir(), "builtin")
    assert {
        "coordination-clash-review",
        "element-parameters",
        "measurement-audit",
        "model-quality-review",
        "property-completeness-review",
        "quantity-snapshot",
        "quick-model-check",
        "revision-diff-review",
        "revision-qa-gate",
    } == set(specs)
    for spec in specs.values():
        assert spec.steps
        assert spec.title


def test_element_parameters_analyzes_then_gates_then_proposes() -> None:
    """The write path is a separate stage behind a human decision, and the
    analysis stage cannot reach a proposal tool at all."""
    registry = WorkflowRegistry(project_dir=__import__("pathlib").Path("."))
    spec = registry.get("element-parameters")
    rows = {row["name"]: row for row in registry.entries()}

    assert spec.scope == "selection"
    assert rows["element-parameters"]["has_gate"] is True
    assert rows["element-parameters"]["requires_model"] is True
    assert [step.kind for step in spec.steps] == ["agent", "gate", "agent", "export"]
    analyze, gate, propose, _ = spec.steps
    assert analyze.preset == "parameters"
    assert "property-proposals" not in analyze.blocks
    assert "documents" in analyze.blocks and "code" in analyze.blocks
    assert "{{ steps.analyze.text }}" in gate.detail
    assert set(propose.blocks) == {"ifc-context", "property-proposals"}
    assert "review" in propose.needs


def test_model_quality_review_scores_deterministically_before_the_agent() -> None:
    registry = WorkflowRegistry(project_dir=__import__("pathlib").Path("."))
    spec = registry.get("model-quality-review")

    assert spec.scope == "model"
    tools = [step for step in spec.steps if step.kind == "tool"]
    assert tools[0].tool == "assess_model_quality"
    assert tools[0].optional is False
    assert {step.tool for step in tools[1:]} == {"check_model_health", "validate_model"}
    assert all(step.optional for step in tools[1:])
    agent = next(step for step in spec.steps if step.kind == "agent")
    assert "{{ steps.scorecard.text }}" in agent.prompt
    assert "validation" in agent.blocks


def test_workflow_summaries_describe_model_and_gate_requirements() -> None:
    registry = WorkflowRegistry(project_dir=__import__("pathlib").Path("."))
    rows = {row["name"]: row for row in registry.entries()}

    assert rows["quick-model-check"]["requires_model"] is False
    assert rows["quick-model-check"]["has_gate"] is False
    assert rows["coordination-clash-review"]["requires_model"] is True
    assert rows["coordination-clash-review"]["has_gate"] is True


def test_project_workflows_override_builtins(tmp_path) -> None:
    directory = tmp_path / ".ifc-console" / "agents" / "workflows"
    directory.mkdir(parents=True)
    (directory / "revision-qa-gate.yaml").write_text(
        MINIMAL.replace("name: sample", "name: revision-qa-gate"), encoding="utf-8"
    )
    registry = WorkflowRegistry(tmp_path)
    assert registry.get("revision-qa-gate").title == "Sample workflow"
    origins = {row["name"]: row["origin"] for row in registry.entries()}
    assert origins["revision-qa-gate"] == "project"
    assert origins["measurement-audit"] == "builtin"


def test_project_workflow_save_is_atomic_and_refuses_overwrite(tmp_path) -> None:
    registry = WorkflowRegistry(tmp_path)
    spec = parse_workflow(MINIMAL)
    path = registry.save(spec)

    assert path.is_file()
    assert registry.get("sample") == spec
    with pytest.raises(ToolError) as excinfo:
        registry.save(spec)
    assert excinfo.value.code == "FILE_EXISTS"


def test_unknown_workflow_names_are_reported(tmp_path) -> None:
    with pytest.raises(ToolError) as excinfo:
        WorkflowRegistry(tmp_path).get("nope")
    assert "no workflow named" in str(excinfo.value)


def test_a_broken_file_does_not_hide_the_others(tmp_path) -> None:
    directory = tmp_path / ".ifc-console" / "agents" / "workflows"
    directory.mkdir(parents=True)
    (directory / "broken.yaml").write_text("steps: [[[", encoding="utf-8")
    (directory / "good.yaml").write_text(MINIMAL, encoding="utf-8")
    names = {row["name"] for row in WorkflowRegistry(tmp_path).entries()}
    assert "sample" in names


STAGED = """
version: "1"
name: staged
title: Staged review
scope: either
system_prompt: |
  Review the {{ settings.audience }} view of the model.
settings:
  audience: coordinator
steps:
  - id: health
    kind: tool
    tool: check_model_health
    arguments: {detail: "{{ settings.audience }}"}
    optional: true
  - id: review
    kind: agent
    preset: review
    prompt: Explain the health result for {{ settings.audience }}.
  - id: approve
    kind: gate
    message: Export the findings?
  - id: report
    kind: export
    body: |
      # Findings
      {{ steps.review.text }}
"""


def test_chat_instructions_turn_the_steps_into_a_procedure() -> None:
    spec = parse_workflow(STAGED)
    text = chat_instructions(
        spec,
        scope={"mode": "selection", "count": 2, "text": "Use only the two selected walls."},
        note="Focus on level 2.",
    )
    assert text.startswith("Review the coordinator view of the model.")
    assert "Scope:\nUse only the two selected walls." in text
    assert "- audience: coordinator" in text
    assert "Additional guidance:\nFocus on level 2." in text
    procedure = text.split("Procedure for this workflow, in order:", 1)[1]
    assert 'call check_model_health with {"detail": "coordinator"}. Skip it if the call fails.' in procedure
    assert "Explain the health result for coordinator." in procedure
    assert "stop and ask the user before continuing: Export the findings?" in procedure
    # A step reference has no runner to fill it in a chat, so it is named.
    assert "<steps.review.text>" in procedure
    assert "{{" not in text


def test_chat_instructions_stay_bounded_and_default_the_settings() -> None:
    long_prompt = "x" * 19_000
    spec = parse_workflow(
        STAGED.replace("  Review the {{ settings.audience }} view of the model.", "  " + long_prompt)
    )
    padding = {f"context_{index}": "y" * 2000 for index in range(8)}
    text = chat_instructions(spec, settings={"audience": "site manager", **padding})
    assert len(text) <= CHAT_INSTRUCTIONS_CHARS + 60
    assert text.endswith("[workflow instructions truncated]")
    assert "- audience: site manager" in text


def test_chat_task_prompt_prefers_the_first_agent_step() -> None:
    assert chat_task_prompt(parse_workflow(STAGED)) == "Explain the health result for coordinator."
    assert chat_task_prompt(parse_workflow(MINIMAL)).startswith("Carry out the workflow procedure")
