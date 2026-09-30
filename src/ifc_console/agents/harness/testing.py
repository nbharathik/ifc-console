"""A scripted ACP agent: the engine the tests and `ifc-console dev` spawn.

Run as ``python -m ifc_console.agents.harness.testing``. It streams a text
chunk, a tool call it asks permission for, and a closing line, and returns
usage. With IFC_CONSOLE_FAKE_ACP_MCP=1 the tool call really goes through the
MCP server the client injected, which is how the bridge path is exercised.
Words in the prompt steer it: ``sleep`` waits for a cancel, ``refuse`` ends
with a refusal, ``bash`` asks to run a shell command, ``plan`` sends a plan.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from acp import PROTOCOL_VERSION, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AuthenticateResponse,
    ContentToolCallContent,
    Implementation,
    InitializeResponse,
    McpCapabilities,
    NewSessionResponse,
    PermissionOption,
    PlanEntry,
    PromptCapabilities,
    PromptResponse,
    SessionMode,
    SessionModeState,
    SetSessionModeResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    Usage,
)

ENGINE_NAME = "rehearsal-acp"
MCP_ENV = "IFC_CONSOLE_FAKE_ACP_MCP"
TOOL_TITLE = "ifc-console_get_session_status"
_OPTIONS = [
    PermissionOption(option_id="once", name="Allow once", kind="allow_once"),
    PermissionOption(option_id="always", name="Always allow", kind="allow_always"),
    PermissionOption(option_id="reject", name="Reject", kind="reject_once"),
]


def _log(message: str) -> None:
    print(f"[fake-acp] {message}", file=sys.stderr, flush=True)


class FakeAcpAgent:
    def __init__(self) -> None:
        self.conn: Any = None
        self.servers: dict[str, list[Any]] = {}
        self.cancelled: set[str] = set()
        self.counter = 0

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(self, protocol_version: int, **_kwargs: Any) -> InitializeResponse:
        del protocol_version
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                mcp_capabilities=McpCapabilities(http=True),
                prompt_capabilities=PromptCapabilities(image=True),
            ),
            agent_info=Implementation(name=ENGINE_NAME, version="1"),
        )

    async def authenticate(self, method_id: str, **_kwargs: Any) -> AuthenticateResponse:
        del method_id
        return AuthenticateResponse()

    async def new_session(
        self, cwd: str, mcp_servers: list[Any] | None = None, **_kwargs: Any
    ) -> NewSessionResponse:
        self.counter += 1
        session_id = f"fake-{self.counter}"
        self.servers[session_id] = list(mcp_servers or [])
        _log(f"session {session_id} cwd={cwd} servers={len(self.servers[session_id])}")
        return NewSessionResponse(
            session_id=session_id,
            modes=SessionModeState(
                current_mode_id="fake",
                available_modes=[
                    SessionMode(id="fake", name="Fake"),
                    SessionMode(id="ifc", name="IFC"),
                ],
            ),
        )

    async def set_session_mode(self, session_id: str, mode_id: str, **_kwargs: Any) -> Any:
        _log(f"session {session_id} mode={mode_id}")
        return SetSessionModeResponse()

    async def cancel(self, session_id: str, **_kwargs: Any) -> None:
        self.cancelled.add(session_id)

    async def _say(self, session_id: str, text: str) -> None:
        await self.conn.session_update(
            session_id=session_id,
            update=AgentMessageChunk(
                session_update="agent_message_chunk",
                content=TextContentBlock(type="text", text=text),
            ),
        )

    async def prompt(self, session_id: str, prompt: list[Any], **_kwargs: Any) -> PromptResponse:
        self.cancelled.discard(session_id)
        text = " ".join(getattr(block, "text", "") or "" for block in prompt)
        images = sum(1 for block in prompt if getattr(block, "type", "") == "image")
        words = text.lower()
        usage = Usage(total_tokens=3, input_tokens=2, output_tokens=1)

        if "sleep" in words:
            await self._say(session_id, "sleeping until cancelled ")
            while session_id not in self.cancelled:
                await asyncio.sleep(0.05)
            return PromptResponse(stop_reason="cancelled")
        if "refuse" in words:
            return PromptResponse(stop_reason="refusal")
        if "plan" in words:
            await self.conn.session_update(
                session_id=session_id,
                update=AgentPlanUpdate(
                    session_update="plan",
                    entries=[
                        PlanEntry(content="look at the model", priority="high", status="completed"),
                        PlanEntry(content="answer", priority="medium", status="in_progress"),
                    ],
                ),
            )
        await self.conn.session_update(
            session_id=session_id,
            update=AgentThoughtChunk(
                session_update="agent_thought_chunk",
                content=TextContentBlock(type="text", text="thinking about the session"),
            ),
        )
        await self._say(session_id, "checking the session ")

        title = "bash" if "bash" in words else TOOL_TITLE
        kind = "execute" if "bash" in words else "other"
        raw_input = {"command": "ls"} if "bash" in words else {}
        call_id = f"call-{self.counter}-{len(self.cancelled) + 1}"
        await self.conn.session_update(
            session_id=session_id,
            update=ToolCallStart(
                session_update="tool_call",
                tool_call_id=call_id,
                title=title,
                kind=kind,
                status="pending",
                raw_input=raw_input,
            ),
        )
        allowed = True
        if "noperm" not in words:
            response = await self.conn.request_permission(
                session_id=session_id,
                tool_call=ToolCallUpdate(tool_call_id=call_id, title=title, raw_input=raw_input),
                options=list(_OPTIONS),
            )
            outcome = response.outcome
            allowed = getattr(outcome, "outcome", "") == "selected" and getattr(
                outcome, "option_id", ""
            ) in ("once", "always")
        if session_id in self.cancelled:
            return PromptResponse(stop_reason="cancelled")
        if not allowed:
            await self.conn.session_update(
                session_id=session_id,
                update=ToolCallProgress(
                    session_update="tool_call_update",
                    tool_call_id=call_id,
                    status="failed",
                    content=[
                        ContentToolCallContent(
                            type="content",
                            content=TextContentBlock(type="text", text="permission denied"),
                        )
                    ],
                ),
            )
            await self._say(session_id, "the tool was denied.")
            return PromptResponse(stop_reason="end_turn", usage=usage)

        raw_output: Any
        if title == "bash":
            raw_output = "file-a\nfile-b"
        else:
            raw_output = await self._call_mcp(session_id)
        await self.conn.session_update(
            session_id=session_id,
            update=ToolCallProgress(
                session_update="tool_call_update",
                tool_call_id=call_id,
                status="completed",
                raw_output=raw_output,
            ),
        )
        first = text.strip().splitlines()[0] if text.strip() else ""
        note = f" with {images} image(s)" if images else ""
        await self._say(session_id, f"done{note}. echo: {first[:80]}")
        return PromptResponse(stop_reason="end_turn", usage=usage)

    async def _call_mcp(self, session_id: str) -> Any:
        servers = self.servers.get(session_id) or []
        if os.environ.get(MCP_ENV, "").strip() not in ("1", "true", "yes") or not servers:
            return {"ok": True, "data": {"fake": True, "servers": len(servers)}, "meta": {}}
        server = servers[0]
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            from mcp.client.streamable_http import streamablehttp_client

            if getattr(server, "type", "") == "http":
                headers = {h.name: h.value for h in getattr(server, "headers", None) or []}
                async with (
                    streamablehttp_client(server.url, headers=headers) as (read, write, _),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool("get_session_status", {})
            else:
                params = StdioServerParameters(
                    command=server.command,
                    args=list(server.args),
                    env={item.name: item.value for item in getattr(server, "env", None) or []},
                )
                async with (
                    stdio_client(params, errlog=sys.stderr) as (read, write),
                    ClientSession(read, write) as session,
                ):
                    await session.initialize()
                    result = await session.call_tool("get_session_status", {})
        except Exception as exc:
            _log(f"mcp call failed: {exc!r}")
            return {
                "ok": False,
                "error": {"code": "FAKE_MCP_FAILED", "message": repr(exc)[:300], "hint": ""},
                "meta": {},
            }
        structured = getattr(result, "structuredContent", None)
        if isinstance(structured, dict) and "ok" in structured:
            return structured
        for item in getattr(result, "content", None) or []:
            text = getattr(item, "text", None)
            if text:
                try:
                    return json.loads(text)
                except ValueError:
                    return {"ok": True, "data": {"output": text}, "meta": {}}
        return {"ok": True, "data": {"output": ""}, "meta": {}}


def main() -> None:
    asyncio.run(run_agent(FakeAcpAgent()))


if __name__ == "__main__":
    main()
