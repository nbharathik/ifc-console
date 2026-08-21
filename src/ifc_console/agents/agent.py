"""Composable, bounded agent loop over IFC, Python, and MCP tools."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from ifc_console.agents.approvals import DenyAllApprovals
from ifc_console.agents.models import (
    AgentEvent,
    AgentImage,
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


async def _before_deadline(
    deadline: float, operation: Callable[[], Awaitable[Any]]
) -> Any:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("agent run exceeded its timeout")
    try:
        return await asyncio.wait_for(operation(), timeout=remaining)
    except asyncio.TimeoutError:
        raise TimeoutError("agent run exceeded its timeout") from None


def _extract_images(tool_result: dict[str, Any]) -> tuple[dict[str, Any], tuple[AgentImage, ...]]:
    """Split image content out of a tool result.

    Base64 images do not belong in the JSON a model rereads every round; they
    become vision input on a follow-up user message instead. The tool result
    keeps a count in their place.
    """
    data = tool_result.get("data")
    if not isinstance(data, Mapping):
        return tool_result, ()
    raw = data.get("images")
    if not isinstance(raw, list) or not raw:
        return tool_result, ()
    images: list[AgentImage] = []
    for item in raw:
        if (
            isinstance(item, Mapping)
            and str(item.get("media_type", "")).startswith("image/")
            and isinstance(item.get("data"), str)
        ):
            try:
                images.append(AgentImage.model_validate(dict(item)))
            except Exception:
                return tool_result, ()
        else:
            return tool_result, ()
    slim = {**tool_result, "data": {**dict(data), "images": len(images)}}
    return slim, tuple(images)


def _timeout_tool_result() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "TIMEOUT",
            "message": "the agent run exceeded its timeout",
            "hint": "Retry with a longer timeout or a smaller task.",
        },
        "meta": {},
    }


def _parse_structured(text: str, response_model: type[BaseModel]) -> BaseModel:
    """The final text as a validated response_model instance, or ValueError."""
    body = text.strip()
    if body.startswith("```"):
        first_break = body.find("\n")
        closing = body.rfind("```")
        if first_break != -1 and closing > first_break:
            body = body[first_break + 1 : closing].strip()
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("the answer contains no JSON object")
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the answer is not valid JSON: {exc}") from exc
    try:
        return response_model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first.get("loc", ()))
        raise ValueError(f"{where or 'answer'}: {first.get('msg')}") from exc


def _add_usage(total: AgentUsage, current: AgentUsage) -> AgentUsage:
    def add(left: int | None, right: int | None) -> int | None:
        if left is None and right is None:
            return None
        return (left or 0) + (right or 0)

    return AgentUsage(
        input_tokens=add(total.input_tokens, current.input_tokens),
        output_tokens=add(total.output_tokens, current.output_tokens),
    )


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
        self._thread_lock_users: dict[str, int] = {}

    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        response_model: type[BaseModel] | None = None,
        images: Sequence[AgentImage | Mapping[str, str]] | None = None,
    ) -> AgentRunResult:
        if response_model is None:
            return await self._run_once(
                prompt, thread_id=thread_id, options=options, images=images
            )
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        guided = (
            f"{prompt}\n\nEnd your final answer with only a JSON object matching "
            f"this schema, no prose around it:\n{schema}"
        )
        result = await self._run_once(
            guided, thread_id=thread_id, options=options, images=images
        )
        for attempt in (0, 1):
            try:
                data = _parse_structured(result.text, response_model)
            except ValueError as exc:
                if attempt:
                    raise AgentRunError(
                        f"the final answer did not match {response_model.__name__}: {exc}"
                    ) from exc
                correction = (
                    f"That answer did not validate ({exc}). Answer again with "
                    "only the JSON object, nothing else."
                )
                result = await self._run_once(
                    correction, thread_id=result.thread_id, options=options
                )
                continue
            return result.model_copy(update={"data": data})
        raise AgentRunError("structured answer retry loop ended unexpectedly")

    async def _run_once(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        images: Sequence[AgentImage | Mapping[str, str]] | None = None,
    ) -> AgentRunResult:
        completed: AgentRunResult | None = None
        failure: str | None = None
        async for event in self.stream(
            prompt, thread_id=thread_id, options=options, images=images
        ):
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
        images: Sequence[AgentImage | Mapping[str, str]] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        attached = tuple(AgentImage.model_validate(image) for image in images or ())
        selected_thread = thread_id or _id("thread")
        lock = self._thread_locks.setdefault(selected_thread, asyncio.Lock())
        self._thread_lock_users[selected_thread] = (
            self._thread_lock_users.get(selected_thread, 0) + 1
        )
        try:
            async with lock:
                async for event in self._stream_locked(
                    prompt.strip(),
                    thread_id=selected_thread,
                    options=dict(options or {}),
                    images=attached,
                ):
                    yield event
        finally:
            users = self._thread_lock_users[selected_thread] - 1
            if users:
                self._thread_lock_users[selected_thread] = users
            else:
                self._thread_lock_users.pop(selected_thread, None)
                if self._thread_locks.get(selected_thread) is lock:
                    self._thread_locks.pop(selected_thread, None)

    async def _stream_locked(
        self,
        prompt: str,
        *,
        thread_id: str,
        options: Mapping[str, Any],
        images: tuple[AgentImage, ...] = (),
    ) -> AsyncIterator[AgentEvent]:
        run_id = _id("run")
        deadline = time.monotonic() + self.limits.timeout_s
        history = list(await self.thread_store.load(thread_id))
        history.append(AgentMessage(role="user", text=prompt, images=images))
        await self.thread_store.save(thread_id, history)
        records: list[AgentToolCallRecord] = []
        answer_parts: list[str] = []
        usage = AgentUsage()
        yield AgentEvent(type="run_started", run_id=run_id, thread_id=thread_id)

        try:
            for round_index in range(self.limits.max_tool_rounds):
                round_text: list[str] = []
                calls: list[dict[str, Any]] = []
                round_usage = AgentUsage()
                stream = self.model.stream(
                    messages=history,
                    tools=self.tools.definitions,
                    system=self.instructions,
                    options={**self.model_options, **dict(options)},
                )
                try:
                    while True:
                        try:
                            event = await _before_deadline(deadline, stream.__anext__)
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
                            round_usage = AgentUsage(
                                input_tokens=event.get("in"),
                                output_tokens=event.get("out"),
                            )
                            yield AgentEvent(
                                type="usage",
                                run_id=run_id,
                                thread_id=thread_id,
                                usage=round_usage,
                            )
                        elif kind == "error":
                            raise AgentRunError(str(event.get("text") or "model provider failed"))
                finally:
                    close_stream = getattr(stream, "aclose", None)
                    if close_stream is not None:
                        await _before_deadline(deadline, close_stream)
                usage = _add_usage(usage, round_usage)

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
                budget_exhausted = False
                deadline_exhausted = False
                # image-bearing results attach after the whole round: a user
                # message between two tool replies breaks provider transcripts
                round_images: list[tuple[str, tuple[AgentImage, ...]]] = []
                parallel = self._parallel_batch(calls, round_index)
                if parallel is not None and len(records) + len(calls) <= self.limits.max_tool_calls:
                    for call_id, name, arguments, _ in parallel:
                        yield AgentEvent(
                            type="tool_call_started",
                            run_id=run_id,
                            thread_id=thread_id,
                            tool_call_id=call_id,
                            tool_name=name,
                            arguments=arguments,
                        )
                    try:
                        results = await _before_deadline(
                            deadline,
                            lambda batch=parallel: asyncio.gather(
                                *(self._execute_tool(item[3]) for item in batch),
                                return_exceptions=True,
                            ),
                        )
                    except TimeoutError:
                        deadline_exhausted = True
                        results = [_timeout_tool_result() for _ in parallel]
                    for raised in results:
                        if isinstance(raised, BaseException):
                            raise raised
                    for (call_id, name, arguments, _), raw_result in zip(
                        parallel, results, strict=True
                    ):
                        tool_result, tool_images = _extract_images(raw_result)
                        if tool_images:
                            round_images.append((name, tool_images))
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
                    for name, tool_images in round_images:
                        history.append(
                            AgentMessage(
                                role="user",
                                text=f"[image content from {name}]",
                                images=tool_images,
                            )
                        )
                    await self.thread_store.save(thread_id, history)
                    if deadline_exhausted:
                        raise TimeoutError("agent run exceeded its timeout")
                    continue
                for index, raw_call in enumerate(calls):
                    call_id = str(raw_call.get("id") or f"call-{round_index}-{index}")
                    name = str(raw_call.get("name") or "")
                    raw_arguments = raw_call.get("arguments") or "{}"
                    parse_error: str | None = None
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
                        parse_error = str(exc)
                    yield AgentEvent(
                        type="tool_call_started",
                        run_id=run_id,
                        thread_id=thread_id,
                        tool_call_id=call_id,
                        tool_name=name,
                        arguments=arguments,
                    )
                    # Every requested call gets a tool message, even past the
                    # budget, so saved threads never carry a dangling tool call.
                    if deadline_exhausted or time.monotonic() >= deadline:
                        deadline_exhausted = True
                        tool_result = _timeout_tool_result()
                    elif len(records) >= self.limits.max_tool_calls:
                        budget_exhausted = True
                        tool_result = {
                            "ok": False,
                            "error": {
                                "code": "LIMIT_REACHED",
                                "message": (
                                    f"the run's {self.limits.max_tool_calls} tool-call "
                                    "budget is spent"
                                ),
                                "hint": "Answer with what you already have.",
                            },
                            "meta": {},
                        }
                    elif parse_error is not None:
                        tool_result = {
                            "ok": False,
                            "error": {
                                "code": "INVALID_INPUT",
                                "message": f"invalid tool arguments: {parse_error}",
                                "hint": "Call the tool again with one JSON object.",
                            },
                            "meta": {},
                        }
                    else:
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
                                try:
                                    decision = await _before_deadline(
                                        deadline, lambda task=decision_task: task
                                    )
                                except TimeoutError:
                                    decision = None
                                    deadline_exhausted = True
                            finally:
                                if not decision_task.done():
                                    decision_task.cancel()
                                    with contextlib.suppress(asyncio.CancelledError):
                                        await decision_task
                            if decision is None:
                                tool_result = _timeout_tool_result()
                            else:
                                yield AgentEvent(
                                    type="approval_resolved",
                                    run_id=run_id,
                                    thread_id=thread_id,
                                    tool_call_id=call_id,
                                    tool_name=name,
                                    decision=decision,
                                )
                            if decision is not None and not decision.approved:
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
                            elif decision is not None:
                                try:
                                    tool_result = await _before_deadline(
                                        deadline,
                                        lambda call=tool_call: self._execute_tool(call),
                                    )
                                except TimeoutError:
                                    deadline_exhausted = True
                                    tool_result = _timeout_tool_result()
                        else:
                            try:
                                tool_result = await _before_deadline(
                                    deadline,
                                    lambda call=tool_call: self._execute_tool(call),
                                )
                            except TimeoutError:
                                deadline_exhausted = True
                                tool_result = _timeout_tool_result()

                    tool_result, tool_images = _extract_images(tool_result)
                    if tool_images:
                        round_images.append((name, tool_images))
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
                for name, tool_images in round_images:
                    history.append(
                        AgentMessage(
                            role="user",
                            text=f"[image content from {name}]",
                            images=tool_images,
                        )
                    )
                await self.thread_store.save(thread_id, history)
                if deadline_exhausted:
                    raise TimeoutError("agent run exceeded its timeout")
                if budget_exhausted:
                    raise AgentRunError(f"stopped after {self.limits.max_tool_calls} tool calls")
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

    def _parallel_batch(
        self, calls: list[dict[str, Any]], round_index: int
    ) -> list[tuple[str, str, dict[str, Any], ToolCall]] | None:
        """The round's calls prepared for concurrent execution, or None.

        Only rounds where every call parses, resolves, is read-only, and needs
        no approval qualify; anything else takes the sequential path with its
        budget, approval, and error handling.
        """
        if not self.limits.parallel_read_only or len(calls) < 2:
            return None
        batch: list[tuple[str, str, dict[str, Any], ToolCall]] = []
        for index, raw_call in enumerate(calls):
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
                    return None
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            try:
                definition = self.tools.require(name)
            except KeyError:
                return None
            if definition.requires_approval:
                return None
            if definition.annotations.get("readOnlyHint") is not True:
                return None
            batch.append((call_id, name, arguments, ToolCall(id=call_id, name=name, arguments=arguments)))
        return batch

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
