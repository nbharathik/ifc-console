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
from collections.abc import AsyncIterator, Mapping, Sequence
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
    validate_settings,
)

log = logging.getLogger("ifc-console.agents")

# A gate is a person reading, not the run working, so it gets its own clock.
GATE_TIMEOUT_S = 3600.0

# A follow-up carries the finished run back to the model. Caps keep a long
# report from crowding out the question itself.
FOLLOW_UP_REPORT_CHARS = 12_000
FOLLOW_UP_TURN_CHARS = 4_000
FOLLOW_UP_TURNS = 8


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
        self,
        spec: WorkflowSpec,
        inputs: Mapping[str, Any] | None = None,
        *,
        scope: str = "model",
        note: str = "",
        settings: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield panel-shaped events for one whole run."""
        from ifc_console_agents.panel import panel_runtime

        values = validate_inputs(spec, inputs or {})
        run_settings = validate_settings(spec, settings)
        runtime = panel_runtime(self.core)
        toolset = await runtime.toolset()
        viewer_scope = self._viewer_scope(scope)
        context: dict[str, Any] = {
            "inputs": values,
            "settings": run_settings,
            "steps": {},
            "scope": viewer_scope,
            "run": {"note": note.strip()},
        }
        outcomes: list[StepOutcome] = []

        self.core.audit.record(
            "workflow_started", workflow=spec.name, run=self.run_id
        )
        yield {
            "type": "workflow_started",
            "run_id": self.run_id,
            "workflow": spec.name,
            "title": spec.title,
            "scope": viewer_scope,
            "steps": [
                {"id": step.id, "kind": step.kind, "title": step.title or step.id}
                for step in spec.steps
            ],
        }
        yield {
            "type": "workflow_context",
            "run_id": self.run_id,
            "system_prompt": self._workflow_instructions(spec, context),
            "settings": run_settings,
            "guidance": note.strip(),
            "scope": viewer_scope,
            "model": self.model_label,
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

            started_event: dict[str, Any] = {
                "type": "step_started",
                "id": step.id,
                "kind": step.kind,
                "title": title,
            }
            if isinstance(step, ToolStep):
                started_event["tool"] = step.tool
                started_event["arguments"] = render_arguments(
                    dict(step.arguments), context
                )
            yield started_event
            outcome = StepOutcome(id=step.id, kind=step.kind, title=title, state="succeeded")
            try:
                if isinstance(step, ToolStep):
                    await self._run_tool(step, toolset, context, outcome)
                elif isinstance(step, AgentStep):
                    async for event in self._run_agent(
                        step,
                        runtime,
                        context,
                        outcome,
                        workflow_prompt=spec.system_prompt,
                        additional_instructions=spec.additional_instructions,
                    ):
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

    async def follow_up(
        self,
        spec: WorkflowSpec,
        message: str,
        *,
        report: str = "",
        history: Sequence[Mapping[str, Any]] = (),
        scope: str = "model",
        note: str = "",
        settings: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Answer one question about a run that already finished.

        The reader stays in the run they were reading: the same workflow
        prompt, the same tool surface, and the report already on screen are
        the context, so a follow-up is a continuation rather than a new run.
        """
        from ifc_console_agents.panel import panel_runtime

        run_settings = validate_settings(spec, settings)
        runtime = panel_runtime(self.core)
        viewer_scope = self._viewer_scope(scope)
        context: dict[str, Any] = {
            "inputs": {},
            "settings": run_settings,
            "steps": {},
            "scope": viewer_scope,
            "run": {"note": note.strip()},
        }
        base = next((step for step in spec.steps if isinstance(step, AgentStep)), None)
        step = AgentStep(
            id="follow-up",
            title="Follow-up",
            prompt=message.strip() or "Explain the result in more detail.",
            agent=base.agent if base else "",
            preset=base.preset if base else "review",
            blocks=base.blocks if base else (),
            role=base.role if base else "",
            max_tool_rounds=base.max_tool_rounds if base else 10,
            max_tool_calls=base.max_tool_calls if base else 40,
        )
        outcome = StepOutcome(
            id=step.id, kind=step.kind, title=step.title, state="succeeded"
        )
        self.core.audit.record(
            "workflow_follow_up", workflow=spec.name, run=self.run_id
        )
        yield {
            "type": "follow_up_started",
            "run_id": self.run_id,
            "workflow": spec.name,
            "question": message.strip(),
        }
        try:
            async for event in self._run_agent(
                step,
                runtime,
                context,
                outcome,
                workflow_prompt=spec.system_prompt,
                additional_instructions=spec.additional_instructions,
                extra_context=_follow_up_context(report, history),
            ):
                yield event
        except (ToolError, WorkflowRunError, ValueError, RuntimeError) as exc:
            outcome.state = "failed"
            outcome.error = str(exc)
            log.info("workflow %s follow-up failed: %s", spec.name, exc)
        outcome.finished_at = _now()
        yield {
            "type": "follow_up_completed",
            "run_id": self.run_id,
            "workflow": spec.name,
            "state": outcome.state,
            "text": outcome.text,
            "error": outcome.error,
        }

    def _viewer_scope(self, mode: str) -> dict[str, Any]:
        return viewer_scope(self.core, mode)

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
        *,
        workflow_prompt: str = "",
        additional_instructions: str = "",
        extra_context: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        from ifc_console_agents.agent import Agent
        from ifc_console_agents.blocks import compose
        from ifc_console_agents.panel import _build_thread, _typed_payloads, panel_limits
        from ifc_console_agents.presets import PRESET_BY_NAME

        if self.model is None:
            raise WorkflowRunError(
                "this step needs a language model; choose a provider and model first"
            )
        role = step.role
        rendered_system = "\n\n".join(
            part
            for part in (
                render(workflow_prompt, context).strip(),
                (
                    "Additional instructions:\n"
                    + render(additional_instructions, context).strip()
                    if additional_instructions.strip()
                    else ""
                ),
                # Never rendered: this carries a finished report, and a report
                # is data, not a template.
                extra_context.strip(),
            )
            if part
        )
        runtime_context = self._runtime_instructions(context)
        if step.agent:
            pack = self.core.agent_packs.get(step.agent)
            if pack is None:
                raise WorkflowRunError(f"unknown agent {step.agent!r}")
            instructions = "\n\n".join(
                part
                for part in (
                    rendered_system,
                    role.strip(),
                    self.instructions.strip(),
                    runtime_context,
                )
                if part
            )
            thread = await _build_thread(
                self.core,
                pack,
                signature=(self.run_id, step.id),
                model=self.model,
                persistent=False,
                instructions=instructions,
                model_label=self.model_label,
                approval_handler=self._approval_handler(),
            )
            agent = thread.agent
            agent.limits = panel_limits(
                self.core,
                AgentLimits(
                    max_tool_rounds=step.max_tool_rounds,
                    max_tool_calls=step.max_tool_calls,
                ),
            )
        else:
            blocks = step.blocks
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
                extra_instructions="\n\n".join(
                    part
                    for part in (
                        rendered_system,
                        self.instructions.strip(),
                        runtime_context,
                    )
                    if part
                ),
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
        yield {
            "type": "agent_started",
            "step_id": step.id,
            "agent": step.agent or step.preset or "workflow agent",
            "model": self.model_label,
            "system_prompt": rendered_system,
            "task_prompt": prompt,
            "runtime_context": runtime_context,
        }
        text_parts: list[str] = []
        completed = False
        usage: dict[str, int | None] = {"in": None, "out": None}
        async for event in agent.stream(prompt, thread_id=f"{self.run_id}-{step.id}"):
            if event.type == "text_delta" and event.text:
                text_parts.append(event.text)
            elif event.type == "run_completed" and event.run_result is not None:
                completed = True
                outcome.text = event.run_result.text
                usage = {
                    "in": event.run_result.usage.input_tokens,
                    "out": event.run_result.usage.output_tokens,
                }
            elif event.type == "run_failed":
                raise WorkflowRunError(event.text or "the agent step failed")
            for payload in _typed_payloads(event):
                yield {**payload, "step_id": step.id}
        if not outcome.text:
            outcome.text = "".join(text_parts).strip()
        if not completed and not outcome.text:
            raise WorkflowRunError("the agent step produced no answer")
        yield {
            "type": "agent_finished",
            "step_id": step.id,
            "agent": step.agent or step.preset or "workflow agent",
            "usage": usage,
            "text": outcome.text,
        }

    @staticmethod
    def _workflow_instructions(
        spec: WorkflowSpec, context: Mapping[str, Any]
    ) -> str:
        return "\n\n".join(
            part
            for part in (
                render(spec.system_prompt, context).strip(),
                (
                    "Additional instructions:\n"
                    + render(spec.additional_instructions, context).strip()
                    if spec.additional_instructions.strip()
                    else ""
                ),
            )
            if part
        )

    @staticmethod
    def _runtime_instructions(context: Mapping[str, Any]) -> str:
        """Stable run context appended to the provider system prompt."""
        scope = str(context.get("scope", {}).get("text") or "").strip()
        settings = context.get("settings") or {}
        note = str(context.get("run", {}).get("note") or "").strip()
        parts = ["Run context (treat these values as instructions for this run):"]
        if scope:
            parts.append(f"Scope:\n{scope}")
        if settings:
            rows = "\n".join(f"- {key}: {value}" for key, value in settings.items())
            parts.append(f"Settings:\n{rows}")
        if note:
            parts.append(f"Additional guidance:\n{note}")
        return "\n\n".join(parts)

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


