"""Running a whole workflow: steps in order, gates, exports, and failures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ifc_console_agents.testing import ScriptedAgentModel, text_round
from ifc_console_agents.workflow_runner import WorkflowRunner
from ifc_console_agents.workflows import parse_workflow

pytestmark = pytest.mark.asyncio


TOOL_THEN_AGENT = """
version: "1"
name: tool-then-agent
title: Tool then agent
steps:
  - id: info
    kind: tool
    title: Project info
    tool: get_ifc_project_info
  - id: explain
    kind: agent
    title: Explain
    needs: [info]
    blocks: [ifc-context]
    prompt: "Summarise this project: {{ steps.info.text }}"
  - id: report
    kind: export
    title: Report
    needs: [explain]
    name: test-report
    body: |
      # Report
      {{ steps.explain.text }}
"""


@pytest.fixture
async def workflow_core(core, work_model: Path):
    from ifc_console.application.operations import build_operations

    build_operations(core)
    core.start_audit()
    await core.open_model(work_model)
    core.enable_chat()
    return core


async def _collect(runner, spec, inputs=None):
    return [event async for event in runner.stream(spec, inputs or {})]


async def test_a_workflow_runs_its_steps_in_order(workflow_core):
    runner = WorkflowRunner(
        workflow_core,
        model=ScriptedAgentModel([text_round("A small office building.")]),
        auto_approve=True,
    )
    events = await _collect(runner, parse_workflow(TOOL_THEN_AGENT))

    kinds = [event["type"] for event in events]
    assert kinds[0] == "workflow_started"
    assert kinds[-1] == "workflow_completed"

    finished = [event for event in events if event["type"] == "step_finished"]
    assert [item["id"] for item in finished] == ["info", "explain", "report"]
    assert all(item["state"] == "succeeded" for item in finished)
    assert events[-1]["state"] == "succeeded"


async def test_a_step_result_reaches_the_next_prompt(workflow_core):
    model = ScriptedAgentModel([text_round("done")])
    runner = WorkflowRunner(workflow_core, model=model, auto_approve=True)
    await _collect(runner, parse_workflow(TOOL_THEN_AGENT))

    prompt = model.turns[0]["messages"][-1].text
    assert "Summarise this project:" in prompt
    # The tool envelope, not an empty placeholder, is what the agent received.
    assert "{{" not in prompt
    assert len(prompt) > len("Summarise this project: ")


async def test_an_agent_step_only_carries_the_blocks_it_declares(workflow_core):
    model = ScriptedAgentModel([text_round("done")])
    runner = WorkflowRunner(workflow_core, model=model, auto_approve=True)
    await _collect(runner, parse_workflow(TOOL_THEN_AGENT))

    offered = set(model.turns[0]["tools"])
    assert "get_ifc_project_info" in offered
    # ifc-context does not carry measurement or code tools; a scoped stage is
    # the whole point of declaring blocks.
    assert "execute_ifc_code" not in offered
    assert "measure_local_thickness" not in offered


async def test_an_export_step_writes_an_artifact(workflow_core):
    runner = WorkflowRunner(
        workflow_core,
        model=ScriptedAgentModel([text_round("All good.")]),
        auto_approve=True,
    )
    events = await _collect(runner, parse_workflow(TOOL_THEN_AGENT))

    export = next(
        event
        for event in events
        if event["type"] == "step_finished" and event["id"] == "report"
    )
    assert export["artifact"]["artifact_id"].startswith("sha256:")
    assert export["artifact"]["name"] == "test-report.md"
    assert "All good." in export["text"]
    assert "All good." in events[-1]["summary"]


async def test_a_failed_tool_step_skips_the_rest(workflow_core):
    spec = parse_workflow(
        """
version: "1"
name: broken
title: Broken
steps:
  - id: missing
    kind: tool
    tool: no_such_tool
  - id: after
    kind: agent
    blocks: [ifc-context]
    prompt: never runs
    needs: [missing]
"""
    )
    runner = WorkflowRunner(
        workflow_core, model=ScriptedAgentModel([]), auto_approve=True
    )
    events = await _collect(runner, spec)

    states = {
        event["id"]: event["state"]
        for event in events
        if event["type"] == "step_finished"
    }
    assert states == {"missing": "failed", "after": "skipped"}
    assert events[-1]["state"] == "failed"


async def test_an_optional_tool_step_is_skipped_not_fatal(workflow_core):
    spec = parse_workflow(
        """
version: "1"
name: optional
title: Optional
steps:
  - id: missing
    kind: tool
    tool: no_such_tool
    optional: true
  - id: after
    kind: agent
    blocks: [ifc-context]
    prompt: "ran anyway"
    needs: [missing]
