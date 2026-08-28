"""Compose a specialist agent as one namespaced supervisor tool."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ifc_console.toolsets import ToolDefinition

from ifc_console_agents.agent import Agent
from ifc_console_agents.approvals import DenyAllApprovals
from ifc_console_agents.models import AgentEvent, AgentLimits, ApprovalHandler

# A specialist that can hire its own specialist is a budget with no bottom.
MAX_DELEGATION_DEPTH = 1


class AgentToolSource:
    """Expose one bounded specialist agent to another agent's toolset.

    The specialist is streamed, not run blind: every child event is forwarded
    to ``on_event`` with its depth, so a host renders the child's tool calls
    and can answer its approvals. Without a sink nobody could answer one, so
    the child is given a deny-all handler rather than a wait that can only end
    in its own timeout.

    The child's thread is derived from ``parent_thread``, never named by the
    model, so a specialist cannot be pointed at another conversation.
    """

    def __init__(
        self,
        agent: Agent,
        *,
        name: str,
        description: str,
        namespace: str = "agents",
        on_event: Callable[[AgentEvent], Any] | None = None,
        limits: AgentLimits | None = None,
        approval_handler: ApprovalHandler | None = None,
        parent_thread: str = "",
        parent_run_id: str = "",
        depth: int = 0,
    ) -> None:
        if not name.strip() or not description.strip():
            raise ValueError("agent tool name and description must not be empty")
        self.agent = agent
        self.name = name.strip()
        self.description = description.strip()
        self.namespace = namespace
        self.source_id = f"agent:{agent.name}"
        self.on_event = on_event
        self.parent_thread = parent_thread
        self.parent_run_id = parent_run_id
        self.depth = max(0, int(depth))
        if limits is not None:
            # the specialist serves this tool, so the tool's budget is its own
            self.agent.limits = limits
        # An approval nobody can display is an approval nobody can grant.
        self.approval_handler = (
            approval_handler if on_event is not None else DenyAllApprovals()
        )

    async def list_tools(self) -> Sequence[ToolDefinition]:
        return [
            ToolDefinition(
                name=self.name,
                native_name=self.name,
                description=self.description,
                input_schema={
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "The bounded task for the specialist agent.",
                        }
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                tags=frozenset({"agent", "delegation"}),
                source=self.source_id,
            )
        ]

    def _invalid(self, message: str, hint: str) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"code": "INVALID_INPUT", "message": message, "hint": hint},
            "meta": {"tool_source": self.source_id},
        }

    async def _forward(self, event: AgentEvent) -> None:
        if self.on_event is None:
            return
        stamped = event.model_copy(
            update={"depth": self.depth + 1, "parent_run_id": self.parent_run_id or None}
        )
        outcome = self.on_event(stamped)
        if inspect.isawaitable(outcome):
            await outcome

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name != self.name:
            raise KeyError(name)
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return self._invalid(
                "specialist prompt must not be empty",
                "Provide one bounded task in prompt.",
            )
        if self.depth >= MAX_DELEGATION_DEPTH:
            return {
                "ok": False,
                "error": {
                    "code": "DELEGATION_DEPTH",
                    "message": "a delegated agent may not delegate again",
                    "hint": "Do this part of the task yourself.",
                },
                "meta": {"tool_source": self.source_id, "depth": self.depth},
            }
        thread_id = f"{self.parent_thread or 'agent'}::sub::{self.name}"
        result = None
        failure = ""
        async for event in self.agent.stream(
            prompt.strip(),
            thread_id=thread_id,
            approval_handler=self.approval_handler,
        ):
            await self._forward(event)
            if event.type == "run_completed":
                result = event.run_result
            elif event.type == "run_failed":
                failure = event.text or "the specialist run failed"
        if result is None:
            return {
                "ok": False,
                "error": {
                    "code": "DELEGATION_FAILED",
                    "message": failure or "the specialist produced no answer",
                    "hint": "Try a narrower task, or do it yourself.",
                },
                "meta": {"tool_source": self.source_id, "thread_id": thread_id},
            }
        return {
            "ok": True,
            "data": {
                "text": result.text,
                "run_id": result.run_id,
                "thread_id": result.thread_id,
                "stopped_reason": result.stopped_reason,
                "tool_calls": [
                    {
                        "name": call.name,
                        "ok": call.ok,
                        "summary": call.summary,
                    }
                    for call in result.tool_calls
                ],
                "usage": result.usage.model_dump(mode="json"),
            },
            "meta": {"tool_source": self.source_id, "depth": self.depth + 1},
        }

    async def aclose(self) -> None:
        return None


__all__ = ["MAX_DELEGATION_DEPTH", "AgentToolSource"]
