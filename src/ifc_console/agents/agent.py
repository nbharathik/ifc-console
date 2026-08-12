"""Composable, bounded agent loop over IFC, Python, and MCP tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ifc_console.agents.approvals import DenyAllApprovals
from ifc_console.agents.models import (
    AgentEvent,
    AgentLimits,
    AgentMessage,
    AgentModel,
    AgentRunResult,
    AgentToolCallRecord,
    AgentUsage,
    ApprovalHandler,
    ApprovalRequest,
    ThreadStore,
)
from ifc_console.agents.storage import InMemoryThreadStore
from ifc_console.toolsets import ToolCall, ToolMiddleware, Toolset


class AgentRunError(RuntimeError):
    """A run ended without a valid assistant result."""


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _summary(result: Mapping[str, Any]) -> str:
    if result.get("ok"):
        meta = result.get("meta") or {}
        returned = meta.get("returned") if isinstance(meta, Mapping) else None
        return f"{returned} row(s)" if returned is not None else "ok"
    error = result.get("error") or {}
    return str(error.get("code") or "failed") if isinstance(error, Mapping) else "failed"


def _tool_text(result: Mapping[str, Any], limit: int) -> str:
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) <= limit:
        return text
    suffix = "...[truncated; narrow the request]"
    return text[: max(0, limit - len(suffix))] + suffix


class Agent:
    """A provider-neutral agent with scoped tools and host-owned approvals."""

    def __init__(
        self,
        *,
        name: str,
        model: AgentModel,
        tools: Toolset,
        instructions: str,
        thread_store: ThreadStore | None = None,
        approval_handler: ApprovalHandler | None = None,
        limits: AgentLimits | None = None,
        middleware: Sequence[ToolMiddleware] = (),
        model_options: Mapping[str, Any] | None = None,
    ) -> None:
        if not name.strip():
            raise ValueError("agent name must not be empty")
        self.name = name.strip()
        self.model = model
        self.tools = tools
        self.instructions = instructions.strip()
        self.thread_store = thread_store or InMemoryThreadStore()
        self.approval_handler = approval_handler or DenyAllApprovals()
        self.limits = limits or AgentLimits()
        self.middleware = tuple(middleware)
        self.model_options = dict(model_options or {})
        self._thread_locks: dict[str, asyncio.Lock] = {}

    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> AgentRunResult:
        completed: AgentRunResult | None = None
        failure: str | None = None
        async for event in self.stream(prompt, thread_id=thread_id, options=options):
            if event.type == "run_completed":
                completed = event.run_result
            elif event.type == "run_failed":
                failure = event.text or "agent run failed"
        if completed is None:
            raise AgentRunError(failure or "agent run did not complete")
        return completed

    async def stream(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        selected_thread = thread_id or _id("thread")
        lock = self._thread_locks.setdefault(selected_thread, asyncio.Lock())
        async with lock:
            async for event in self._stream_locked(
                prompt.strip(),
                thread_id=selected_thread,
                options=dict(options or {}),
            ):
                yield event

    async def _stream_locked(
        self,
        prompt: str,
        *,
        thread_id: str,
        options: Mapping[str, Any],
    ) -> AsyncIterator[AgentEvent]:
        run_id = _id("run")
        deadline = time.monotonic() + self.limits.timeout_s
        history = list(await self.thread_store.load(thread_id))
        history.append(AgentMessage(role="user", text=prompt))
        await self.thread_store.save(thread_id, history)
        records: list[AgentToolCallRecord] = []
        answer_parts: list[str] = []
        usage = AgentUsage()
        yield AgentEvent(type="run_started", run_id=run_id, thread_id=thread_id)

        try:
            for round_index in range(self.limits.max_tool_rounds):
                round_text: list[str] = []
                calls: list[dict[str, Any]] = []
                stream = self.model.stream(
                    messages=history,
                    tools=self.tools.definitions,
                    system=self.instructions,
                    options={**self.model_options, **dict(options)},
                )
                try:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("agent run exceeded its timeout")
                        try:
                            event = await asyncio.wait_for(stream.__anext__(), timeout=remaining)
                        except StopAsyncIteration:
                            break
                        kind = event.get("type")
                        if kind == "content":
                            text = str(event.get("text") or "")
                            round_text.append(text)
                            answer_parts.append(text)
                            yield AgentEvent(
                                type="text_delta",
                                run_id=run_id,
                                thread_id=thread_id,
                                text=text,
                            )
                        elif kind == "reasoning":
                            yield AgentEvent(
                                type="reasoning_delta",
                                run_id=run_id,
                                thread_id=thread_id,
                                text=str(event.get("text") or ""),
                            )
                        elif kind == "tool_calls":
                            raw_calls = event.get("calls")
                            if isinstance(raw_calls, list):
                                calls = [
                                    dict(call) for call in raw_calls if isinstance(call, Mapping)
                                ]
                        elif kind == "usage":
                            usage = AgentUsage(
                                input_tokens=event.get("in"),
                                output_tokens=event.get("out"),
                            )
                            yield AgentEvent(
                                type="usage",
                                run_id=run_id,
                                thread_id=thread_id,
                                usage=usage,
                            )
                        elif kind == "error":
                            raise AgentRunError(str(event.get("text") or "model provider failed"))
                finally:
                    close_stream = getattr(stream, "aclose", None)
                    if close_stream is not None:
                        await close_stream()

                if not calls:
                    history.append(AgentMessage(role="assistant", text="".join(round_text)))
                    await self.thread_store.save(thread_id, history)
                    result = AgentRunResult(
                        run_id=run_id,
                        thread_id=thread_id,
                        text="".join(answer_parts).strip(),
                        messages=tuple(history),
                        tool_calls=tuple(records),
                        usage=usage,
                    )
                    yield AgentEvent(
                        type="run_completed",
                        run_id=run_id,
                        thread_id=thread_id,
                        run_result=result,
                    )
                    return

                history.append(
                    AgentMessage(
                        role="assistant",
                        text="".join(round_text),
                        tool_calls=tuple(calls),
                    )
                )
                for index, raw_call in enumerate(calls):
                    if len(records) >= self.limits.max_tool_calls:
                        raise AgentRunError(
                            f"stopped after {self.limits.max_tool_calls} tool calls"
                        )
                    call_id = str(raw_call.get("id") or f"call-{round_index}-{index}")
                    name = str(raw_call.get("name") or "")
                    raw_arguments = raw_call.get("arguments") or "{}"
                    try:
                        arguments = (
                            json.loads(raw_arguments)
                            if isinstance(raw_arguments, str)
                            else dict(raw_arguments)
                        )
                        if not isinstance(arguments, dict):
                            raise ValueError("arguments must be an object")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        arguments = {}
                        tool_result = {
                            "ok": False,
                            "error": {
                                "code": "INVALID_INPUT",
                                "message": f"invalid tool arguments: {exc}",
                                "hint": "Call the tool again with one JSON object.",
                            },
                            "meta": {},
                        }
                    else:
                        yield AgentEvent(
                            type="tool_call_started",
                            run_id=run_id,
                            thread_id=thread_id,
                            tool_call_id=call_id,
                            tool_name=name,
                            arguments=arguments,
                        )
                        tool_call = ToolCall(id=call_id, name=name, arguments=arguments)
                        try:
                            definition = self.tools.require(name)
                        except KeyError:
                            definition = None
                        if definition is not None and definition.requires_approval:
                            request = ApprovalRequest(
                                request_id=_id("approval"),
                                run_id=run_id,
                                thread_id=thread_id,
                                tool_call_id=call_id,
                                tool_name=name,
                                arguments=arguments,
                                required_capabilities=definition.required_capabilities,
                            )
                            decision_task = asyncio.create_task(
                                self.approval_handler.request(request)
                            )
                            await asyncio.sleep(0)
                            try:
                                yield AgentEvent(
                                    type="approval_requested",
                                    run_id=run_id,
                                    thread_id=thread_id,
                                    tool_call_id=call_id,
                                    tool_name=name,
                                    arguments=arguments,
                                    approval=request,
                                )
                                decision = await decision_task
                            finally:
                                if not decision_task.done():
                                    decision_task.cancel()
                                    with contextlib.suppress(asyncio.CancelledError):
                                        await decision_task
                            yield AgentEvent(
                                type="approval_resolved",
                                run_id=run_id,
                                thread_id=thread_id,
                                tool_call_id=call_id,
                                tool_name=name,
                                decision=decision,
                            )
                            if not decision.approved:
                                tool_result = {
                                    "ok": False,
                                    "error": {
                                        "code": "APPROVAL_REQUIRED",
                                        "message": f"the host denied {name}",
                                        "hint": decision.reason
                                        or "Ask the user to approve the action.",
                                    },
                                    "meta": {},
                                }
                            else:
                                tool_result = await self._execute_tool(tool_call)
                        else:
                            tool_result = await self._execute_tool(tool_call)

                    record = AgentToolCallRecord(
                        id=call_id,
                        name=name,
                        arguments=arguments,
                        ok=bool(tool_result.get("ok")),
                        summary=_summary(tool_result),
                        result=tool_result,
                    )
                    records.append(record)
                    yield AgentEvent(
                        type="tool_call_finished",
                        run_id=run_id,
                        thread_id=thread_id,
                        tool_call_id=call_id,
                        tool_name=name,
                        arguments=arguments,
                        result=tool_result,
                    )
                    history.append(
                        AgentMessage(
                            role="tool",
                            tool_call_id=call_id,
                            name=name,
                            text=_tool_text(tool_result, self.limits.max_tool_result_chars),
                        )
                    )
                await self.thread_store.save(thread_id, history)
            raise AgentRunError(f"stopped after {self.limits.max_tool_rounds} tool rounds")
        except (AgentRunError, TimeoutError) as exc:
            await self.thread_store.save(thread_id, history)
            yield AgentEvent(
                type="run_failed",
                run_id=run_id,
                thread_id=thread_id,
                text=str(exc),
            )
        except Exception as exc:
            await self.thread_store.save(thread_id, history)
            yield AgentEvent(
                type="run_failed",
                run_id=run_id,
                thread_id=thread_id,
                text=f"agent dependency failed with {type(exc).__name__}",
            )

    async def _execute_tool(self, call: ToolCall) -> dict[str, Any]:
        async def terminal(next_call: ToolCall) -> dict[str, Any]:
            return await self.tools.call(next_call.name, next_call.arguments)

        call_next = terminal
        for middleware in reversed(self.middleware):
            downstream = call_next

            async def wrapped(
                next_call: ToolCall,
                *,
                current: ToolMiddleware = middleware,
                following=downstream,
            ) -> dict[str, Any]:
                return await current(next_call, following)

            call_next = wrapped
        return await call_next(call)


__all__ = ["Agent", "AgentRunError"]
