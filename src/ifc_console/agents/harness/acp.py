"""The ACP side of an engine: one subprocess per thread, events as AgentEvents.

The engine runs its own loop and calls its own tools; this module only
translates. Session updates become the same ``AgentEvent`` kinds the bundled
``Agent`` emits, and a permission request becomes the panel's approval card.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from ifc_console.agents.harness.engine import MCP_SERVER_NAME, EngineSpec
from ifc_console.agents.models import (
    AgentEvent,
    AgentLimits,
    AgentProgress,
    AgentToolCallRecord,
    ApprovalDecision,
    ApprovalRequest,
)

log = logging.getLogger("ifc-console.agents.harness")

# How engines prefix the tools of an injected MCP server.
_SERVER_PREFIXES = tuple(
    f"{prefix}{name}{joiner}"
    for name in (MCP_SERVER_NAME, MCP_SERVER_NAME.replace("-", "_"))
    for prefix, joiner in (("mcp__", "__"), ("", "__"), ("", "_"), ("", "."), ("", ":"))
)
_CANCEL_TIMEOUT_S = 5.0
_SHUTDOWN_TIMEOUT_S = 2.0
_MAX_NOTE = 120


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def native_tool_name(title: str | None) -> str:
    """The ifc-console name behind an engine's title for an MCP tool call."""
    name = (title or "").strip() or "tool"
    for prefix in _SERVER_PREFIXES:
        if name.startswith(prefix) and len(name) > len(prefix):
            return name[len(prefix) :]
    return name


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        return {"input": value}
    if value is None:
        return {}
    return {"input": value}


def _block_text(block: Any) -> str:
    kind = getattr(block, "type", "")
    if kind == "text":
        return str(getattr(block, "text", "") or "")
    if kind == "image":
        return "[image]"
    if kind == "resource_link":
        return str(getattr(block, "uri", "") or "[resource]")
    if kind == "resource":
        resource = getattr(block, "resource", None)
        text = getattr(resource, "text", None)
        return str(text) if text else str(getattr(resource, "uri", "") or "[resource]")
    if kind == "audio":
        return "[audio]"
    return ""


def _content_items(update: Any) -> tuple[list[str], list[str], list[str]]:
    """Texts, edited paths, terminal ids carried by a tool call update."""
    texts: list[str] = []
    edited: list[str] = []
    terminals: list[str] = []
    for item in getattr(update, "content", None) or ():
        kind = getattr(item, "type", "")
        if kind == "content":
            text = _block_text(getattr(item, "content", None))
            if text:
                texts.append(text)
        elif kind == "diff":
            edited.append(str(getattr(item, "path", "") or ""))
        elif kind == "terminal":
            terminals.append(str(getattr(item, "terminal_id", "") or ""))
    return texts, edited, terminals


def _looks_like_envelope(value: Any) -> bool:
    return isinstance(value, Mapping) and "ok" in value


def _envelope_from_raw(raw: Any, texts: list[str]) -> tuple[dict | None, bool]:
    """(envelope, consumed): the console envelope hidden in rawOutput, if any."""
    candidate: Any = raw
    if isinstance(candidate, str):
        with contextlib.suppress(ValueError):
            candidate = json.loads(candidate)
    if _looks_like_envelope(candidate):
        return dict(candidate), True
    if isinstance(candidate, Mapping):
        structured = candidate.get("structuredContent")
        if _looks_like_envelope(structured):
            return dict(structured), True
        items = candidate.get("content")
        if isinstance(items, list):
            for item in items:
                if not (isinstance(item, Mapping) and item.get("type") == "text"):
                    continue
                inner: Any = item.get("text")
                with contextlib.suppress(ValueError, TypeError):
                    inner = json.loads(inner)
                if _looks_like_envelope(inner):
                    return dict(inner), True
                texts.append(str(item.get("text") or ""))
            return None, True
    return None, False


def envelope_from_update(update: Any, *, engine: str, limit: int, failed: bool = False) -> dict:
    """What a finished tool call returned, as an {ok, data, meta} envelope.

    An engine puts whatever it likes in rawOutput; the console's own tools
    come back as envelopes or MCP results, everything else is wrapped.
    """
    texts, edited, terminals = _content_items(update)
    raw = getattr(update, "raw_output", None)
    source = f"harness:{engine}"
    if failed:
        message = (texts[0] if texts else "") or str(raw or "the engine reported a failure")
        return {
            "ok": False,
            "error": {"code": "TOOL_FAILED", "message": message[:400], "hint": ""},
            "meta": {"tool_source": source},
        }
    envelope, consumed = _envelope_from_raw(raw, texts)
    if envelope is not None:
        meta = dict(envelope.get("meta") or {})
        meta.setdefault("tool_source", source)
        envelope["meta"] = meta
        return envelope
    output = ""
    if not consumed and raw not in (None, {}, ""):
        output = raw if isinstance(raw, str) else json.dumps(raw, default=str)
    if not output:
        output = "\n".join(text for text in texts if text)
    data: dict[str, Any] = {"output": output[:limit]}
    if edited:
        data["edited"] = edited
    if terminals:
        data["terminal"] = terminals[0]
    return {"ok": True, "data": data, "meta": {"tool_source": source}}


