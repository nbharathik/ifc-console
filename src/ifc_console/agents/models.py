"""Stable data contracts for the composable agent runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.toolsets import ToolDefinition

AgentRole = Literal["user", "assistant", "tool"]
AgentEventType = Literal[
    "run_started",
    "text_delta",
    "reasoning_delta",
    "tool_call_started",
    "approval_requested",
    "approval_resolved",
    "tool_call_finished",
    "usage",
    "run_completed",
    "run_failed",
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentMessage(BaseModel):
    """Provider-neutral conversation message, including tool round state."""

    model_config = ConfigDict(frozen=True)

    role: AgentRole
    text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    name: str | None = None


class AgentLimits(BaseModel):
    """Hard limits for one run."""

    model_config = ConfigDict(frozen=True)

    max_tool_rounds: int = Field(default=8, ge=1, le=100)
    max_tool_calls: int = Field(default=32, ge=0, le=1000)
    timeout_s: float = Field(default=600.0, gt=0, le=86_400)
    max_tool_result_chars: int = Field(default=12_000, ge=512, le=1_000_000)


class ApprovalRequest(BaseModel):
    """A caller-owned decision requested before a protected tool executes."""

    model_config = ConfigDict(frozen=True)

    request_id: str
    run_id: str
    thread_id: str
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    required_capabilities: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utc_now)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    approved: bool
    decided_by: str = "application"
    reason: str = ""


class AgentToolCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any]
    ok: bool
    summary: str
    result: dict[str, Any]


class AgentUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None


class AgentRunResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    thread_id: str
    text: str
    messages: tuple[AgentMessage, ...]
    tool_calls: tuple[AgentToolCallRecord, ...] = ()
    usage: AgentUsage = Field(default_factory=AgentUsage)
    error: str | None = None


class AgentEvent(BaseModel):
    """One discriminated streaming event emitted by an agent run."""

    model_config = ConfigDict(frozen=True)

    type: AgentEventType
    run_id: str
    thread_id: str
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    approval: ApprovalRequest | None = None
    decision: ApprovalDecision | None = None
    usage: AgentUsage | None = None
    run_result: AgentRunResult | None = None
    created_at: datetime = Field(default_factory=utc_now)


@runtime_checkable
class AgentModel(Protocol):
    """Any LLM adapter capable of one streamed tool-use round."""

    provider_id: str
    model_id: str

    def stream(
        self,
        *,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
        system: str,
        options: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]: ...


@runtime_checkable
class ThreadStore(Protocol):
    async def load(self, thread_id: str) -> Sequence[AgentMessage]: ...

    async def save(self, thread_id: str, messages: Sequence[AgentMessage]) -> None: ...

    async def delete(self, thread_id: str) -> bool: ...


@runtime_checkable
class ApprovalHandler(Protocol):
    async def request(self, request: ApprovalRequest) -> ApprovalDecision: ...


__all__ = [
    "AgentEvent",
    "AgentEventType",
    "AgentLimits",
    "AgentMessage",
    "AgentModel",
    "AgentRole",
    "AgentRunResult",
    "AgentToolCallRecord",
    "AgentUsage",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalRequest",
    "ThreadStore",
]
