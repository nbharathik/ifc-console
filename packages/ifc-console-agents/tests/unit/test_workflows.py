"""Workflow specs, the registry, and template rendering."""

from __future__ import annotations

import pytest
from ifc_console.core.results import ToolError

from ifc_console_agents.workflows import (
    AgentStep,
    ExportStep,
    ToolStep,
    WorkflowRegistry,
    parse_workflow,
    render,
    render_arguments,
    validate_inputs,
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
    assert {"revision-qa-gate", "revision-diff-review", "measurement-audit"} <= set(specs)
    for spec in specs.values():
        assert spec.steps
        assert spec.title


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