"""
    )
    runner = WorkflowRunner(
        workflow_core,
        model=ScriptedAgentModel([text_round("finished")]),
        auto_approve=True,
    )
    events = await _collect(runner, spec)

    states = {
        event["id"]: event["state"]
        for event in events
        if event["type"] == "step_finished"
    }
    assert states == {"missing": "skipped", "after": "succeeded"}
    assert events[-1]["state"] == "succeeded"


GATED = """
version: "1"
name: gated
title: Gated
steps:
  - id: hold
    kind: gate
    message: "Approve before continuing"
  - id: after
    kind: agent
    blocks: [ifc-context]
    prompt: "continue"
    needs: [hold]
"""


async def test_a_gate_blocks_until_a_decision_lands(workflow_core):
    from ifc_console_agents.models import ApprovalDecision
    from ifc_console_agents.panel import _panel_state

    state = _panel_state(workflow_core)
    runner = WorkflowRunner(
        workflow_core,
        model=ScriptedAgentModel([text_round("carried on")]),
        auto_approve=False,
    )
    events: list[dict] = []
    stream = runner.stream(parse_workflow(GATED), {})

    async for event in stream:
        events.append(event)
        if event["type"] == "gate_requested":
            # The run is now waiting on exactly this decision.
            _owner, future = state.pending_approvals[event["request_id"]]
            assert not future.done()
            future.set_result(ApprovalDecision(approved=True, decided_by="test"))

    assert any(event["type"] == "gate_resolved" for event in events)
    states = {
        event["id"]: event["state"]
        for event in events
        if event["type"] == "step_finished"
    }
    assert states == {"hold": "succeeded", "after": "succeeded"}


async def test_a_refused_gate_stops_the_run(workflow_core):
    from ifc_console_agents.models import ApprovalDecision
    from ifc_console_agents.panel import _panel_state

    state = _panel_state(workflow_core)
    runner = WorkflowRunner(
        workflow_core, model=ScriptedAgentModel([]), auto_approve=False
    )
    events: list[dict] = []
    async for event in runner.stream(parse_workflow(GATED), {}):
        events.append(event)
        if event["type"] == "gate_requested":
            _owner, future = state.pending_approvals[event["request_id"]]
            future.set_result(
                ApprovalDecision(approved=False, decided_by="test", reason="not yet")
            )

    states = {
        event["id"]: event["state"]
        for event in events
        if event["type"] == "step_finished"
    }
    assert states["hold"] == "failed"
    assert states["after"] == "skipped"
    assert events[-1]["state"] == "failed"


async def test_a_gate_leaves_no_pending_decision_behind(workflow_core):
    from ifc_console_agents.models import ApprovalDecision
    from ifc_console_agents.panel import _panel_state

    state = _panel_state(workflow_core)
    runner = WorkflowRunner(
        workflow_core,
        model=ScriptedAgentModel([text_round("ok")]),
        auto_approve=False,
    )
    async for event in runner.stream(parse_workflow(GATED), {}):
        if event["type"] == "gate_requested":
            _owner, future = state.pending_approvals[event["request_id"]]
            future.set_result(ApprovalDecision(approved=True, decided_by="test"))
    assert state.pending_approvals == {}


async def test_auto_approve_does_not_wait_for_anyone(workflow_core):
    runner = WorkflowRunner(
        workflow_core,
        model=ScriptedAgentModel([text_round("straight through")]),
        auto_approve=True,
    )
    events = await asyncio.wait_for(
        _collect(runner, parse_workflow(GATED)), timeout=10
    )
    assert not any(event["type"] == "gate_requested" for event in events)
    assert events[-1]["state"] == "succeeded"


async def test_a_workflow_without_agent_steps_needs_no_model(workflow_core):
    """Tools, gates, and a report are work the console does on its own."""
    spec = parse_workflow(
        """
version: "1"
name: no-model
title: No model
steps:
  - id: info
    kind: tool
    tool: get_ifc_project_info
  - id: report
    kind: export
    name: no-model-report
    body: "# Report\\n{{ steps.info.text }}"
    needs: [info]
"""
    )
    runner = WorkflowRunner(workflow_core, model=None, auto_approve=True)
    events = await _collect(runner, spec)
    assert events[-1]["state"] == "succeeded"


async def test_an_agent_step_without_a_model_fails_clearly(workflow_core):
    spec = parse_workflow(
        """
version: "1"
name: needs-model
title: Needs model
steps:
  - id: think
    kind: agent
    blocks: [ifc-context]
    prompt: anything
"""
    )
    runner = WorkflowRunner(workflow_core, model=None, auto_approve=True)
    events = await _collect(runner, spec)
    finished = next(event for event in events if event["type"] == "step_finished")
    assert finished["state"] == "failed"
    assert "language model" in finished["error"]


async def test_required_inputs_are_enforced_before_anything_runs(workflow_core):
    from ifc_console.core.results import ToolError

    spec = parse_workflow(
        """
version: "1"
name: needs-input
title: Needs input
inputs:
  - id: target
    label: Target
    type: text
    required: true
steps:
  - id: info
    kind: tool
    tool: get_ifc_project_info
"""
    )
    runner = WorkflowRunner(
        workflow_core, model=ScriptedAgentModel([]), auto_approve=True
    )
    with pytest.raises(ToolError):
        await _collect(runner, spec, {})