def viewer_scope(core: Any, mode: str) -> dict[str, Any]:
    """The run scope as the agent reads it: the whole model or exact GlobalIds.

    Shared by the staged runner and the chat route, so a workflow attached to
    a conversation names the same selection the Runs surface would.
    """
    hub = core.viewer_hub
    rows = hub.selection_rows() if hub.connected else []
    selections = [
        {
            "model_id": str(row.get("model_id") or ""),
            "model": str(row.get("model") or row.get("model_id") or "IFC"),
            "guids": [str(item) for item in row.get("guids") or []],
        }
        for row in rows
        if row.get("guids")
    ]
    count = sum(len(row["guids"]) for row in selections)
    if mode == "selection" and not count:
        raise WorkflowRunError(
            "the workflow is scoped to the viewer selection, but nothing is selected"
        )
    if mode != "selection":
        return {
            "mode": "model",
            "count": 0,
            "selections": [],
            "text": "Whole open model. No viewer selection restricts this run.",
        }
    lines = [
        "Use only the elements selected in the 3D viewer as the workflow scope.",
        "Do not broaden the scope unless the task explicitly requires surrounding context.",
    ]
    for row in selections:
        lines.append(
            f"- {row['model']} (model_id={row['model_id']}): " + ", ".join(row["guids"])
        )
    return {
        "mode": "selection",
        "count": count,
        "selections": selections,
        "text": "\n".join(lines),
    }


