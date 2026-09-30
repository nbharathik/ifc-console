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
    "tool_progress",
    "approval_requested",
    "approval_resolved",
    "tool_call_finished",
    "usage",
    "run_completed",
    "run_failed",
]
# why a run stopped calling tools while it still had more to do
AgentStopReason = Literal["tool_budget", "round_budget"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AgentImage(BaseModel):
    """One image a message carries, as base64 without a data: prefix."""

    model_config = ConfigDict(frozen=True)

    media_type: str = Field(pattern=r"^image/(png|jpeg|gif|webp)$")
    data: str = Field(min_length=1)

    @classmethod
    def from_file(cls, path: str | Any) -> AgentImage:
        """Load a png, jpg, gif, or webp file from disk."""
        import base64
        from pathlib import Path

        target = Path(path)
        suffix = target.suffix.lower().lstrip(".")
        media = {"jpg": "jpeg"}.get(suffix, suffix)
        return cls(
            media_type=f"image/{media}",
            data=base64.b64encode(target.read_bytes()).decode("ascii"),
        )


class AgentMessage(BaseModel):
    """Provider-neutral conversation message, including tool round state."""

    model_config = ConfigDict(frozen=True)

    role: AgentRole
    text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    # vision input; providers that cannot carry images on a role drop them
    images: tuple[AgentImage, ...] = ()


class AgentLimits(BaseModel):
    """Hard limits for one run."""

    model_config = ConfigDict(frozen=True)

    max_tool_rounds: int = Field(default=8, ge=1, le=100)
    max_tool_calls: int = Field(default=32, ge=0, le=1000)
    timeout_s: float = Field(default=600.0, gt=0, le=86_400)
    # A human reading an approval is not the run working, so the wait has its
    # own clock and the run deadline is credited back afterwards.
    approval_timeout_s: float = Field(default=3600.0, gt=0, le=86_400)
    max_tool_result_chars: int = Field(default=12_000, ge=512, le=1_000_000)
    # rounds where every requested call is read-only run concurrently
    parallel_read_only: bool = True


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


class AgentProgress(BaseModel):
    """How far a still-running tool call has got.

    A ten-second tool is indistinguishable from a hang unless it says so, and
    ``elapsed_s`` alone already tells the reader the run is alive.
    """

    model_config = ConfigDict(frozen=True)

    done: int = Field(default=0, ge=0)
    total: int | None = Field(default=None, ge=0)
    note: str = ""
    elapsed_s: float = Field(default=0.0, ge=0)


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
    # set when the answer came from the wrap-up round rather than the model
    # choosing to stop calling tools
    stopped_reason: AgentStopReason | None = None
    # the validated response_model instance when Agent.run asked for one
    data: Any = None


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
    progress: AgentProgress | None = None
    usage: AgentUsage | None = None
    run_result: AgentRunResult | None = None
    # 0 for the run the caller started, 1 for a delegated specialist's own
    # events forwarded through it, and so on.
    depth: int = Field(default=0, ge=0)
    parent_run_id: str | None = None
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
    "AgentProgress",
    "AgentRole",
    "AgentRunResult",
    "AgentStopReason",
    "AgentToolCallRecord",
    "AgentUsage",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalRequest",
    "ThreadStore",
]
