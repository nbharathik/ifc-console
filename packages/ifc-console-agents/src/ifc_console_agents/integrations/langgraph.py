"""LangGraph orchestration behind IFC Console's agent event contract.

The built-in agent loop remains the default. Applications can opt into this
adapter for checkpointed, multi-stage workflows while continuing to expose
``AgentEvent`` objects to their host UI.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from importlib import import_module
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator
from typing_extensions import TypedDict

from ifc_console_agents.models import (
    AgentEvent,
    AgentImage,
    AgentMessage,
    AgentRunResult,
    AgentToolCallRecord,
    AgentUsage,
    ApprovalDecision,
    ApprovalRequest,
)

GRAPH_EVENTS_KEY = "ifc_agent_events"
GRAPH_FINAL_TEXT_KEY = "ifc_final_text"
GRAPH_ERROR_KEY = "ifc_error"

_GRAPH_EVENT_TYPES = Literal[
    "text_delta",
    "reasoning_delta",
    "tool_call_started",
    "approval_requested",
    "approval_resolved",
    "tool_call_finished",
    "usage",
]


class LangGraphUnavailable(ImportError):
    """A required graph dependency is not installed in this environment."""


class GraphWorkflowError(RuntimeError):
    """A graph emitted an invalid IFC Console workflow update."""


class GraphWorkflowState(TypedDict, total=False):
    """Small JSON-safe state shared by the adapter and application graph nodes."""

    prompt: str
    thread_id: str
    data: dict[str, Any]
    ifc_agent_events: list[dict[str, Any]]
    ifc_final_text: str
    ifc_error: str | None


class GraphAgentEvent(BaseModel):
    """One graph-node event before run and thread identity are attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: _GRAPH_EVENT_TYPES
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    approval: ApprovalRequest | None = None
    decision: ApprovalDecision | None = None
    usage: AgentUsage | None = None

    @model_validator(mode="after")
    def complete_event(self) -> GraphAgentEvent:
        if self.type in {"text_delta", "reasoning_delta"} and self.text is None:
            raise ValueError(f"{self.type} requires text")
        if self.type.startswith("tool_call_") and (not self.tool_call_id or not self.tool_name):
            raise ValueError(f"{self.type} requires tool_call_id and tool_name")
        if self.type == "approval_requested" and self.approval is None:
            raise ValueError("approval_requested requires approval")
        if self.type == "approval_resolved" and self.decision is None:
            raise ValueError("approval_resolved requires decision")
        if self.type == "usage" and self.usage is None:
            raise ValueError("usage requires usage")
        return self


class _CompiledGraph(Protocol):
    def astream(
        self,
        input: Any,
        config: Mapping[str, Any],
        *,
        stream_mode: str,
    ) -> AsyncIterator[Any]: ...


GraphBuilder = Callable[[Any, Any, Any], Any]
CommandFactory = Callable[[Any], Any]


