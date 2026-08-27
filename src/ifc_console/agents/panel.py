"""The agent panel backend: packs behind the same chat surface.

Plain chat stays stateless; a pack conversation keeps its thread server side
so the pack's Agent owns history, tool budget, and structured state. Events
stream in the exact SSE vocabulary the chat panel already renders, so one
browser panel serves both.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from contextlib import nullcontext, suppress
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ifc_console.agents.agent import Agent
from ifc_console.agents.files import MAX_REFERENCE_BYTES
from ifc_console.agents.models import AgentLimits

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.agents")

_MAX_THREADS = 20
_MAX_PROMPT_CHARS = 100_000
_MAX_UPLOAD_BYTES = MAX_REFERENCE_BYTES
# The panel prints the call arguments beside the tool, so they have to be
# whole enough to read. Still bounded: a model can pass a long selector.
_MAX_ARGUMENT_CHARS = 2000


@dataclass
class PanelThread:
    pack: str
    agent: Agent
    signature: tuple[str, ...]
    content_gate: Any = None


class AgentPanelState:
    """Per-console-run panel threads and the shared runtime view."""

    def __init__(self) -> None:
        self.threads: OrderedDict[str, PanelThread] = OrderedDict()
        self.runtime: Any = None
        self.content_store: Any = None
        # Stream setup, deletion, and bulk reset all cross an await boundary.
        # Keep one lifecycle gate so a response that has been prepared but has
        # not started iterating cannot escape a concurrent reset.
        self.lifecycle_lock = asyncio.Lock()
        self.reset_epoch = 0
        self.thread_epochs: dict[str, int] = {}
        self.active_streams: dict[str, set[asyncio.Task[Any]]] = {}
        self.deleting_threads: set[str] = set()
        self.clearing_threads = False
        # request_id -> (the run that asked, the future its handler awaits).
        self.pending_approvals: dict[str, tuple[Any, asyncio.Future[Any]]] = {}

    def deny_owned(self, owner: Any) -> int:
        """Resolve one run's unanswered approvals to False, and only that run's.

        Every conversation shares this dict, so denying all of it on teardown
        would answer another panel's on-screen card behind the reader's back.
        """
        denied = 0
        for holder, pending in list(self.pending_approvals.values()):
            if holder is owner and not pending.done():
                pending.set_result(False)
                denied += 1
        return denied

    def remember(self, thread_id: str, thread: PanelThread) -> None:
        self.threads[thread_id] = thread
        self.threads.move_to_end(thread_id)
        while len(self.threads) > _MAX_THREADS:
            self.threads.popitem(last=False)


class PanelApprovalHandler:
    """Ask the browser, and wait.

    The agent is blocked on this call, so the future is the whole mechanism:
    the SSE stream has already told the reader what is being asked, and
    /api/agents/approve is what resolves it. A stream that dies takes its own
    pending decisions with it, which denies rather than hangs; ``owner`` is
    what keeps it from taking another conversation's with them.
    """

    def __init__(self, state: AgentPanelState, owner: Any = None) -> None:
        self.state = state
        self.owner = owner

    async def request(self, request: Any) -> Any:
        from ifc_console.agents.models import ApprovalDecision

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self.state.pending_approvals[request.request_id] = (self.owner, future)
        try:
            # The agent yields `approval_requested` before it awaits this, so
            # the browser already has the question. All this has to do is be
            # the thing that the answer lands on.
            decision = await future
        except asyncio.CancelledError:
            raise
        finally:
            self.state.pending_approvals.pop(request.request_id, None)
        if isinstance(decision, ApprovalDecision):
            return decision
        return ApprovalDecision(
            approved=bool(decision),
            decided_by="chat-panel",
        )


class AutoApprovalHandler:
    """Approve protected calls without asking, because the human said so.

    This is not a weaker policy than the deny-all default; it is the same
    decision made once, up front, instead of once per call. Mode, capability,
    path and ChangeSet checks all still run underneath it.
    """

    def __init__(self, core: AppCore) -> None:
        self.core = core

    async def request(self, request: Any) -> Any:
        from ifc_console.agents.models import ApprovalDecision

        self.core.audit.record(
            "agent_approval_auto",
            tool=request.tool_name,
            thread=request.thread_id,
        )
        return ApprovalDecision(
            approved=True,
            decided_by="session-autonomy",
            reason="the session is in auto mode",
        )


def _needs_decision(definition: Any) -> bool:
    """What the panel stops and asks about while autonomy is off.

    Reading the model is not worth a prompt; anything that runs generated
    code, writes an artifact, or touches the model is. Asking about every
    `get_element` would train people to click through the ones that matter.
    """
    tags = set(getattr(definition, "tags", ()) or ())
    if getattr(definition, "name", "") == "execute_ifc_code":
        return True
    if "read" in tags and not {"write", "preview", "destructive"} & tags:
        return False
    return True


def _valid_panel_thread_id(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("panel-") and len(value) <= 100


def _pack_configuration_digest(pack: Any) -> str:
    """Fingerprint every declarative input that can change an agent run."""
    payload: dict[str, Any] = {"info": pack.info.model_dump(mode="json")}
    for name in ("blueprint", "preset", "declared_limits"):
        value = getattr(pack, name, None)
        if value is None:
            continue
        if hasattr(value, "model_dump"):
            payload[name] = value.model_dump(mode="json")
        elif is_dataclass(value):
            payload[name] = asdict(value)
        else:
            payload[name] = str(value)
    explicit = getattr(pack, "configuration_signature", None)
    if callable(explicit):
        explicit = explicit()
    if explicit is not None:
        payload["host"] = str(explicit)
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(raw.encode("utf-8")).hexdigest()


def _signature_token(signature: tuple[str, ...]) -> str:
    return sha256("\x1f".join(signature).encode("utf-8")).hexdigest()[:12]


def _new_panel_thread_id(signature: tuple[str, ...]) -> str:
    return f"panel-{_signature_token(signature)}-{uuid4().hex[:12]}"


def _thread_matches_signature(thread_id: str, signature: tuple[str, ...]) -> bool:
    return thread_id.startswith(f"panel-{_signature_token(signature)}-")


def _thread_directory(core: AppCore) -> Path:
    return core.store.project_dir / ".ifc-console" / "agents" / "threads"


def _clear_owned_panel_thread_files(directory: Path) -> int:
    """Delete every identifiable panel thread, including invalid records.

    The directory can also be used by an embedding application's own
    ``JsonThreadStore``. A record is ours only when it carries a valid
    ``panel-*`` id and its hashed filename matches that id. We deliberately do
    not require a valid record version or messages array, so orphaned and
    semantically corrupt panel records are still removable.
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, UnicodeDecodeError, ValueError):
            # An unreadable record cannot safely be attributed to the panel;
            # preserving a possible embedding-owned thread is the safer side.
            continue
        thread_id = payload.get("thread_id") if isinstance(payload, dict) else None
        if not _valid_panel_thread_id(thread_id):
            continue
        expected = sha256(thread_id.encode("utf-8")).hexdigest() + ".json"
        if path.name != expected:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed += 1
    return removed