def _summary(envelope: Mapping[str, Any]) -> str:
    if envelope.get("ok"):
        meta = envelope.get("meta") or {}
        returned = meta.get("returned") if isinstance(meta, Mapping) else None
        return f"{returned} row(s)" if returned is not None else "ok"
    error = envelope.get("error") or {}
    if isinstance(error, Mapping):
        return f"{error.get('code', 'ERROR')}: {error.get('message', '')}"[:200]
    return "failed"


def _render_plan(entries: Sequence[Any]) -> str:
    lines = []
    for entry in entries:
        status = str(getattr(entry, "status", "") or "")
        mark = "x" if status == "completed" else ("-" if status == "in_progress" else " ")
        lines.append(f"- [{mark}] {getattr(entry, 'content', '')}")
    return "Plan:\n" + "\n".join(lines) + "\n" if lines else ""


@dataclass
class _ToolCallState:
    id: str
    name: str
    arguments: dict[str, Any]
    started_at: float
    finished: bool = False


@dataclass
class RunState:
    """Everything one prompt turn accumulates while the engine works."""

    run_id: str
    thread_id: str
    engine: str
    limits: AgentLimits
    decider: Any
    capabilities_for: Callable[[str], tuple[str, ...]] = lambda name: ()
    queue: asyncio.Queue[Any] = field(default_factory=asyncio.Queue)
    answer: list[str] = field(default_factory=list)
    records: list[AgentToolCallRecord] = field(default_factory=list)
    calls: dict[str, _ToolCallState] = field(default_factory=dict)
    permission_tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    cancelled: bool = False
    cancel_reason: str = ""
    cancel_sent: bool = False
    approval_credit_s: float = 0.0
    tool_calls_started: int = 0
    plan_text: str = ""

    def emit(self, **fields: Any) -> None:
        self.queue.put_nowait(AgentEvent(run_id=self.run_id, thread_id=self.thread_id, **fields))

    @property
    def budget_exceeded(self) -> bool:
        return self.tool_calls_started >= self.limits.max_tool_calls > 0

    # -- session/update ---------------------------------------------------
    def on_update(self, update: Any) -> None:
        kind = str(getattr(update, "session_update", "") or "")
        if kind == "agent_message_chunk":
            text = _block_text(getattr(update, "content", None))
            if text:
                self.answer.append(text)
                self.emit(type="text_delta", text=text)
        elif kind == "agent_thought_chunk":
            text = _block_text(getattr(update, "content", None))
            if text:
                self.emit(type="reasoning_delta", text=text)
        elif kind == "tool_call":
            self._start_call(update)
            if getattr(update, "status", None) in ("completed", "failed"):
                self._finish_call(update)
        elif kind == "tool_call_update":
            call_id = str(getattr(update, "tool_call_id", "") or "")
            if call_id not in self.calls:
                self._start_call(update)
            status = getattr(update, "status", None)
            if status in ("completed", "failed"):
                self._finish_call(update)
            else:
                state = self.calls[call_id]
                texts, _edited, _terminals = _content_items(update)
                note = (getattr(update, "title", None) or (texts[0] if texts else "") or "")[
                    :_MAX_NOTE
                ]
                self.emit(
                    type="tool_progress",
                    tool_call_id=call_id,
                    tool_name=state.name,
                    progress=AgentProgress(
                        note=note, elapsed_s=max(0.0, time.monotonic() - state.started_at)
                    ),
                )
        elif kind == "plan":
            rendered = _render_plan(getattr(update, "entries", None) or ())
            if rendered and rendered != self.plan_text:
                self.plan_text = rendered
                self.emit(type="reasoning_delta", text="\n" + rendered)
        elif kind == "plan_update":
            plan = getattr(update, "plan", None)
            text = str(getattr(plan, "content", "") or "")
            entries = getattr(plan, "entries", None)
            if entries:
                text = _render_plan(entries)
            if text and text != self.plan_text:
                self.plan_text = text
                self.emit(type="reasoning_delta", text="\n" + text)
        else:
            log.debug("ignored ACP update %s", kind)

    def _start_call(self, update: Any) -> None:
        call_id = str(getattr(update, "tool_call_id", "") or "") or _id("call")
        if call_id in self.calls:
            return
        name = native_tool_name(getattr(update, "title", None))
        arguments = _as_dict(getattr(update, "raw_input", None))
        self.calls[call_id] = _ToolCallState(
            id=call_id, name=name, arguments=arguments, started_at=time.monotonic()
        )
        self.tool_calls_started += 1
        self.emit(
            type="tool_call_started", tool_call_id=call_id, tool_name=name, arguments=arguments
        )

    def _finish_call(self, update: Any) -> None:
        call_id = str(getattr(update, "tool_call_id", "") or "")
        state = self.calls[call_id]
        if state.finished:
            return
        state.finished = True
        failed = getattr(update, "status", None) == "failed"
        envelope = envelope_from_update(
            update, engine=self.engine, limit=self.limits.max_tool_result_chars, failed=failed
        )
        self.records.append(
            AgentToolCallRecord(
                id=call_id,
                name=state.name,
                arguments=state.arguments,
                ok=bool(envelope.get("ok")),
                summary=_summary(envelope),
                result=envelope,
            )
        )
        self.emit(
            type="tool_call_finished",
            tool_call_id=call_id,
            tool_name=state.name,
            arguments=state.arguments,
            result=envelope,
        )

    # -- session/request_permission ---------------------------------------
    async def on_permission(self, tool_call: Any, options: Sequence[Any]) -> Any:
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        call_id = str(getattr(tool_call, "tool_call_id", "") or "")
        if call_id and call_id not in self.calls:
            self._start_call(tool_call)
        state = self.calls.get(call_id)
        name = state.name if state else native_tool_name(getattr(tool_call, "title", None))
        arguments = state.arguments if state else _as_dict(getattr(tool_call, "raw_input", None))
        request = ApprovalRequest(
            request_id=_id("approval"),
            run_id=self.run_id,
            thread_id=self.thread_id,
            tool_call_id=call_id or _id("call"),
            tool_name=name,
            arguments=arguments,
            required_capabilities=tuple(self.capabilities_for(name)),
        )
        if self.cancelled:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        self.emit(
            type="approval_requested",
            tool_call_id=request.tool_call_id,
            tool_name=name,
            arguments=arguments,
            approval=request,
        )
        asked_at = time.monotonic()
        task = asyncio.ensure_future(self.decider.request(request))
        self.permission_tasks.add(task)
        decision: ApprovalDecision | None
        try:
            decision = await asyncio.wait_for(task, timeout=self.limits.approval_timeout_s)
        except asyncio.TimeoutError:
            decision = None
        except asyncio.CancelledError:
            self.cancelled = True
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        finally:
            self.permission_tasks.discard(task)
            self.approval_credit_s += time.monotonic() - asked_at
        if self.cancelled:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        if decision is None:
            decision = ApprovalDecision(
                approved=False, decided_by="timeout", reason="nobody answered in time"
            )
        elif not isinstance(decision, ApprovalDecision):
            decision = ApprovalDecision(approved=bool(decision), decided_by="host")
        self.emit(
            type="approval_resolved",
            tool_call_id=request.tool_call_id,
            tool_name=name,
            decision=decision,
        )
        wanted = (
            ("allow_once", "allow_always")
            if decision.approved
            else ("reject_once", "reject_always")
        )
        for kind in wanted:
            for option in options:
                if getattr(option, "kind", "") == kind:
                    return RequestPermissionResponse(
                        outcome=AllowedOutcome(outcome="selected", option_id=option.option_id)
                    )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    def cancel_pending(self) -> None:
        for task in tuple(self.permission_tasks):
            if not task.done():
                task.cancel()


