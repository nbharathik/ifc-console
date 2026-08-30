"""Execute one workflow, step by step, as a stream of events.

The runner owns no agent logic of its own. An agent step composes blocks the
same way the panel does and streams through the same :class:`Agent`, so tool
blocks, approval cards, and AI provenance all behave exactly as they do in
chat. What the runner adds is order, a shared context between steps, and a
human gate that blocks the run until somebody answers.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from ifc_console.core.results import ToolError

from ifc_console_agents.models import AgentLimits
from ifc_console_agents.workflows import (
    AgentStep,
    ExportStep,
    GateStep,
    StepOutcome,
    ToolStep,
    WorkflowSpec,
    render,
    render_arguments,
    steps_in_order,
    validate_inputs,
)

log = logging.getLogger("ifc-console.agents")

# A gate is a person reading, not the run working, so it gets its own clock.
GATE_TIMEOUT_S = 3600.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowRunError(RuntimeError):
    """A workflow stopped before its last step."""


class WorkflowRunner:
    """Run one workflow against the console the panel is already serving."""

    def __init__(
        self,
        core: Any,
        *,
        model: Any,
        model_label: str = "",
        auto_approve: bool = False,
        instructions: str = "",
    ) -> None:
        self.core = core
        self.model = model
        self.model_label = model_label
        self.auto_approve = auto_approve
        self.instructions = instructions
        self.run_id = f"wfrun-{uuid4().hex[:16]}"

    # The panel state holds pending decisions; a headless run has none.
    def _panel_state(self) -> Any:
        return getattr(self.core, "agent_panel", None)

    def _approval_handler(self) -> Any:
        from ifc_console_agents.panel import AutoApprovalHandler, PanelApprovalHandler

        if self.auto_approve:
            return AutoApprovalHandler(self.core)
        state = self._panel_state()
        if state is None:
            from ifc_console_agents.approvals import DenyAllApprovals

            return DenyAllApprovals()
        return PanelApprovalHandler(state, owner=self)

    async def stream(
        self, spec: WorkflowSpec, inputs: Mapping[str, Any] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield panel-shaped events for one whole run."""
        from ifc_console_agents.panel import panel_runtime

        values = validate_inputs(spec, inputs or {})
        runtime = panel_runtime(self.core)
        toolset = await runtime.toolset()
        context: dict[str, Any] = {"inputs": values, "steps": {}}
        outcomes: list[StepOutcome] = []

        self.core.audit.record(
            "workflow_started", workflow=spec.name, run=self.run_id
        )
        yield {
            "type": "workflow_started",
            "run_id": self.run_id,
            "workflow": spec.name,
            "title": spec.title,
            "steps": [
                {"id": step.id, "kind": step.kind, "title": step.title or step.id}
                for step in spec.steps
            ],
        }

        failed = False
        for step in steps_in_order(spec):
            title = step.title or step.id
            if failed:
                outcome = StepOutcome(
                    id=step.id, kind=step.kind, title=title, state="skipped",
                    error="an earlier step did not finish",
                )
                outcome.finished_at = _now()
                outcomes.append(outcome)
                context["steps"][step.id] = outcome.as_context()
                yield {"type": "step_finished", **outcome.as_json()}
                continue

            yield {
                "type": "step_started",
                "id": step.id,
                "kind": step.kind,
                "title": title,
            }
            outcome = StepOutcome(id=step.id, kind=step.kind, title=title, state="succeeded")
            try:
                if isinstance(step, ToolStep):
                    await self._run_tool(step, toolset, context, outcome)
                elif isinstance(step, AgentStep):
                    async for event in self._run_agent(step, runtime, context, outcome):
                        yield event
                elif isinstance(step, GateStep):
                    async for event in self._run_gate(step, context, outcome):
                        yield event
                elif isinstance(step, ExportStep):
                    self._run_export(step, spec, context, outcome)
            except asyncio.CancelledError:
                outcome.state = "cancelled"
                outcome.error = "the run was interrupted"
                outcome.finished_at = _now()
                context["steps"][step.id] = outcome.as_context()
                outcomes.append(outcome)
                yield {"type": "step_finished", **outcome.as_json()}
                raise
            except (ToolError, WorkflowRunError, ValueError, RuntimeError) as exc:
                outcome.state = "failed"
                outcome.error = str(exc)
                log.info("workflow %s step %s failed: %s", spec.name, step.id, exc)

            outcome.finished_at = _now()
            context["steps"][step.id] = outcome.as_context()
            outcomes.append(outcome)
            yield {"type": "step_finished", **outcome.as_json()}
            if outcome.state in {"failed", "cancelled"}:
                failed = True

        state = "failed" if failed else "succeeded"
        self.core.audit.record(
            "workflow_finished", workflow=spec.name, run=self.run_id, state=state
        )
        yield {
            "type": "workflow_completed",
            "run_id": self.run_id,
            "workflow": spec.name,
            "state": state,
            "steps": [item.as_json() for item in outcomes],
            "summary": _final_text(outcomes),
        }

    async def _run_tool(
        self,
        step: ToolStep,
        toolset: Any,
        context: Mapping[str, Any],
        outcome: StepOutcome,
    ) -> None:
        if step.tool not in set(toolset.names):
            message = f"tool {step.tool!r} is not available in this session"
            if step.optional:
                outcome.state = "skipped"
                outcome.error = message
                return
            raise WorkflowRunError(message)
        arguments = render_arguments(dict(step.arguments), context)
        result = await toolset.call(step.tool, arguments)
        ok = bool(isinstance(result, Mapping) and result.get("ok"))
        data = result.get("data") if isinstance(result, Mapping) else None
        outcome.data = data
        outcome.text = _envelope_text(result)
        if not ok:
            message = _envelope_error(result)
            if step.optional:
                outcome.state = "skipped"
                outcome.error = message
                return
            raise WorkflowRunError(message)

    async def _run_agent(
        self,
        step: AgentStep,
        runtime: Any,
        context: Mapping[str, Any],
        outcome: StepOutcome,
    ) -> AsyncIterator[dict[str, Any]]:
        from ifc_console_agents.agent import Agent
        from ifc_console_agents.blocks import compose
        from ifc_console_agents.panel import _typed_payloads, panel_limits
        from ifc_console_agents.presets import PRESET_BY_NAME

        if self.model is None:
            raise WorkflowRunError(
                "this step needs a language model; choose a provider and model first"
            )
        blocks = step.blocks
        role = step.role
        if step.preset:
            preset = PRESET_BY_NAME.get(step.preset)
            if preset is None:
                raise WorkflowRunError(f"unknown agent preset {step.preset!r}")
            blocks = blocks or preset.blocks
            role = role or preset.role
        composition = await compose(
            runtime,
            blocks,
            role=role,
            extra_instructions=self.instructions,
            viewer=getattr(self.core, "viewer_supported", False),
            agent=f"workflow-{step.id}",
            model_label=self.model_label,
        )
        agent = Agent(
            name=f"workflow-{step.id}",
            model=self.model,
            tools=composition.tools,
            instructions=composition.instructions,
            limits=panel_limits(
                self.core,
                AgentLimits(
                    max_tool_rounds=step.max_tool_rounds,
                    max_tool_calls=step.max_tool_calls,
                ),
            ),
            approval_handler=self._approval_handler(),
        )
        prompt = render(step.prompt, context)
        text_parts: list[str] = []
        completed = False
        async for event in agent.stream(prompt, thread_id=f"{self.run_id}-{step.id}"):
            if event.type == "text_delta" and event.text:
                text_parts.append(event.text)
            elif event.type == "run_completed" and event.run_result is not None:
                completed = True
                outcome.text = event.run_result.text
            elif event.type == "run_failed":
                raise WorkflowRunError(event.text or "the agent step failed")
            for payload in _typed_payloads(event):
                yield {**payload, "step_id": step.id}
        if not outcome.text:
            outcome.text = "".join(text_parts).strip()
        if not completed and not outcome.text:
            raise WorkflowRunError("the agent step produced no answer")

    async def _run_gate(
        self, step: GateStep, context: Mapping[str, Any], outcome: StepOutcome
    ) -> AsyncIterator[dict[str, Any]]:
        message = render(step.message, context)
        detail = render(step.detail, context) if step.detail else ""
        if self.auto_approve:
            outcome.text = f"{message} (approved automatically)"
            self.core.audit.record(
                "workflow_gate_auto", run=self.run_id, step=step.id
            )
            return
        state = self._panel_state()
        if state is None:
            raise WorkflowRunError(
                "this workflow needs a human decision and nothing is watching the run"
            )
        request_id = f"gate-{uuid4().hex[:16]}"
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        state.pending_approvals[request_id] = (self, future)
        yield {
            "type": "gate_requested",
            "step_id": step.id,
            "request_id": request_id,
            "message": message,
            "detail": detail,
        }
        try:
            decision = await asyncio.wait_for(future, timeout=GATE_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise WorkflowRunError("nobody answered this decision in time") from exc
        finally:
            state.pending_approvals.pop(request_id, None)
        approved = bool(getattr(decision, "approved", decision))
        reason = str(getattr(decision, "reason", "") or "")
        self.core.audit.record(
            "workflow_gate_decided",
            run=self.run_id,
            step=step.id,
            approved=approved,
        )
        yield {
            "type": "gate_resolved",
            "step_id": step.id,
            "request_id": request_id,
            "approved": approved,
            "reason": reason,
        }
        if not approved:
            raise WorkflowRunError(reason or "a reviewer stopped the run here")
        outcome.text = f"{message} (approved{': ' + reason if reason else ''})"

    def _run_export(
        self,
        step: ExportStep,
        spec: WorkflowSpec,
        context: Mapping[str, Any],
        outcome: StepOutcome,
    ) -> None:
        body = render(step.body, context)
        ref = self.core.artifacts.put_text(
            body,
            name=f"{step.name}.md",
            kind="report",
            media_type="text/markdown",
            producer=f"workflow:{spec.name}",
            metadata={"workflow": spec.name, "run_id": self.run_id, "step": step.id},
        )
        outcome.text = body
        outcome.artifact = {
            "artifact_id": getattr(ref, "artifact_id", None),
            "name": getattr(ref, "name", f"{step.name}.md"),
            "media_type": getattr(ref, "media_type", "text/markdown"),
            "size_bytes": getattr(ref, "size_bytes", len(body.encode("utf-8"))),
        }


def _envelope_text(result: Any) -> str:
    """A short, readable digest of one tool envelope for the next prompt."""
    if not isinstance(result, Mapping):
        return ""
    data = result.get("data")
    if isinstance(data, Mapping):
        for key in ("summary", "message", "text", "report"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    import json

    try:
        return json.dumps(data, ensure_ascii=False, default=str)[:4000]
    except (TypeError, ValueError):
        return str(data)[:4000]


def _envelope_error(result: Any) -> str:
    if isinstance(result, Mapping):
        error = result.get("error")
        if isinstance(error, Mapping):
            code = error.get("code") or "TOOL_FAILED"
            message = error.get("message") or "the tool reported a failure"
            return f"{code}: {message}"
    return "the tool reported a failure"


def _final_text(outcomes: list[StepOutcome]) -> str:
    """The last thing worth showing: the newest report, else the last answer."""
    for outcome in reversed(outcomes):
        if outcome.kind == "export" and outcome.state == "succeeded" and outcome.text:
            return outcome.text
    for outcome in reversed(outcomes):
        if outcome.kind == "agent" and outcome.state == "succeeded" and outcome.text:
            return outcome.text
    return ""


__all__ = ["GATE_TIMEOUT_S", "WorkflowRunError", "WorkflowRunner"]
