"""Test doubles for agent applications: run and assert without an LLM key.

ScriptedAgentModel plays back provider events round by round, so a test can
drive the bundled Agent through tool calls and answers deterministically.
RecordingThreadStore remembers every save. The envelope builders produce the
same {ok, data/error, meta} shape real tools return.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ifc_console.agents.models import AgentMessage
from ifc_console.agents.storage import InMemoryThreadStore
from ifc_console.toolsets import ToolDefinition


def ok_envelope(data: Mapping[str, Any] | None = None, **meta: Any) -> dict[str, Any]:
    """A successful tool envelope, as Toolset.call would return it."""
    return {"ok": True, "data": dict(data or {}), "meta": dict(meta)}


def error_envelope(code: str, message: str, hint: str = "", **meta: Any) -> dict[str, Any]:
    """A failed tool envelope with the standard error fields."""
    return {
        "ok": False,
        "error": {"code": code, "message": message, "hint": hint},
        "meta": dict(meta),
    }


def text_round(text: str) -> list[dict[str, Any]]:
    """One provider round that answers with plain text."""
    return [{"type": "content", "text": text}]


def tool_call_round(
    *calls: Mapping[str, Any], text: str = ""
) -> list[dict[str, Any]]:
    """One provider round that requests tool calls.

    Each call is {"name": ..., "arguments": {...} or JSON string, "id": ...};
    missing ids are filled in.
    """
    prepared = []
    for index, call in enumerate(calls):
        item = dict(call)
        item.setdefault("id", f"call-{index + 1}")
        item.setdefault("arguments", "{}")
        prepared.append(item)
    events: list[dict[str, Any]] = []
    if text:
        events.append({"type": "content", "text": text})
    events.append({"type": "tool_calls", "calls": prepared})
    return events


class ScriptedAgentModel:
    """An AgentModel that plays back pre-written provider rounds.

    Each round is a list of provider events ({"type": "content"|"tool_calls"|
    "reasoning"|"usage"|"error", ...}); text_round and tool_call_round build
    the common ones. One round is consumed per model turn. Running past the
    script yields an error event, which fails the run visibly.
    """

    provider_id = "test"
    model_id = "scripted"

    def __init__(self, rounds: Sequence[Sequence[Mapping[str, Any]]]) -> None:
        self._rounds = list(rounds)
        self.turns: list[dict[str, Any]] = []

    async def stream(
        self,
        *,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
        system: str,
        options: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        self.turns.append(
            {
                "messages": list(messages),
                "tools": [tool.name for tool in tools],
                "system": system,
                "options": dict(options),
            }
        )
        if not self._rounds:
            yield {"type": "error", "text": "scripted model ran out of rounds"}
            return
        for event in self._rounds.pop(0):
            yield event


class RecordingThreadStore(InMemoryThreadStore):
    """An in-memory thread store that remembers every save for assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.saves: list[tuple[str, int]] = []

    async def save(self, thread_id: str, messages: Sequence[AgentMessage]) -> None:
        await super().save(thread_id, messages)
        self.saves.append((thread_id, len(messages)))


__all__ = [
    "RecordingThreadStore",
    "ScriptedAgentModel",
    "error_envelope",
    "ok_envelope",
    "text_round",
    "tool_call_round",
]