class HarnessAcpClient:
    """The client half of the protocol: updates in, permissions answered."""

    def __init__(self, session: AcpSession) -> None:
        self.session = session

    async def session_update(self, session_id: str, update: Any, **_kwargs: Any) -> None:
        run = self.session.run
        if run is None or session_id != self.session.session_id:
            return
        run.on_update(update)

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[Any], **_kwargs: Any
    ) -> Any:
        from acp.schema import DeniedOutcome, RequestPermissionResponse

        run = self.session.run
        if run is None or session_id != self.session.session_id:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return await run.on_permission(tool_call, options)


class AcpSession:
    """One engine process and the ACP session that lives in it."""

    def __init__(
        self,
        engine: EngineSpec,
        *,
        thread_id: str,
        cwd: Path,
        env: Mapping[str, str],
        mcp_servers: Callable[[bool], Sequence[Any]],
        stderr_path: Path | None = None,
    ) -> None:
        self.engine = engine
        self.thread_id = thread_id
        self.cwd = cwd
        self.env = dict(env)
        self._mcp_servers = mcp_servers
        self.stderr_path = stderr_path
        self.conn: Any = None
        self.process: Any = None
        self.session_id: str = ""
        self.capabilities: Any = None
        self.modes: Any = None
        self.run: RunState | None = None
        self.turns = 0
        self.last_used = time.monotonic()
        self.closed = False
        self._stack: AsyncExitStack | None = None
        self._stderr: Any = None
        self._close_lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self.run is not None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None and not self.closed

    async def start(self) -> None:
        from acp import PROTOCOL_VERSION
        from acp.schema import ClientCapabilities, Implementation
        from acp.stdio import spawn_agent_process

        from ifc_console.agents import __version__

        executable = self.engine.resolve_command()
        if executable is None:
            raise RuntimeError(
                f"engine {self.engine.name!r}: {self.engine.command} is not installed or not on PATH"
            )
        stderr: Any = subprocess.DEVNULL
        if self.stderr_path is not None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr = open(self.stderr_path, "ab")  # noqa: SIM115
            stderr = self._stderr
        stack = AsyncExitStack()
        try:
            self.conn, self.process = await stack.enter_async_context(
                spawn_agent_process(
                    HarnessAcpClient(self),
                    executable,
                    *self.engine.args,
                    env=self.env,
                    cwd=self.cwd,
                    transport_kwargs={"stderr": stderr, "shutdown_timeout": _SHUTDOWN_TIMEOUT_S},
                )
            )
            response = await self.conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),
                client_info=Implementation(name="ifc-console", version=__version__),
            )
            self.capabilities = getattr(response, "agent_capabilities", None)
            http = bool(
                getattr(getattr(self.capabilities, "mcp_capabilities", None), "http", False)
            )
            session = await self.conn.new_session(
                cwd=str(self.cwd), mcp_servers=list(self._mcp_servers(http))
            )
            self.session_id = session.session_id
            self.modes = getattr(session, "modes", None)
            if self.engine.mode:
                await self._select_mode(self.engine.mode)
        except BaseException:
            await stack.aclose()
            self._close_stderr()
            raise
        self._stack = stack
        self.last_used = time.monotonic()

    async def _select_mode(self, mode_id: str) -> None:
        available = {
            str(getattr(mode, "id", ""))
            for mode in getattr(self.modes, "available_modes", ()) or ()
        }
        if available and mode_id not in available:
            log.warning(
                "engine %s has no session mode %r (has %s)",
                self.engine.name,
                mode_id,
                ", ".join(sorted(available)),
            )
            return
        with contextlib.suppress(Exception):
            await self.conn.set_session_mode(session_id=self.session_id, mode_id=mode_id)

    def supports_images(self) -> bool:
        prompt = getattr(self.capabilities, "prompt_capabilities", None)
        return bool(getattr(prompt, "image", False))

    async def prompt(self, run: RunState, blocks: Sequence[Any]) -> Any:
        """Send one turn. The run collects updates until the response lands."""
        if self.run is not None:
            raise RuntimeError("the engine session already has a prompt in flight")
        self.run = run
        self.turns += 1
        try:
            return await self.conn.prompt(session_id=self.session_id, prompt=list(blocks))
        finally:
            self.run = None
            self.last_used = time.monotonic()

    async def cancel(self, reason: str) -> None:
        run = self.run
        if run is not None:
            if run.cancel_sent:
                return
            run.cancel_sent = True
            run.cancelled = True
            run.cancel_reason = run.cancel_reason or reason
            run.cancel_pending()
        if self.conn is None or self.closed:
            return
        try:
            await asyncio.wait_for(self.conn.cancel(session_id=self.session_id), _CANCEL_TIMEOUT_S)
        except Exception:
            log.debug("engine %s did not take the cancel", self.engine.name, exc_info=True)

    async def close(self) -> None:
        """Stop the process: stdin EOF first, then the whole process tree."""
        async with self._close_lock:
            if self.closed:
                return
            self.closed = True
            await self._close_locked()

    async def _close_locked(self) -> None:
        run = self.run
        if run is not None:
            run.cancelled = True
            run.cancel_pending()
        process = self.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                if process.stdin is not None:
                    process.stdin.close()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(process.wait(), _SHUTDOWN_TIMEOUT_S)
            if process.returncode is None:
                _kill_tree(process)
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
            self._stack = None
        self._close_stderr()

    def kill_sync(self) -> None:
        """Best effort from a synchronous shutdown path; nothing is awaited."""
        self.closed = True
        process = self.process
        if process is not None and process.returncode is None:
            _kill_tree(process)
        self._close_stderr()

    def _close_stderr(self) -> None:
        if self._stderr is not None:
            with contextlib.suppress(Exception):
                self._stderr.close()
            self._stderr = None


def _kill_tree(process: Any) -> None:
    """Kill the engine and whatever it spawned (npx wrappers leave children)."""
    pid = getattr(process, "pid", None)
    if pid is None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    with contextlib.suppress(Exception):
        process.kill()


__all__ = [
    "AcpSession",
    "HarnessAcpClient",
    "RunState",
    "envelope_from_update",
    "native_tool_name",
]