def _json_copy(value: Any, *, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be JSON-serializable") from exc


def graph_update(
    *,
    events: Sequence[GraphAgentEvent | Mapping[str, Any]] = (),
    final_text: str | None = None,
    error: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> GraphWorkflowState:
    """Build a validated update that a LangGraph node can return."""
    if final_text is not None and error is not None:
        raise ValueError("a graph update cannot be both final and failed")
    update: GraphWorkflowState = {}
    if events:
        payloads = [
            GraphAgentEvent.model_validate(event).model_dump(mode="json", exclude_none=True)
            for event in events
        ]
        update[GRAPH_EVENTS_KEY] = _json_copy(payloads, label="graph events")
    if final_text is not None:
        update[GRAPH_FINAL_TEXT_KEY] = str(final_text)
    if error is not None:
        update[GRAPH_ERROR_KEY] = str(error)
    if data is not None:
        copied = _json_copy(dict(data), label="graph data")
        update["data"] = copied
    return update


def graph_approval_interrupt(request: ApprovalRequest) -> dict[str, Any]:
    """Create the JSON payload passed to ``langgraph.types.interrupt``.

    LangGraph requires Python 3.11 or newer to propagate the context used by
    ``interrupt`` through asynchronous graph execution.
    """
    event = GraphAgentEvent(type="approval_requested", approval=request)
    return event.model_dump(mode="json", exclude_none=True)


def _load_langgraph() -> tuple[Any, Any, Any, CommandFactory]:
    try:
        graph_module = import_module("langgraph.graph")
        types_module = import_module("langgraph.types")
    except ImportError:
        raise LangGraphUnavailable(
            "LangGraph ships with ifc-console-agents, but is missing from this "
            "environment. Reinstall or upgrade ifc-console-agents."
        ) from None
    return (
        graph_module.StateGraph,
        graph_module.START,
        graph_module.END,
        lambda value: types_module.Command(resume=value),
    )


def create_langgraph_workflow(
    configure: GraphBuilder,
    *,
    checkpointer: Any = None,
    name: str = "ifc-workflow",
    configuration_digest: str = "",
) -> LangGraphWorkflow:
    """Build an opt-in graph over the shared IFC workflow state.

    ``configure`` receives ``(builder, START, END)`` and adds nodes, normal or
    conditional edges, and subgraphs. The supplied checkpointer remains owned
    by the caller so local SQLite and hosted database lifecycles stay explicit.
    """
    if not name.strip():
        raise ValueError("graph workflow name must not be empty")
    StateGraph, start, end, command_factory = _load_langgraph()
    builder = StateGraph(GraphWorkflowState)
    configured = configure(builder, start, end)
    if inspect.isawaitable(configured):
        raise TypeError("configure must be a synchronous graph builder")
    if configured is not None:
        builder = configured
    if not callable(getattr(builder, "compile", None)):
        raise TypeError("configure must return a graph builder or None")
    graph = builder.compile(checkpointer=checkpointer, name=name.strip())
    return LangGraphWorkflow(
        graph,
        name=name.strip(),
        configuration_digest=configuration_digest,
        checkpointer=checkpointer,
        command_factory=command_factory,
    )


def _updates(chunk: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(chunk, Mapping):
        raise GraphWorkflowError("LangGraph emitted a non-object update")
    reserved = {GRAPH_EVENTS_KEY, GRAPH_FINAL_TEXT_KEY, GRAPH_ERROR_KEY, "__interrupt__"}
    if reserved.intersection(chunk):
        return (chunk,)
    updates = tuple(value for value in chunk.values() if isinstance(value, Mapping))
    if len(updates) != len(chunk):
        raise GraphWorkflowError("LangGraph emitted an invalid node update")
    return updates


def _interrupt_values(value: Any) -> tuple[Any, ...]:
    values = value if isinstance(value, (list, tuple)) else (value,)
    return tuple(getattr(item, "value", item) for item in values)


def _summary(result: Mapping[str, Any]) -> str:
    if result.get("ok"):
        return "ok"
    error = result.get("error")
    if isinstance(error, Mapping):
        return str(error.get("code") or "failed")
    return "failed"


def _add_usage(total: AgentUsage, current: AgentUsage) -> AgentUsage:
    def add(left: int | None, right: int | None) -> int | None:
        if left is None and right is None:
            return None
        return (left or 0) + (right or 0)

    return AgentUsage(
        input_tokens=add(total.input_tokens, current.input_tokens),
        output_tokens=add(total.output_tokens, current.output_tokens),
    )


class LangGraphWorkflow:
    """Adapt one compiled LangGraph to the existing ``AgentEvent`` boundary."""

    def __init__(
        self,
        graph: _CompiledGraph,
        *,
        name: str,
        configuration_digest: str = "",
        checkpointer: Any = None,
        command_factory: CommandFactory | None = None,
    ) -> None:
        if not callable(getattr(graph, "astream", None)):
            raise TypeError("compiled graph must provide astream")
        self.graph = graph
        self.name = name
        self.configuration_digest = configuration_digest
        self.checkpointer = checkpointer
        self._command_factory = command_factory
        self._thread_locks: dict[str, asyncio.Lock] = {}
        self._thread_lock_users: dict[str, int] = {}

    async def stream(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        images: Sequence[AgentImage | Mapping[str, str]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Start a graph run using the same call shape as the built-in agent."""
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        if images:
            raise ValueError("graph checkpoints accept artifact references, not inline image bytes")
        selected = thread_id or f"graph-{uuid.uuid4().hex}"
        state: GraphWorkflowState = {
            "prompt": prompt.strip(),
            "thread_id": selected,
            "data": {},
        }
        async for event in self._locked_run(state, thread_id=selected, prompt=prompt.strip()):
            yield event

    async def resume(
        self,
        value: Any,
        *,
        thread_id: str,
    ) -> AsyncIterator[AgentEvent]:
        """Resume a checkpointed interrupt on the same graph thread."""
        safe_value = _json_copy(value, label="resume value")
        command_factory = self._command_factory
        if command_factory is None:
            *_unused, command_factory = _load_langgraph()
        command = command_factory(safe_value)
        async for event in self._locked_run(command, thread_id=thread_id, prompt=""):
            yield event

    async def delete_thread(self, thread_id: str) -> bool:
        """Delete graph checkpoints when the caller-owned saver supports it."""
        if self.checkpointer is None:
            return False
        delete = getattr(self.checkpointer, "adelete_thread", None)
        if delete is None:
            delete = getattr(self.checkpointer, "delete_thread", None)
        if delete is None:
            return False
        result = delete(thread_id)
        if inspect.isawaitable(result):
            await result
        return True

    async def _locked_run(
        self,
        graph_input: Any,
        *,
        thread_id: str,
        prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        lock = self._thread_locks.setdefault(thread_id, asyncio.Lock())
        self._thread_lock_users[thread_id] = self._thread_lock_users.get(thread_id, 0) + 1
        try:
            async with lock:
                async for event in self._run(graph_input, thread_id=thread_id, prompt=prompt):
                    yield event
        finally:
            users = self._thread_lock_users[thread_id] - 1
            if users:
                self._thread_lock_users[thread_id] = users
            else:
                self._thread_lock_users.pop(thread_id, None)
                if self._thread_locks.get(thread_id) is lock:
                    self._thread_locks.pop(thread_id, None)

    async def _run(
        self,
        graph_input: Any,
        *,
        thread_id: str,
        prompt: str,
    ) -> AsyncIterator[AgentEvent]:
        run_id = f"run_{uuid.uuid4().hex}"
        text_parts: list[str] = []
        final_text: str | None = None
        failure: str | None = None
        paused = False
        records: list[AgentToolCallRecord] = []
        usage = AgentUsage()
        yield AgentEvent(type="run_started", run_id=run_id, thread_id=thread_id)
        config = {
            "configurable": {"thread_id": thread_id},
            "metadata": {
                "ifc_console_workflow": self.name,
                "configuration_digest": self.configuration_digest,
            },
        }
        stream = self.graph.astream(graph_input, config, stream_mode="updates")
        try:
            async for chunk in stream:
                for update in _updates(chunk):
                    raw_events = update.get(GRAPH_EVENTS_KEY, ())
                    if not isinstance(raw_events, (list, tuple)):
                        raise GraphWorkflowError(f"{GRAPH_EVENTS_KEY} must be a list")
                    for raw_event in raw_events:
                        event = GraphAgentEvent.model_validate(raw_event)
                        attached = AgentEvent(
                            **event.model_dump(mode="python", exclude_none=True),
                            run_id=run_id,
                            thread_id=thread_id,
                        )
                        if event.type == "text_delta":
                            text_parts.append(event.text or "")
                        elif event.type == "usage" and event.usage is not None:
                            usage = _add_usage(usage, event.usage)
                        elif event.type == "tool_call_finished":
                            result = event.result or {}
                            records.append(
                                AgentToolCallRecord(
                                    id=event.tool_call_id or "",
                                    name=event.tool_name or "",
                                    arguments=event.arguments or {},
                                    ok=bool(result.get("ok")),
                                    summary=_summary(result),
                                    result=result,
                                )
                            )
                        yield attached
                    if GRAPH_FINAL_TEXT_KEY in update:
                        value = update[GRAPH_FINAL_TEXT_KEY]
                        if not isinstance(value, str):
                            raise GraphWorkflowError(f"{GRAPH_FINAL_TEXT_KEY} must be text")
                        final_text = value
                    if GRAPH_ERROR_KEY in update:
                        value = update[GRAPH_ERROR_KEY]
                        if value is not None and not isinstance(value, str):
                            raise GraphWorkflowError(f"{GRAPH_ERROR_KEY} must be text or null")
                        failure = value
                    if "__interrupt__" in update:
                        for value in _interrupt_values(update["__interrupt__"]):
                            event = GraphAgentEvent.model_validate(value)
                            if event.type != "approval_requested":
                                raise GraphWorkflowError(
                                    "graph interrupts must carry approval_requested events"
                                )
                            paused = True
                            yield AgentEvent(
                                **event.model_dump(mode="python", exclude_none=True),
                                run_id=run_id,
                                thread_id=thread_id,
                            )
        except asyncio.CancelledError:
            raise
        except GraphWorkflowError as exc:
            failure = str(exc)
        except Exception as exc:
            failure = f"graph workflow failed with {type(exc).__name__}"
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        if paused:
            return
        if failure is not None:
            yield AgentEvent(
                type="run_failed",
                run_id=run_id,
                thread_id=thread_id,
                text=failure,
            )
            return
        answer = final_text if final_text is not None else "".join(text_parts).strip()
        messages: list[AgentMessage] = []
        if prompt:
            messages.append(AgentMessage(role="user", text=prompt))
        messages.append(AgentMessage(role="assistant", text=answer))
        result = AgentRunResult(
            run_id=run_id,
            thread_id=thread_id,
            text=answer,
            messages=tuple(messages),
            tool_calls=tuple(records),
            usage=usage,
        )
        yield AgentEvent(
            type="run_completed",
            run_id=run_id,
            thread_id=thread_id,
            run_result=result,
        )


__all__ = [
    "GRAPH_ERROR_KEY",
    "GRAPH_EVENTS_KEY",
    "GRAPH_FINAL_TEXT_KEY",
    "GraphAgentEvent",
    "GraphWorkflowError",
    "GraphWorkflowState",
    "LangGraphUnavailable",
    "LangGraphWorkflow",
    "create_langgraph_workflow",
    "graph_approval_interrupt",
    "graph_update",
]
