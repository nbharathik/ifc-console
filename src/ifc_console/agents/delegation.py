"""Compose a specialist agent as one namespaced supervisor tool."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ifc_console.agents.agent import Agent
from ifc_console.toolsets import ToolDefinition


class AgentToolSource:
    """Expose one bounded specialist agent to another agent's toolset."""

    def __init__(
        self,
        agent: Agent,
        *,
        name: str,
        description: str,
        namespace: str = "agents",
    ) -> None:
        if not name.strip() or not description.strip():
            raise ValueError("agent tool name and description must not be empty")
        self.agent = agent
        self.name = name.strip()
        self.description = description.strip()
        self.namespace = namespace
        self.source_id = f"agent:{agent.name}"

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
                        },
                        "thread_id": {
                            "type": ["string", "null"],
                            "description": "Optional specialist conversation thread to continue.",
                            "default": None,
                        },
                    },
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                tags=frozenset({"agent", "delegation"}),
                source=self.source_id,
            )
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name != self.name:
            raise KeyError(name)
        prompt = arguments.get("prompt")
        thread_id = arguments.get("thread_id")
        if not isinstance(prompt, str) or not prompt.strip():
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "specialist prompt must not be empty",
                    "hint": "Provide one bounded task in prompt.",
                },
                "meta": {"tool_source": self.source_id},
            }
        if thread_id is not None and not isinstance(thread_id, str):
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "thread_id must be a string or null",
                    "hint": "Omit thread_id to create a fresh specialist thread.",
                },
                "meta": {"tool_source": self.source_id},
            }
        result = await self.agent.run(prompt.strip(), thread_id=thread_id)
        return {
            "ok": True,
            "data": {
                "text": result.text,
                "run_id": result.run_id,
                "thread_id": result.thread_id,
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
            "meta": {"tool_source": self.source_id},
        }

    async def aclose(self) -> None:
        return None


__all__ = ["AgentToolSource"]
