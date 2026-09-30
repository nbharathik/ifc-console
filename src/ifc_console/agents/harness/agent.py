"""An external engine behind the same surface as the bundled Agent.

``HarnessAgent`` has what the panel, the workflow runner and the CLI touch on
an ``Agent``: ``tools``, ``limits``, ``thread_store``, ``approval_handler``,
``middleware`` and ``stream()`` yielding ``AgentEvent``. The engine runs the
loop and its own tools; the console keeps the transcript, the budget clock,
the approvals and the IFC tools it serves over MCP.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any
from uuid import uuid4

from ifc_console.agents.harness.acp import _CANCEL_TIMEOUT_S, AcpSession, RunState
from ifc_console.agents.harness.engine import EngineSpec
from ifc_console.agents.models import (
    AgentEvent,
    AgentImage,
    AgentLimits,
    AgentMessage,
    AgentRunResult,
    AgentUsage,
)
from ifc_console.agents.storage import InMemoryThreadStore

log = logging.getLogger("ifc-console.agents.harness")

_DONE = object()
_INSTRUCTIONS_HEAD = "[ifc-console session instructions]"
_INSTRUCTIONS_TAIL = "[end of instructions]"
_RECAP_HEAD = "[recap of this conversation so far]"
_RECAP_TAIL = "[end of recap]"
_TOOL_RULE = (
    "Your ifc-console tools are served by the MCP server named 'ifc-console' "
    "(your client may show them with that prefix). Use only those tools for "
    "anything about the IFC model; do not use your own file or shell tools on "
    "the model. Every fact about the model comes from those tools."
)
_STOP_NOTE = "[The user stopped this run"
_RECAP_TURNS = 6
_RECAP_CHARS = 600
_TIMEOUT_TEXT = "agent run exceeded its timeout"


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def session_instructions(instructions: str) -> str:
    """The composed prompt, with the tool list retitled for an MCP client."""
    text = instructions.strip()
    marker = "Your tools:\n"
    index = text.rfind(marker)
    if index >= 0:
        listing = text[index + len(marker) :].strip()
        text = text[:index].rstrip() + "\n\n" + _TOOL_RULE + "\n\nTools:\n" + listing
    else:
        text = text + "\n\n" + _TOOL_RULE if text else _TOOL_RULE
    return text


def _recap(history: Sequence[AgentMessage]) -> str:
    turns = [m for m in history if m.role in ("user", "assistant") and m.text.strip()]
    if not turns:
        return ""
    lines = []
    for message in turns[-_RECAP_TURNS:]:
        text = message.text.strip()
        if len(text) > _RECAP_CHARS:
            text = text[:_RECAP_CHARS] + "..."
        lines.append(f"{message.role}: {text}")
    return "\n".join((_RECAP_HEAD, *lines, _RECAP_TAIL))


class HarnessAgent:
    """A thread's worth of turns on one engine, event for event like Agent."""

    def __init__(
        self,
        *,
        name: str,
        engine: EngineSpec,
        host: Any,
        tools: Any,
        instructions: str,
        thread_store: Any = None,
        approval_handler: Any = None,
        limits: AgentLimits | None = None,
        middleware: Sequence[Any] = (),
        model_label: str = "",
    ) -> None:
        from ifc_console.agents.approvals import DenyAllApprovals

        self.name = name.strip() or f"{engine.name}-engine"
        self.engine = engine
        self.host = host
        self.tools = tools
        self.instructions = instructions.strip()
        self.thread_store = thread_store or InMemoryThreadStore()
        self.approval_handler = approval_handler or DenyAllApprovals()
        self.limits = limits or AgentLimits()
        self.middleware = tuple(middleware)
        self.model = None
        self.model_label = model_label or engine.provider_id
        self._locks: dict[str, asyncio.Lock] = {}
        self._threads: set[str] = set()

    # -- what the engine gets told ----------------------------------------
    def _capabilities_for(self, name: str) -> tuple[str, ...]:
        require = getattr(self.tools, "require", None)
        if require is None:
            return ()
        try:
            return tuple(require(name).required_capabilities)
        except Exception:
            return ()

    def _turn_text(self, prompt: str, history: Sequence[AgentMessage], *, created: bool) -> str:
        parts: list[str] = []
        if created and self.engine.instructions in ("prompt", "both"):
            parts.append(
                "\n".join(
                    (
                        _INSTRUCTIONS_HEAD,
                        session_instructions(self.instructions),
                        _INSTRUCTIONS_TAIL,
                    )
                )
            )
        if created and history:
            recap = _recap(history)
            if recap:
                parts.append(recap)
        elif history:
            last = history[-1]
            if last.role == "assistant" and _STOP_NOTE in last.text:
                parts.append(last.text[last.text.index(_STOP_NOTE) :].strip())
        parts.append(prompt)
        return "\n\n".join(parts)

    # -- the Agent surface --------------------------------------------------
    async def run(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        response_model: Any = None,
        images: Sequence[AgentImage | Mapping[str, str]] | None = None,
    ) -> AgentRunResult:
        from ifc_console.agents.agent import AgentRunError, _parse_structured

        text = prompt
        if response_model is not None:
            schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
            text = (
                f"{prompt}\n\nEnd your final answer with only a JSON object matching "
                f"this schema, no prose around it:\n{schema}"
            )
        completed: AgentRunResult | None = None
        failure: str | None = None
        async for event in self.stream(text, thread_id=thread_id, options=options, images=images):
            if event.type == "run_completed":
                completed = event.run_result
            elif event.type == "run_failed":
                failure = event.text or "agent run failed"
        if completed is None:
            raise AgentRunError(failure or "agent run did not complete")
        if response_model is None:
            return completed
        try:
            data = _parse_structured(completed.text, response_model)
        except ValueError as exc:
            raise AgentRunError(
                f"the final answer did not match {response_model.__name__}: {exc}"
            ) from exc
        return completed.model_copy(update={"data": data})

    async def stream(
        self,
        prompt: str,
        *,
        thread_id: str | None = None,
        options: Mapping[str, Any] | None = None,
        images: Sequence[AgentImage | Mapping[str, str]] | None = None,
        approval_handler: Any = None,
    ) -> AsyncIterator[AgentEvent]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        del options  # temperature and friends belong to the engine's own config
        attached = tuple(AgentImage.model_validate(image) for image in images or ())
        thread = thread_id or _id("thread")
        lock = self._locks.setdefault(thread, asyncio.Lock())
        async with lock:
            async for event in self._stream_locked(
                prompt.strip(), thread=thread, images=attached, decider=approval_handler
            ):
                yield event

    async def _stream_locked(
        self,
        prompt: str,
        *,
        thread: str,
        images: tuple[AgentImage, ...],
        decider: Any,
    ) -> AsyncIterator[AgentEvent]:
        run_id = _id("run")
        decider = decider or self.approval_handler
        history = list(await self.thread_store.load(thread))
        prior = list(history)
        history.append(AgentMessage(role="user", text=prompt, images=images))
        self._threads.add(thread)
        yield AgentEvent(type="run_started", run_id=run_id, thread_id=thread)
        await self.thread_store.save(thread, history)

        try:
            session, created = await self.host.session_for(
                self.engine, thread, instructions=session_instructions(self.instructions)
            )
        except Exception as exc:
            log.warning("engine %s failed to start: %s", self.engine.name, exc)
            yield AgentEvent(
                type="run_failed",
                run_id=run_id,
                thread_id=thread,
                text=f"engine {self.engine.name} failed to start: {exc}"[:400],
            )
            return

        run = RunState(
            run_id=run_id,
            thread_id=thread,
            engine=self.engine.name,
            limits=self.limits,
            decider=decider,
            capabilities_for=self._capabilities_for,
        )
        blocks = self._blocks(self._turn_text(prompt, prior, created=created), images, session)
        prompt_task = asyncio.ensure_future(session.prompt(run, blocks))
        prompt_task.add_done_callback(lambda _task: run.queue.put_nowait(_DONE))
        deadline = time.monotonic() + self.limits.timeout_s
        timed_out = False
        try:
            while True:
                remaining = deadline + run.approval_credit_s - time.monotonic()
                if remaining <= 0 and not run.cancelled:
                    timed_out = True
                    await session.cancel("timeout")
                    remaining = _CANCEL_TIMEOUT_S
                try:
                    item = await asyncio.wait_for(run.queue.get(), timeout=max(remaining, 0.05))
                except asyncio.TimeoutError:
                    if run.cancelled and prompt_task.done():
                        break
                    if run.cancelled:
                        # the engine ignored the cancel; end its process
                        await self.host.discard(session)
                        with contextlib.suppress(asyncio.TimeoutError, Exception):
                            await asyncio.wait_for(asyncio.shield(prompt_task), _CANCEL_TIMEOUT_S)
                        break
                    continue
                if item is _DONE:
                    break
                if item.type == "tool_call_started" and run.budget_exceeded and not run.cancelled:
                    await session.cancel("budget")
                yield item
        except asyncio.CancelledError:
            await self._on_interrupt(session, run, prompt_task)
            partial = "".join(run.answer).strip()
            if partial:
                history.append(AgentMessage(role="assistant", text=partial))
            with contextlib.suppress(Exception):
                await self.thread_store.save(thread, history)
            raise

        while not run.queue.empty():
            item = run.queue.get_nowait()
            if item is not _DONE:
                yield item

        failure: str | None = None
        response: Any = None
        if not prompt_task.done():
            prompt_task.cancel()
            await self.host.discard(session)
            failure = _TIMEOUT_TEXT if timed_out else "the engine did not answer"
        elif prompt_task.cancelled():
            failure = _TIMEOUT_TEXT if timed_out else "the engine run was cancelled"
        elif prompt_task.exception() is not None:
            exc = prompt_task.exception()
            await self.host.discard(session)
            failure = (
                _TIMEOUT_TEXT
                if timed_out
                else f"agent engine {self.engine.name} failed: {type(exc).__name__}: {exc}"[:400]
            )
        else:
            response = prompt_task.result()

        answer = "".join(run.answer).strip()
        if answer:
            history.append(AgentMessage(role="assistant", text=answer))
        with contextlib.suppress(Exception):
            await self.thread_store.save(thread, history)

        usage = AgentUsage()
        reported = getattr(response, "usage", None)
        if reported is not None:
            usage = AgentUsage(
                input_tokens=getattr(reported, "input_tokens", None),
                output_tokens=getattr(reported, "output_tokens", None),
            )
            yield AgentEvent(type="usage", run_id=run_id, thread_id=thread, usage=usage)

        stop = str(getattr(response, "stop_reason", "") or "")
        if failure is None and timed_out:
            failure = _TIMEOUT_TEXT
        elif failure is None and stop == "refusal":
            failure = "the agent engine refused to continue"
        elif failure is None and stop == "cancelled" and run.cancel_reason not in ("budget",):
            failure = (
                _TIMEOUT_TEXT if run.cancel_reason == "timeout" else "the engine cancelled the run"
            )
        if failure is not None:
            yield AgentEvent(type="run_failed", run_id=run_id, thread_id=thread, text=failure)
            return

        stopped_reason = None
        if stop == "cancelled":
            stopped_reason = "tool_budget"
        elif stop in ("max_tokens", "max_turn_requests"):
            stopped_reason = "round_budget"
            if stop == "max_tokens":
                yield AgentEvent(
                    type="text_delta",
                    run_id=run_id,
                    thread_id=thread,
                    text="\n\n[the engine stopped: token limit reached]",
                )
        yield AgentEvent(
            type="run_completed",
            run_id=run_id,
            thread_id=thread,
            run_result=AgentRunResult(
                run_id=run_id,
                thread_id=thread,
                text=answer,
                messages=tuple(history),
                tool_calls=tuple(run.records),
                usage=usage,
                stopped_reason=stopped_reason,
            ),
        )

    async def _on_interrupt(self, session: AcpSession, run: RunState, prompt_task: Any) -> None:
        run.cancelled = True
        run.cancel_reason = run.cancel_reason or "user"
        run.cancel_pending()
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(session.cancel("user")), _CANCEL_TIMEOUT_S)
        try:
            await asyncio.wait_for(asyncio.shield(prompt_task), _CANCEL_TIMEOUT_S)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(Exception):
                await asyncio.shield(self.host.discard(session))

    @staticmethod
    def _blocks(text: str, images: tuple[AgentImage, ...], session: AcpSession) -> list[Any]:
        from acp.schema import ImageContentBlock, TextContentBlock

        blocks: list[Any] = [TextContentBlock(type="text", text=text)]
        if images and not session.supports_images():
            blocks[0] = TextContentBlock(
                type="text",
                text=text
                + f"\n\n[{len(images)} image(s) attached, but this engine cannot read images]",
            )
            return blocks
        for image in images:
            blocks.append(
                ImageContentBlock(type="image", data=image.data, mime_type=image.media_type)
            )
        return blocks

    def release(self) -> None:
        """Close this agent's engine sessions; the panel calls it on eviction."""
        for thread in tuple(self._threads):
            with contextlib.suppress(Exception):
                self.host.release_thread(thread)
        self._threads.clear()