def _follow_up_context(report: str, history: Sequence[Mapping[str, Any]]) -> str:
    """What the model needs to answer a question about a run it already did."""
    parts = ["You are continuing a workflow run you already carried out."]
    if report.strip():
        parts.append(
            "The report you produced:\n" + report.strip()[:FOLLOW_UP_REPORT_CHARS]
        )
    rows: list[str] = []
    for turn in list(history)[-FOLLOW_UP_TURNS:]:
        question = str(turn.get("question") or "").strip()[:FOLLOW_UP_TURN_CHARS]
        answer = str(turn.get("answer") or "").strip()[:FOLLOW_UP_TURN_CHARS]
        if question:
            rows.append(f"Question: {question}")
        if answer:
            rows.append(f"Your answer: {answer}")
    if rows:
        parts.append("Earlier follow-up turns:\n" + "\n\n".join(rows))
    parts.append(
        "Answer the new question against the open model. Where the answer needs "
        "evidence the report does not already contain, check with tools instead "
        "of restating the report."
    )
    return "\n\n".join(parts)


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


__all__ = [
    "FOLLOW_UP_REPORT_CHARS",
    "FOLLOW_UP_TURNS",
    "GATE_TIMEOUT_S",
    "WorkflowRunError",
    "WorkflowRunner",
    "viewer_scope",
]