def _prompt_starts(messages: Sequence[Any]) -> list[int]:
    """Indexes of the real user turns, skipping the synthetic image carriers."""
    from ifc_console.agents.agent import IMAGE_TURN_PREFIX

    return [
        index
        for index, message in enumerate(messages)
        if message.role == "user" and not message.text.startswith(IMAGE_TURN_PREFIX)
    ]


def _tool_calls_since_last_prompt(messages: Sequence[Any]) -> int:
    starts = _prompt_starts(messages)
    tail = messages[starts[-1] :] if starts else messages
    return sum(1 for message in tail if message.role == "tool")


async def _cancel_stream_tasks(tasks: set[asyncio.Task[Any]]) -> int:
    current = asyncio.current_task()
    pending = [task for task in tasks if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return len(pending)


def _panel_state(core: AppCore) -> AgentPanelState:
    state = getattr(core, "agent_panel", None)
    if state is None:
        state = AgentPanelState()
        core.agent_panel = state
    return state


def _content_store(core: AppCore) -> Any:
    state = _panel_state(core)
    if state.content_store is None:
        from ifc_console.agents.content import AgentContentAccessStore

        state.content_store = AgentContentAccessStore(core.store.project_dir)
    return state.content_store


def _content_configuration(core: AppCore, pack: Any) -> tuple[str, ...] | None:
    from ifc_console.agents.content import configured_paths

    return configured_paths(pack, _content_store(core))


def _library_entries(core: AppCore) -> list[dict[str, Any]]:
    return core.agent_files.library_entries(core.project_knowledge.sources())


def _invalidate_agent_threads(core: AppCore, name: str) -> None:
    state = _panel_state(core)
    for thread_id, thread in list(state.threads.items()):
        if thread.pack == name:
            state.threads.pop(thread_id, None)


def panel_runtime(core: AppCore) -> Any:
    """A LocalRuntime view over the running console core, built once.

    The runtime does not own the workbench, and nothing here ever closes it:
    closing would shut the console down with it.
    """
    state = _panel_state(core)
    if state.runtime is None:
        from ifc_console.runtime import LocalOperationBackend, LocalRuntime
        from ifc_console.sdk import AsyncWorkbench

        workbench = AsyncWorkbench(core)
        backend = LocalOperationBackend(workbench, owns_workbench=False)
        state.runtime = LocalRuntime(backend)
    return state.runtime


def panel_limits(core: AppCore, requested: AgentLimits) -> AgentLimits:
    """Give the panel's run budget to a pack's own declared limits.

    A pack's rounds are its own, the way its tool calls already are.
    chat.max_tool_rounds bounds plain chat's loop; applying it here silently
    halved every preset that declares more and threw the paid rounds away.
    The run timeout is the host's: chat.timeout_s is the wait for a provider's
    first token, and a run that calls two ten-second tools outlives it.
    """
    return AgentLimits(
        max_tool_rounds=requested.max_tool_rounds,
        max_tool_calls=requested.max_tool_calls,
        timeout_s=float(core.settings.chat.run_timeout_s),
        approval_timeout_s=requested.approval_timeout_s,
        # Asking a tool for more characters than the run will keep pays for
        # serializing and injection-scanning text nobody ever reads.
        max_tool_result_chars=min(
            requested.max_tool_result_chars, int(core.settings.exec.output_char_limit)
        ),
        parallel_read_only=requested.parallel_read_only,
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _disabled() -> JSONResponse:
    return JSONResponse(
        {
            "error": "chat_disabled",
            "hint": "type /chat in the ifc-console terminal to turn the panel on",
        },
        status_code=404,
    )


def _proposed_change(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a write tool is about to do, from its arguments alone.

    The reviewer decides before the tool runs, so the readable card has to be
    built from the request. Reading raw JSON is not review.
    """
    from ifc_console.agents.provenance import (
        MEASUREMENT_PSET,
        PROPERTY_PSET,
        measurement_property,
    )

    targets = [str(value) for value in arguments.get("global_ids") or [] if value]
    common = {
        "elements": len(targets),
        "targets": targets[:8],
        "value": arguments.get("value"),
        "unit": str(arguments.get("unit") or ""),
        "method": str(arguments.get("method") or ""),
        "source": str(arguments.get("source") or ""),
        "confidence": str(arguments.get("confidence") or ""),
    }
    if tool_name == "measure__propose_measured_value":
        try:
            property_name, _nominal = measurement_property(str(arguments.get("metric") or ""))
        except ValueError:
            property_name = str(arguments.get("metric") or "")
        return {**common, "pset": MEASUREMENT_PSET, "property": property_name}
    if tool_name == "measure__propose_property_value":
        return {
            **common,
            "pset": PROPERTY_PSET,
            "property": str(arguments.get("property_name") or ""),
        }
    if tool_name == "preview_property_change":
        return {
            **common,
            "pset": str(arguments.get("pset_name") or ""),
            "property": str(arguments.get("property_name") or ""),
        }
    if tool_name == "preview_classification_assignment":
        return {
            **common,
            "pset": str(arguments.get("system") or ""),
            "property": str(arguments.get("reference") or ""),
        }
    if tool_name == "execute_ifc_code":
        code = str(arguments.get("code") or "")
        return {
            "elements": 0,
            "targets": [],
            "property": "",
            "pset": "",
            "description": str(arguments.get("description") or "")[:400],
            "code_lines": code.count("\n") + 1 if code else 0,
        }
    return None


def _result_extent(result: Mapping[str, Any]) -> dict[str, Any]:
    """How much of a paged result the model received, in the panel's words."""
    meta = result.get("meta") if isinstance(result.get("meta"), Mapping) else {}
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    cut = data.get("truncation") if isinstance(data.get("truncation"), Mapping) else {}
    extent: dict[str, Any] = {}
    if isinstance(meta.get("total"), int):
        extent["total"] = meta["total"]
    if isinstance(cut.get("kept"), int) and isinstance(cut.get("of"), int):
        extent["shown"] = cut["kept"]
        extent["of"] = cut["of"]
    if isinstance(cut.get("next_offset"), int):
        extent["next_offset"] = cut["next_offset"]
    if meta.get("truncated"):
        extent["truncated"] = True
    return extent


def _event_payloads(event: Any) -> list[dict[str, Any]]:
    """One typed AgentEvent as chat-panel SSE payloads.

    A delegated run's events carry their depth so the panel can nest them
    under the tool call that started them instead of interleaving them.
    """
    depth = int(getattr(event, "depth", 0) or 0)
    payloads = _typed_payloads(event)
    if depth:
        for payload in payloads:
            payload["depth"] = depth
    return payloads


def _typed_payloads(event: Any) -> list[dict[str, Any]]:
    if event.type == "text_delta":
        return [{"type": "content", "text": event.text or ""}]
    if event.type == "reasoning_delta":
        return [{"type": "reasoning", "text": event.text or ""}]
    if event.type == "tool_call_started":
        return [
            {
                "type": "tool_call",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "arguments": json.dumps(
                    event.arguments or {}, default=str, ensure_ascii=False, indent=1
                )[:_MAX_ARGUMENT_CHARS],
            }
        ]
    if event.type == "tool_progress":
        progress = event.progress
        return [
            {
                "type": "tool_progress",
                "id": event.tool_call_id,
                "name": event.tool_name or "",
                "done": int(getattr(progress, "done", 0) or 0),
                "total": getattr(progress, "total", None),
                "note": str(getattr(progress, "note", "") or ""),
                "elapsed": float(getattr(progress, "elapsed_s", 0.0) or 0.0),
            }
        ]
    if event.type == "approval_requested":
        request = event.approval
        payload = {
            "type": "approval",
            "request_id": getattr(request, "request_id", ""),
            "id": event.tool_call_id,
            "name": event.tool_name,
            "capabilities": list(getattr(request, "required_capabilities", ()) or ()),
            "arguments": json.dumps(
                event.arguments or {}, default=str, ensure_ascii=False, indent=1
            )[:_MAX_ARGUMENT_CHARS],
        }
        change = _proposed_change(event.tool_name or "", event.arguments or {})
        if change is not None:
            payload["proposal"] = change
        return [payload]
    if event.type == "approval_resolved":
        decision = event.decision
        return [
            {
                "type": "approval_decided",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "approved": bool(getattr(decision, "approved", False)),
                "decided_by": str(getattr(decision, "decided_by", "") or ""),
                "reason": str(getattr(decision, "reason", "") or ""),
            }
        ]
    if event.type == "tool_call_finished":
        from ifc_console.chat.agent import tool_event

        result = event.result or {}
        payloads = [
            {
                "type": "tool_result",
                "id": event.tool_call_id,
                "name": event.tool_name,
                **tool_event(result),
                **_result_extent(result),
            }
        ]
        from ifc_console.agents.proposals import PROPOSAL_TOOLS

        if event.tool_name in PROPOSAL_TOOLS and result.get("ok"):
            data = result.get("data") or {}
            record = data.get("change_set") if isinstance(data, dict) else None
            if isinstance(record, dict):
                change_set = record.get("change_set") or {}
                changes = change_set.get("changes") or []
                first = changes[0] if changes else {}
                arguments = event.arguments or {}
                payloads.append(
                    {
                        "type": "proposal",
                        "id": record.get("change_set_id"),
                        "count": len(changes),
                        "elements": data.get("elements", len(changes)),
                        "pset": data.get("pset_name")
                        or first.get("pset_name", "IfcConsole_AI_Measurements"),
                        "property": data.get("property_name")
                        or first.get("property_name", "Measured value"),
                        "value": first.get("after"),
                        "unit": arguments.get("unit", ""),
                        "method": arguments.get("method", ""),
                        "source": arguments.get("source", ""),
                        "confidence": arguments.get("confidence", ""),
                        "marked": bool(data.get("ai_marked")),
                        "provenance_change_set": data.get("provenance_change_set", ""),
                        "warning": data.get("warning", ""),
                        "ai_generated": True,
                    }
                )
        return payloads
    if event.type == "usage" and event.usage is not None:
        return [
            {"type": "usage", "in": event.usage.input_tokens, "out": event.usage.output_tokens}
        ]
    if event.type == "run_failed":
        return [{"type": "error", "text": event.text or "agent run failed"}]
    return []


async def _build_thread(
    core: AppCore,
    pack: Any,
    *,
    signature: tuple[str, ...],
    model: Any,
    persistent: bool,
    instructions: str = "",
    model_label: str = "",
    approval_handler: Any = None,
    ask_before_acting: bool = False,
) -> PanelThread:
    """Build one pack's agent.

    The user's own instructions become part of the agent's system prompt, not
    a suffix on their message, so the model treats them as standing policy and
    the block safety rules still sit above them.
    """
    try:
        agent = await pack.build(
            panel_runtime(core),
            model=model,
            viewer=core.viewer.enabled,
            instructions=instructions,
            model_label=model_label,
        )
    except TypeError:
        # A pack registered by an embedding application may predate the
        # instructions parameter; its prompt simply stays fixed.
        agent = await pack.build(
            panel_runtime(core), model=model, viewer=core.viewer.enabled
        )
    if persistent:
        from ifc_console.agents.storage import JsonThreadStore

        agent.thread_store = JsonThreadStore(_thread_directory(core))
    declared_limits = getattr(pack, "declared_limits", agent.limits)
    agent.limits = panel_limits(
        core,
        declared_limits if isinstance(declared_limits, AgentLimits) else agent.limits,
    )
    if approval_handler is not None:
        agent.approval_handler = approval_handler
    if ask_before_acting and hasattr(agent.tools, "requiring_approval"):
        agent.tools = agent.tools.requiring_approval(_needs_decision)

    from ifc_console.agents.content import AgentContentGate

    content_gate = AgentContentGate(_content_configuration(core, pack))
    agent.middleware = (*getattr(agent, "middleware", ()), content_gate)
    return PanelThread(
        pack=pack.info.name,
        agent=agent,
        signature=signature,
        content_gate=content_gate,
    )


def build_agent_panel_routes(core: AppCore) -> list[Route]:
    async def list_agents(_request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        registry = core.agent_packs
        return JSONResponse(
            {
                "agents": [info.model_dump(mode="json") for info in registry.active()],
                "problems": registry.problems,
            }
        )

    async def list_capabilities(_request) -> JSONResponse:
        """What the bundled agents need from this install, and how to repair it."""
        if not core.chat.enabled:
            return _disabled()
        from ifc_console.agents.environment import report

        payload = report()
        payload["viewer"] = core.viewer.enabled
        payload["mode"] = core.policy.mode.value
        return JSONResponse(payload)

    async def agent_workspace(request) -> JSONResponse:
        """Everything about one agent: prompt, blocks, tools, stages, examples."""
        if not core.chat.enabled:
            return _disabled()
        name = request.query_params.get("agent") or ""
        pack = core.agent_packs.get(name) if name else None
        if name and pack is None:
            active = ", ".join(info.name for info in core.agent_packs.active()) or "(none)"
            return JSONResponse(
                {"error": f"no agent named {name!r}", "hint": f"active: {active}"},
                status_code=404,
            )
        instructions = request.query_params.get("instructions") or ""
        if len(instructions) > 12_000:
            return JSONResponse({"error": "instructions are too long"}, status_code=400)
        from ifc_console.agents.workspace import describe, describe_plain_chat

        try:
            payload = (
                await describe(core, pack, instructions=instructions)
                if pack is not None
                else describe_plain_chat(core)
            )
        except Exception:
            log.exception("agent workspace for %s failed", name or "plain chat")
            return JSONResponse(
                {"error": f"could not describe agent {name!r}"}, status_code=500
            )
        if pack is not None:
            try:
                from ifc_console.agents.content import content_access_payload

                library = await asyncio.to_thread(_library_entries, core)
                content = content_access_payload(
                    _content_configuration(core, pack), library
                )
                content["enabled"] = True
                content["usable"] = "files" in pack.info.features
                payload["content"] = content
                payload["files"] = (
                    [row for row in content["files"] if row.get("allowed")]
                    if content["usable"]
                    else []
                )
            except Exception:
                log.warning("workspace file listing failed", exc_info=True)
                payload["files"] = []
                payload["content"] = {
                    "enabled": True,
                    "usable": "files" in pack.info.features,
                    "access": {"mode": "all", "paths": []},
                    "files": [],
                }
        else:
            payload["files"] = []
            payload["content"] = {
                "enabled": False,
                "usable": False,
                "access": {"mode": "none", "paths": []},
                "files": [],
            }
        return JSONResponse(payload)

    async def list_blocks(_request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        from ifc_console.agents.blocks import BLOCKS

        return JSONResponse(
            {"blocks": [block.info().model_dump(mode="json") for block in BLOCKS]}
        )

    async def list_content(request) -> JSONResponse:
        """The shared project content library and optional agent access state."""
        if not core.chat.enabled:
            return _disabled()
        name = request.query_params.get("agent")
        pack = core.agent_packs.get(name) if name else None
        if name and pack is None:
            return JSONResponse({"error": f"no agent named {name!r}"}, status_code=404)
        problem = None
        try:
            await asyncio.to_thread(core.agent_files.sync, core.project_knowledge)
        except Exception as exc:
            problem = str(exc)
            log.warning("agent content sync failed", exc_info=True)
        library = await asyncio.to_thread(_library_entries, core)
        payload: dict[str, Any] = {
            "directory": str(core.agent_files.directory),
            "files": library,
        }
        if pack is not None:
            from ifc_console.agents.content import content_access_payload

            payload.update(
                content_access_payload(_content_configuration(core, pack), library)
            )
            payload["usable"] = "files" in pack.info.features
        if problem:
            payload["problem"] = problem
        return JSONResponse(payload)

    async def set_content_access(request) -> JSONResponse:
        """Persist one agent's standing access to shared project content."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        name = body.get("agent")
        pack = core.agent_packs.get(name) if isinstance(name, str) else None
        if pack is None:
            return JSONResponse({"error": f"no agent named {name!r}"}, status_code=404)
        mode = body.get("mode")
        if mode not in {"all", "selected"}:
            return JSONResponse(
                {"error": "mode must be 'all' or 'selected'"}, status_code=400
            )
        raw_paths = body.get("paths", [])
        if not isinstance(raw_paths, list) or len(raw_paths) > 500:
            return JSONResponse(
                {"error": "paths must be a list with at most 500 items"}, status_code=400
            )
        from ifc_console.agents.content import normalize_content_path

        library = await asyncio.to_thread(_library_entries, core)
        available = {str(row.get("path") or "") for row in library}
        selected: list[str] = []
        for raw in raw_paths:
            path = normalize_content_path(raw)
            if path is None or path not in available:
                return JSONResponse(
                    {"error": f"unknown project content path {raw!r}"}, status_code=400
                )
            if path not in selected:
                selected.append(path)
        configured = None if mode == "all" else tuple(selected)
        blueprint = getattr(pack, "blueprint", None)
        if pack.info.kind == "custom" and blueprint is not None:
            pack = core.agent_packs.save_blueprint(
                blueprint.model_copy(update={"content_paths": configured})
            )
        else:
            await asyncio.to_thread(_content_store(core).set, pack.info.name, configured)
        _invalidate_agent_threads(core, pack.info.name)
        core.audit.record(
            "agent_content_access_saved",
            agent=pack.info.name,
            mode=mode,
            files=len(selected),
        )
        from ifc_console.agents.content import content_access_payload

        payload = content_access_payload(configured, library)
        payload["directory"] = str(core.agent_files.directory)
        return JSONResponse(payload)

    async def save_custom_agent(request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        from pydantic import ValidationError

        from ifc_console.agents.blueprints import AgentBlueprint, blueprint_name

        payload = dict(body)
        title = str(payload.get("title") or "").strip()
        requested_name = str(payload.get("name") or "").strip().lower()
        if not requested_name and title:
            base = blueprint_name(title)
            requested_name = base
            existing = {info.name for info in core.agent_packs.active()}
            counter = 2
            while requested_name in existing:
                suffix = f"-{counter}"
                requested_name = f"{base[:64-len(suffix)].rstrip('-')}{suffix}"
                counter += 1
        payload["name"] = requested_name
        try:
            blueprint = AgentBlueprint.model_validate(payload)
            pack = core.agent_packs.save_blueprint(blueprint)
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        state = _panel_state(core)
        for thread_id, thread in list(state.threads.items()):
            if thread.pack == pack.info.name:
                state.threads.pop(thread_id, None)
        core.audit.record(
            "custom_agent_saved",
            agent=pack.info.name,
            blocks=list(blueprint.blocks),
        )
        return JSONResponse(
            {"agent": pack.info.model_dump(mode="json")}, status_code=201
        )

    async def delete_custom_agent(request) -> JSONResponse:
        """Forget one project-local custom agent and any live thread using it."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        name = body.get("name")
        if not isinstance(name, str) or not name.startswith("custom-") or len(name) > 64:
            return JSONResponse({"error": "invalid custom agent name"}, status_code=400)
        if core.agent_packs.is_builtin(name):
            return JSONResponse({"error": "built-in agents cannot be deleted"}, status_code=403)
        removed = core.agent_packs.delete_blueprint(name)
        if not removed:
            return JSONResponse({"error": f"no custom agent named {name!r}"}, status_code=404)
        state = _panel_state(core)
        for thread_id, thread in list(state.threads.items()):
            if thread.pack == name:
                state.threads.pop(thread_id, None)
        core.audit.record("custom_agent_deleted", agent=name)
        return JSONResponse({"ok": True, "removed": name})

    async def delete_thread(request) -> JSONResponse:
        """Forget one local agent conversation in memory and on disk."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        thread_id = body.get("thread_id")
        if not _valid_panel_thread_id(thread_id):
            return JSONResponse({"error": "invalid panel thread id"}, status_code=400)
        state = _panel_state(core)
        async with state.lifecycle_lock:
            if state.clearing_threads or thread_id in state.deleting_threads:
                return JSONResponse(
                    {"error": "conversation reset is already in progress"}, status_code=409
                )
            state.deleting_threads.add(thread_id)
            state.thread_epochs[thread_id] = state.thread_epochs.get(thread_id, 0) + 1
            state.threads.pop(thread_id, None)
            active = set(state.active_streams.get(thread_id, ()))

        try:
            cancelled = await _cancel_stream_tasks(active)
            from ifc_console.agents.storage import JsonThreadStore

            removed = await JsonThreadStore(_thread_directory(core)).delete(thread_id)
        finally:
            async with state.lifecycle_lock:
                state.deleting_threads.discard(thread_id)
        core.audit.record(
            "agent_panel_thread_deleted",
            thread=thread_id,
            active_streams_cancelled=cancelled,
        )
        return JSONResponse({"ok": True, "removed": removed, "cancelled_runs": cancelled})

    def _store_for(thread_id: str) -> Any:
        """The store that actually holds one thread, durable or in memory."""
        thread = _panel_state(core).threads.get(thread_id)
        if thread is not None:
            return thread.agent.thread_store
        from ifc_console.agents.storage import JsonThreadStore

        return JsonThreadStore(_thread_directory(core))

    async def _reserve_thread(thread_id: Any) -> tuple[Any, set[asyncio.Task[Any]]]:
        """Validate a thread id and take a snapshot of its running streams."""
        if not _valid_panel_thread_id(thread_id):
            return JSONResponse({"error": "invalid panel thread id"}, status_code=400), set()
        state = _panel_state(core)
        async with state.lifecycle_lock:
            if state.clearing_threads or thread_id in state.deleting_threads:
                return (
                    JSONResponse(
                        {"error": "conversation reset is already in progress"}, status_code=409
                    ),
                    set(),
                )
            return None, set(state.active_streams.get(thread_id, ()))

    async def interrupt_run(request) -> JSONResponse:
        """Stop the run on one thread, and record in it that it was stopped.

        The browser aborting its fetch already cancels the generator, but only
        the server can tell the thread what happened, and the model's next turn
        reads that thread rather than the screen.
        """
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        thread_id = body.get("thread_id")
        refusal, active = await _reserve_thread(thread_id)
        if refusal is not None:
            return refusal
        store = _store_for(thread_id)
        cancelled = await _cancel_stream_tasks(active)
        noted = False
        if cancelled:
            from ifc_console.agents.models import AgentMessage

            history: list[Any] = []
            with suppress(Exception):
                history = list(await store.load(thread_id))
            if history:
                ran = _tool_calls_since_last_prompt(history)
                note = (
                    f"[The user stopped this run after {ran} tool call(s). Everything "
                    "above ran; nothing after it did. Do not assume the task finished.]"
                )
                last = history[-1]
                if last.role == "assistant" and not last.tool_calls:
                    history[-1] = last.model_copy(
                        update={"text": f"{last.text}\n\n{note}".strip()}
                    )
                else:
                    history.append(AgentMessage(role="assistant", text=note))
                with suppress(Exception):
                    await store.save(thread_id, history)
                    noted = True
        core.audit.record(
            "agent_panel_run_interrupted",
            thread=thread_id,
            active_streams_cancelled=cancelled,
        )
        return JSONResponse({"ok": True, "cancelled_runs": cancelled, "noted": noted})

    async def truncate_thread(request) -> JSONResponse:
        """Cut a conversation back to its first ``keep_turns`` user messages.

        Edit-and-resend and a real retry both need the failed attempt gone from
        the server thread; without this the model still reads it.
        """
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        thread_id = body.get("thread_id")
        keep_turns = body.get("keep_turns")
        if not isinstance(keep_turns, int) or isinstance(keep_turns, bool) or keep_turns < 0:
            return JSONResponse(
                {"error": "keep_turns must be a whole number of user turns"}, status_code=400
            )
        refusal, active = await _reserve_thread(thread_id)
        if refusal is not None:
            return refusal
        if active:
            return JSONResponse(
                {"error": "stop the run on this conversation before editing it"},
                status_code=409,
            )
        from ifc_console.agents.agent import _sealed

        store = _store_for(thread_id)
        try:
            messages = list(await store.load(thread_id))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        starts = _prompt_starts(messages)
        if keep_turns >= len(starts):
            return JSONResponse(
                {"ok": True, "removed": 0, "messages": len(messages), "turns": len(starts)}
            )
        removed = len(messages) - starts[keep_turns]
        kept = _sealed(messages[: starts[keep_turns]])
        await store.save(thread_id, kept)
        core.audit.record(
            "agent_panel_thread_truncated",
            thread=thread_id,
            kept_turns=keep_turns,
            removed_messages=removed,
        )
        return JSONResponse(
            {"ok": True, "removed": removed, "messages": len(kept), "turns": keep_turns}
        )

    async def clear_threads(_request) -> JSONResponse:
        """Cancel panel runs and remove every project-local panel conversation."""
        if not core.chat.enabled:
            return _disabled()
        state = _panel_state(core)
        async with state.lifecycle_lock:
            if state.clearing_threads:
                return JSONResponse(
                    {"error": "conversation reset is already in progress"}, status_code=409
                )
            state.clearing_threads = True
            state.reset_epoch += 1
            state.thread_epochs.clear()
            state.threads.clear()
            active = {
                task
                for tasks in state.active_streams.values()
                for task in tasks
            }

        cancelled = 0
        removed = 0
        try:
            cancelled = await _cancel_stream_tasks(active)
            removed = await asyncio.to_thread(
                _clear_owned_panel_thread_files, _thread_directory(core)
            )
        finally:
            async with state.lifecycle_lock:
                state.clearing_threads = False
        core.audit.record(
            "agent_panel_threads_cleared",
            removed_threads=removed,
            active_streams_cancelled=cancelled,
        )
        return JSONResponse(
            {"ok": True, "removed_threads": removed, "cancelled_runs": cancelled}
        )

    async def list_files(request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        name = request.query_params.get("agent") or ""
        pack = core.agent_packs.get(name)
        if pack is None or "files" not in pack.info.features:
            return JSONResponse(
                {"error": "this agent does not use project reference files"}, status_code=403
            )
        problem = None
        try:
            summary = await asyncio.to_thread(core.agent_files.sync, core.project_knowledge)
        except Exception as exc:
            log.warning("agent reference sync failed", exc_info=True)
            summary = {
                "changed": False,
                "directory": str(core.agent_files.directory),
                "files": await asyncio.to_thread(
                    core.agent_files.entries, core.project_knowledge.sources()
                ),
            }
            problem = str(exc)
        if problem:
            summary["problem"] = problem
        from ifc_console.agents.content import content_access_payload

        content = content_access_payload(
            _content_configuration(core, pack),
            await asyncio.to_thread(_library_entries, core),
        )
        summary["access"] = content["access"]
        summary["files"] = [row for row in content["files"] if row.get("allowed")]
        return JSONResponse(summary)

    async def stream(request) -> Response:
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

        name = body.get("agent")
        pack = core.agent_packs.get(name) if isinstance(name, str) else None
        if pack is None:
            active = ", ".join(info.name for info in core.agent_packs.active()) or "(none)"
            return JSONResponse(
                {"error": f"no active agent named {name!r}", "hint": f"active: {active}"},
                status_code=404,
            )
        prompt = body.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return JSONResponse({"error": "prompt must be non-empty text"}, status_code=400)
        if len(prompt) > _MAX_PROMPT_CHARS:
            return JSONResponse({"error": "prompt is too long"}, status_code=400)
        persist_history = body.get("persist_history", True)
        if not isinstance(persist_history, bool):
            return JSONResponse({"error": "persist_history must be true or false"}, status_code=400)
        additional_instructions = body.get("additional_instructions")
        if additional_instructions is not None and (
            not isinstance(additional_instructions, str) or len(additional_instructions) > 12_000
        ):
            return JSONResponse(
                {"error": "additional_instructions must be text up to 12000 characters"},
                status_code=400,
            )

        if "files" in pack.info.features:
            try:
                await asyncio.to_thread(core.agent_files.sync, core.project_knowledge)
            except Exception:
                # Retrieval tools will return a precise corpus error if the
                # prompt needs the references; model-only measurements can run.
                log.warning("agent reference sync failed before a run", exc_info=True)

        from ifc_console.chat.providers import PROVIDERS, ProviderError, validate_base_url

        settings = core.settings.chat
        provider_id = str(body.get("provider") or core.chat.provider or "openai").lower()
        provider = PROVIDERS.get(provider_id)
        if provider is None:
            return JSONResponse({"error": f"unknown provider {provider_id!r}"}, status_code=400)
        chosen = str(body.get("model") or core.chat.model or provider.suggested_model).strip()
        if not chosen:
            return JSONResponse(
                {"error": f"pick a model for {provider.label} first"}, status_code=400
            )
        raw_base = str(body.get("base_url") or core.chat.base_url or provider.base_url)
        try:
            base_url = validate_base_url(raw_base, local_only=settings.local_only)
        except ProviderError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

        from ifc_console.agents.providers import ProviderModel

        options: dict[str, Any] = {}
        for key in ("temperature", "top_p", "max_tokens"):
            value = body.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                options[key] = value
        model = ProviderModel(
            provider=provider.id,
            model=chosen,
            api_key=(
                str(body.get("api_key")).strip()
                if body.get("api_key")
                else core.chat.key_for(provider.id)
            ),
            base_url=base_url,
            local_only=settings.local_only,
            timeout_s=float(settings.timeout_s),
        )

        state = _panel_state(core)
        session_instructions = (additional_instructions or "").strip()
        content_paths = _content_configuration(core, pack)
        content_signature = (
            "content:all"
            if content_paths is None
            else "content:"
            + sha256("\x1f".join(sorted(content_paths)).encode("utf-8")).hexdigest()
        )
        autonomy = "auto" if core.ai_autonomy else "approval"
        signature = (
            provider.id,
            chosen,
            base_url,
            _pack_configuration_digest(pack),
            content_signature,
            "persistent" if persist_history else "ephemeral",
            sha256(session_instructions.encode("utf-8")).hexdigest()[:16],
            f"autonomy:{autonomy}",
        )
        requested_thread_id = (
            body.get("thread_id") if isinstance(body.get("thread_id"), str) else None
        )
        if requested_thread_id and not _valid_panel_thread_id(requested_thread_id):
            return JSONResponse({"error": "invalid panel thread id"}, status_code=400)
        async with state.lifecycle_lock:
            if state.clearing_threads or (
                requested_thread_id and requested_thread_id in state.deleting_threads
            ):
                return JSONResponse(
                    {"error": "conversation reset is already in progress"}, status_code=409
                )
            thread_id = requested_thread_id
            thread = state.threads.get(thread_id) if thread_id else None
            if thread is not None and (
                thread.pack != pack.info.name or thread.signature != signature
            ):
                # A browser pointer identifies one conversation configuration.
                # Reusing its disk id with another provider/model/agent would
                # load the old messages into a visually fresh configuration.
                # Fork instead; the original thread remains available from its
                # archived conversation.
                thread = None
                thread_id = None
            elif thread is None and thread_id and not _thread_matches_signature(
                thread_id, signature
            ):
                # A durable record has no in-memory signature after restart or
                # LRU eviction. Its signed id is the configuration fence.
                thread_id = None
            if thread is None:
                if not persist_history or not thread_id:
                    thread_id = _new_panel_thread_id(signature)
                try:
                    thread = await _build_thread(
                        core,
                        pack,
                        signature=signature,
                        model=model,
                        persistent=persist_history,
                        instructions=session_instructions,
                        model_label=f"{provider.id}/{chosen}",
                        # Asking the browser needs a run to ask on behalf of,
                        # so that handler is built per run. Left standing, the
                        # agent's deny-all default is the safe fallback.
                        approval_handler=(
                            AutoApprovalHandler(core) if autonomy == "auto" else None
                        ),
                        ask_before_acting=autonomy == "approval",
                    )
                except Exception:
                    log.exception("agent pack %s failed to build", pack.info.name)
                    return JSONResponse(
                        {"error": f"agent {pack.info.name!r} failed to start"},
                        status_code=500,
                    )
            assert thread_id is not None and thread is not None
            state.remember(thread_id, thread)
            run_reset_epoch = state.reset_epoch
            run_thread_epoch = state.thread_epochs.get(thread_id, 0)
        core.audit.record(
            "agent_panel_request",
            agent=pack.info.name,
            provider=provider.id,
            model=chosen,
            thread=thread_id,
        )

        attachments = body.get("attachments")
        requested_attachments = attachments if isinstance(attachments, list) else []
        from ifc_console.agents.content import managed_content_path, normalize_content_path

        available_content = {
            str(row.get("path") or "")
            for row in await asyncio.to_thread(_library_entries, core)
        }
        available_content.update(
            path
            for source in core.project_knowledge.sources()
            if (path := normalize_content_path(source.get("path"))) is not None
            and managed_content_path(path)
        )
        attachment_paths: list[str] = []
        for item in requested_attachments:
            path = normalize_content_path(item)
            if path is not None and path in available_content and path not in attachment_paths:
                attachment_paths.append(path)
            if len(attachment_paths) == 8:
                break
        images = await asyncio.to_thread(
            core.agent_files.prompt_images,
            attachment_paths,
            core.project_knowledge.sources(),
        )
        attachment_note = ""
        if attachment_paths:
            attachment_note = (
                "\n\nThe user attached these indexed project references to this message: "
                + ", ".join(attachment_paths)
                + ". Inspect them with the document/image tools when relevant."
            )
        # "this wall" usually means the clicked one; carrying the selection
        # with the message saves the get_viewer_selection round that answered
        # it, while the tool stays there for anything richer.
        hub = core.viewer_hub
        if (
            hub.connected
            and hub.selection
            and hub.selection_model_id in (None, core.session.model_id)
        ):
            shown = list(hub.selection)[:10]
            more = len(hub.selection) - len(shown)
            attachment_note += (
                f"\n\n[Viewer context: the user has {len(hub.selection)} element(s) "
                "selected: " + ", ".join(shown)
                + (f" and {more} more" if more > 0 else "")
                + ". 'This' or 'selected' in the message means these GlobalIds.]"
            )

        async def events():
            stream_task = asyncio.current_task()
            async with state.lifecycle_lock:
                stale = (
                    stream_task is None
                    or state.clearing_threads
                    or thread_id in state.deleting_threads
                    or state.reset_epoch != run_reset_epoch
                    or state.thread_epochs.get(thread_id, 0) != run_thread_epoch
                )
                if not stale and stream_task is not None:
                    state.active_streams.setdefault(thread_id, set()).add(stream_task)
            if stale:
                yield _sse(
                    {
                        "type": "error",
                        "text": "conversation history was reset before this run started",
                    }
                )
                yield _sse({"type": "done"})
                return
            # The stance can change between runs on the same thread, so the
            # handler is chosen per run. It is passed into the run rather than
            # stored on the agent, which a concurrent run would overwrite.
            approval_handler = (
                AutoApprovalHandler(core)
                if autonomy == "auto"
                else PanelApprovalHandler(state, owner=stream_task)
            )
            access = (
                thread.content_gate.temporary(attachment_paths)
                if thread.content_gate is not None
                else nullcontext()
            )
            try:
                with access:
                    yield _sse({"type": "thread", "id": thread_id, "agent": pack.info.name})
                    async for event in thread.agent.stream(
                        prompt.strip() + attachment_note,
                        thread_id=thread_id,
                        options=options,
                        images=images,
                        approval_handler=approval_handler,
                    ):
                        for payload in _event_payloads(event):
                            yield _sse(payload)
            except asyncio.CancelledError:
                raise
            except Exception:  # the panel must always finish cleanly
                log.exception("agent panel stream failed")
                yield _sse({"type": "error", "text": "internal agent error"})
            finally:
                state.deny_owned(stream_task)
                if stream_task is not None:
                    async with state.lifecycle_lock:
                        active = state.active_streams.get(thread_id)
                        if active is not None:
                            active.discard(stream_task)
                            if not active:
                                state.active_streams.pop(thread_id, None)
            yield _sse({"type": "done"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def approve(request) -> JSONResponse:
        """Answer one pending approval. The agent is blocked until this lands."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return JSONResponse({"error": "request_id is required"}, status_code=400)
        approved = body.get("approved")
        if not isinstance(approved, bool):
            return JSONResponse({"error": "approved must be true or false"}, status_code=400)
        reason = body.get("reason")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 400):
            return JSONResponse(
                {"error": "reason must be text up to 400 characters"}, status_code=400
            )
        state = _panel_state(core)
        entry = state.pending_approvals.get(request_id)
        pending = entry[1] if entry is not None else None
        if pending is None or pending.done():
            # The run ended, or this decision already landed. Saying so is
            # better than silently accepting a decision nothing is waiting for.
            return JSONResponse(
                {"error": "that request is no longer waiting for a decision"},
                status_code=409,
            )
        from ifc_console.agents.models import ApprovalDecision

        core.audit.record(
            "agent_approval_decided",
            request=request_id,
            approved=approved,
        )
        pending.set_result(
            ApprovalDecision(
                approved=approved,
                decided_by="chat-panel",
                reason=(reason or "").strip(),
            )
        )
        return JSONResponse({"ok": True, "approved": approved})

    async def upload(request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        from ifc_console.knowledge.ingest import SUPPORTED_SUFFIXES

        library_upload = request.url.path.endswith("/content/upload")
        name = request.query_params.get("agent") or ""
        pack = core.agent_packs.get(name) if name else None
        if not library_upload and (pack is None or "files" not in pack.info.features):
            return JSONResponse(
                {"error": "this agent does not accept uploads"}, status_code=403
            )
        from pathlib import Path

        raw_name = Path(request.query_params.get("name") or "").name
        suffix = Path(raw_name).suffix.lower()
        if not raw_name or suffix not in SUPPORTED_SUFFIXES:
            return JSONResponse(
                {"error": f"pass name=<file> ending in one of {', '.join(SUPPORTED_SUFFIXES)}"},
                status_code=400,
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "file is larger than 25 MB"}, status_code=413)
        data = await request.body()
        if not data:
            return JSONResponse({"error": "the file is empty"}, status_code=400)
        if len(data) > _MAX_UPLOAD_BYTES:
            return JSONResponse({"error": "file is larger than 25 MB"}, status_code=413)

        from ifc_console.core.results import ToolError

        try:
            save = (
                core.agent_files.save_upload
                if library_upload
                else core.agent_files.save_turn_upload
            )
            target = await asyncio.to_thread(save, raw_name, data)
        except ToolError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=400
            )
        relative_target = target.relative_to(core.store.project_dir).as_posix()
        try:
            report = await asyncio.to_thread(core.project_knowledge.ingest, [target])
        except ToolError as exc:
            core.audit.record(
                "agent_content_upload" if library_upload else "agent_panel_upload",
                agent=name,
                file=str(target),
                indexed=False,
                problem=exc.code,
            )
            return JSONResponse(
                {
                    "saved": str(target),
                    "attachment": {"path": relative_target, "media": "image" if suffix in {".png", ".jpg", ".jpeg"} else "document"},
                    "indexed": False,
                    "error": exc.message,
                    "code": exc.code,
                    "hint": exc.hint,
                    "files": await asyncio.to_thread(
                        core.agent_files.entries, core.project_knowledge.sources()
                    ),
                },
                status_code=202,
            )
        core.audit.record(
            "agent_content_upload" if library_upload else "agent_panel_upload",
            agent=name,
            file=str(target),
            records=report["records"],
        )
        summary: dict[str, Any] = {
            "saved": str(target),
            "attachment": {"path": relative_target, "media": "image" if suffix in {".png", ".jpg", ".jpeg"} else "document"},
            "indexed": True,
            "documents": report["documents"],
            "records": report["records"],
            "files": await asyncio.to_thread(
                core.agent_files.entries, core.project_knowledge.sources()
            ),
        }
        for key in ("instruction_like_chunks", "without_text", "note"):
            if key in report:
                summary[key] = report[key]
        return JSONResponse(summary)

    async def skills_import(request) -> JSONResponse:
        """Drop externally written .md skills into the project skill store."""
        if not core.chat.enabled:
            return _disabled()
        from pathlib import Path

        from ifc_console.agents.skills import MAX_SKILL_BYTES, AgentSkillStore
        from ifc_console.core.results import ToolError

        raw_name = Path(request.query_params.get("name") or "").name
        if not raw_name or Path(raw_name).suffix.lower() not in (".md", ".markdown"):
            return JSONResponse(
                {"error": "pass name=<file> ending in .md"}, status_code=400
            )
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_SKILL_BYTES:
            return JSONResponse({"error": "skill is larger than 64 KB"}, status_code=413)
        data = await request.body()
        if not data:
            return JSONResponse({"error": "the file is empty"}, status_code=400)
        store = AgentSkillStore(core.store.project_dir)
        try:
            row = await asyncio.to_thread(store.import_file, raw_name, data)
        except ToolError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=400
            )
        core.audit.record("skill_import", file=raw_name, name=row["name"], path=row["path"])
        return JSONResponse(
            {"imported": row, "skills": await asyncio.to_thread(store.entries)}
        )

    return [
        Route("/api/agents", list_agents, methods=["GET"]),
        Route("/api/agents/blocks", list_blocks, methods=["GET"]),
        Route("/api/agents/workspace", agent_workspace, methods=["GET"]),
        Route("/api/agents/content", list_content, methods=["GET"]),
        Route("/api/agents/content/access", set_content_access, methods=["POST"]),
        Route("/api/agents/content/upload", upload, methods=["POST"]),
        Route("/api/agents/capabilities", list_capabilities, methods=["GET"]),
        Route("/api/agents/custom", save_custom_agent, methods=["POST"]),
        Route("/api/agents/custom/delete", delete_custom_agent, methods=["POST"]),
        Route("/api/agents/thread/delete", delete_thread, methods=["POST"]),
        Route("/api/agents/thread/truncate", truncate_thread, methods=["POST"]),
        Route("/api/agents/threads/clear", clear_threads, methods=["POST"]),
        Route("/api/agents/interrupt", interrupt_run, methods=["POST"]),
        Route("/api/agents/files", list_files, methods=["GET"]),
        Route("/api/agents/stream", stream, methods=["POST"]),
        Route("/api/agents/approve", approve, methods=["POST"]),
        Route("/api/agents/upload", upload, methods=["POST"]),
        Route("/api/agents/skills/import", skills_import, methods=["POST"]),
    ]


__all__ = ["AgentPanelState", "build_agent_panel_routes", "panel_limits", "panel_runtime"]