class HarnessPack:
    """Any pack, built on an engine instead of a provider model."""

    def __init__(self, inner: Any, engine: EngineSpec, host: Any) -> None:
        self.inner = inner
        self.engine = engine
        self.host = host

    @property
    def info(self) -> Any:
        return self.inner.info

    @property
    def declared_limits(self) -> AgentLimits:
        limits = getattr(self.inner, "declared_limits", None)
        return limits if isinstance(limits, AgentLimits) else AgentLimits()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def configuration_signature(self) -> str:
        explicit = getattr(self.inner, "configuration_signature", None)
        base = explicit() if callable(explicit) else None
        return f"{base}|engine:{self.engine.signature()}"

    async def build(
        self,
        runtime: Any,
        *,
        model: Any = None,
        viewer: bool = False,
        instructions: str = "",
        model_label: str = "",
        **kwargs: Any,
    ) -> HarnessAgent:
        try:
            agent = await self.inner.build(
                runtime,
                model=model,
                viewer=viewer,
                instructions=instructions,
                model_label=model_label,
                **kwargs,
            )
        except TypeError:
            agent = await self.inner.build(runtime, model=model, viewer=viewer)
        return HarnessAgent(
            name=getattr(agent, "name", self.info.name),
            engine=self.engine,
            host=self.host,
            tools=agent.tools,
            instructions=getattr(agent, "instructions", ""),
            thread_store=getattr(agent, "thread_store", None),
            approval_handler=getattr(agent, "approval_handler", None),
            limits=getattr(agent, "limits", None),
            middleware=getattr(agent, "middleware", ()),
            model_label=model_label,
        )


__all__ = ["HarnessAgent", "HarnessPack", "session_instructions"]
