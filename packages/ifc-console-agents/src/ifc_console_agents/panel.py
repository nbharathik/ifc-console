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
import os
import re
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

from ifc_console_agents.agent import Agent
from ifc_console_agents.files import MAX_REFERENCE_BYTES
from ifc_console_agents.models import AgentLimits

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.agents")

_MAX_THREADS = 20
_MAX_THREAD_RECORD_BYTES = 8 * 1024 * 1024
_MAX_PROMPT_CHARS = 100_000
_MAX_UPLOAD_BYTES = MAX_REFERENCE_BYTES
# The panel prints the call arguments beside the tool, so they have to be
# whole enough to read. Still bounded: a model can pass a long selector.
_MAX_ARGUMENT_CHARS = 2000
_WORKFLOW_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


async def _read_limited_body(request: Any, limit: int) -> bytes | None:
    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > limit:
            return None
        data.extend(chunk)
    return bytes(data)


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
            victim = next(
                (candidate for candidate in self.threads if not self.active_streams.get(candidate)),
                None,
            )
            if victim is None:
                break
            self.threads.pop(victim)


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
        from ifc_console_agents.models import ApprovalDecision

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
        from ifc_console_agents.models import ApprovalDecision

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
    return not ("read" in tags and not {"write", "preview", "destructive"} & tags)


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


def _project_storage_key(project_dir: Path) -> str:
    """Stable, non-identifying key for machine-local state for one project."""
    canonical = os.path.normcase(str(project_dir.expanduser().resolve()))
    return sha256(canonical.encode("utf-8")).hexdigest()[:24]


def _thread_directory(core: AppCore) -> Path:
    """Private panel history under the user's IFC Console home.

    The project path scopes conversations without putting prompts, tool output,
    or model context inside a repository. Only its hash appears on disk.
    """
    return (
        core.store.home.expanduser().resolve()
        / "agents"
        / "projects"
        / _project_storage_key(core.store.project_dir)
        / "threads"
    )


def _legacy_thread_directory(core: AppCore) -> Path:
    return core.store.project_dir.expanduser().resolve() / ".ifc-console" / "agents" / "threads"


def migrate_legacy_panel_threads(core: AppCore) -> int:
    """Move IFC Console-owned legacy project threads into private user state.

    Older releases mixed panel conversations with versionable project assets.
    Migrate only records whose ``panel-*`` identity and hashed filename prove
    they belong to this panel; unknown SDK/embedder files are left untouched.
    """
    source = _legacy_thread_directory(core)
    if not source.is_dir():
        return 0
    # A cloned project must not be able to point this migration at files
    # elsewhere on the machine through a repository-controlled symlink.
    try:
        if source.is_symlink() or source.resolve() != source.absolute():
            log.warning("refusing to migrate symlinked Agent thread directory %s", source)
            return 0
    except OSError:
        return 0
    target = _thread_directory(core)
    moved = 0
    for path in source.glob("*.json"):
        try:
            if path.is_symlink() or path.stat().st_size > _MAX_THREAD_RECORD_BYTES:
                continue
            raw = path.read_bytes()
            payload = json.loads(raw)
        except (OSError, UnicodeDecodeError, ValueError):
            continue
        thread_id = payload.get("thread_id") if isinstance(payload, dict) else None
        if not _valid_panel_thread_id(thread_id):
            continue
        expected = sha256(thread_id.encode("utf-8")).hexdigest() + ".json"
        if path.name != expected:
            continue

        target.mkdir(parents=True, exist_ok=True)
        destination = target / path.name
        if destination.exists():
            try:
                if destination.read_bytes() == raw:
                    path.unlink()
                    moved += 1
            except OSError:
                pass
            continue

        temporary = destination.with_suffix(f".{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            with suppress(OSError):
                destination.chmod(0o600)
            path.unlink()
            moved += 1
        except OSError as exc:
            log.warning("could not migrate legacy Agent thread %s: %s", path.name, exc)
        finally:
            with suppress(OSError):
                temporary.unlink()

    # Remove only directories made empty by the migration. Project-owned
    # references, agents, skills, settings, and unknown thread records remain.
    for directory in (source, source.parent, source.parent.parent):
        with suppress(OSError):
            directory.rmdir()
    return moved


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
    from ifc_console_agents.agent import IMAGE_TURN_PREFIX

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
        from ifc_console_agents.content import AgentContentAccessStore

        state.content_store = AgentContentAccessStore(core.store.project_dir)
    return state.content_store


def _content_configuration(core: AppCore, pack: Any) -> tuple[str, ...] | None:
    from ifc_console_agents.content import configured_paths

    return configured_paths(pack, _content_store(core))


def _library_entries(core: AppCore) -> list[dict[str, Any]]:
    return core.agent_files.library_entries(core.project_knowledge.sources())


async def _invalidate_agent_threads(core: AppCore, name: str) -> int:
    state = _panel_state(core)
    async with state.lifecycle_lock:
        thread_ids = [
            thread_id for thread_id, thread in state.threads.items() if thread.pack == name
        ]
        active: set[asyncio.Task[Any]] = set()
        for thread_id in thread_ids:
            state.threads.pop(thread_id, None)
            state.thread_epochs[thread_id] = state.thread_epochs.get(thread_id, 0) + 1
            active.update(state.active_streams.get(thread_id, ()))
    return await _cancel_stream_tasks(active)


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


@dataclass(frozen=True)
class ResolvedModel:
    """One validated provider selection, ready to build an agent with."""

    model: Any
    provider_id: str
    model_id: str
    base_url: str
    options: dict[str, Any]
    capabilities: dict[str, bool | None]


def resolve_provider_model(
    core: AppCore, body: Mapping[str, Any]
) -> tuple[ResolvedModel | None, JSONResponse | None]:
    """Turn a request body into a provider model, or the error to return.

    Chat runs and workflow runs pick a provider the same way, including the
    local-only base URL check, so that decision lives in one place.
    """
    from ifc_console_agents.chat.providers import PROVIDERS, ProviderError, validate_base_url
    from ifc_console_agents.providers import ProviderModel

    settings = core.settings.chat
    provider_id = str(body.get("provider") or core.chat.provider or "openai").lower()
    provider = PROVIDERS.get(provider_id)
    if provider is None:
        return None, JSONResponse({"error": f"unknown provider {provider_id!r}"}, status_code=400)
    chosen = str(body.get("model") or core.chat.model or provider.suggested_model).strip()
    if not chosen:
        return None, JSONResponse(
            {"error": f"pick a model for {provider.label} first"}, status_code=400
        )
    raw_base = str(body.get("base_url") or core.chat.base_url or provider.base_url)
    try:
        base_url = validate_base_url(raw_base, local_only=settings.local_only)
    except ProviderError as exc:
        return None, JSONResponse({"error": str(exc)}, status_code=400)

    options: dict[str, Any] = {}
    for key in ("temperature", "top_p", "max_tokens"):
        value = body.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            options[key] = value
    capabilities: dict[str, bool | None] = {}
    for key in ("tools_supported", "vision_supported"):
        value = body.get(key)
        if value is not None and not isinstance(value, bool):
            return None, JSONResponse(
                {"error": f"{key} must be true, false, or omitted"}, status_code=400
            )
        capabilities[key] = value
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
        tools_supported=capabilities["tools_supported"],
        vision_supported=capabilities["vision_supported"],
    )
    return (
        ResolvedModel(
            model=model,
            provider_id=provider.id,
            model_id=chosen,
            base_url=base_url,
            options=options,
            capabilities=capabilities,
        ),
        None,
    )


def _disabled() -> JSONResponse:
    return JSONResponse(
        {
            "error": "chat_disabled",
            "hint": "type /agent in the ifc-console terminal to open the Agent workspace",
        },
        status_code=404,
    )


def _proposed_change(tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any] | None:
    """What a write tool is about to do, from its arguments alone.

    The reviewer decides before the tool runs, so the readable card has to be
    built from the request. Reading raw JSON is not review.
    """
    from ifc_console.ifc.ai_provenance import (
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
        from ifc_console_agents.chat.agent import tool_event

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
        from ifc_console_agents.proposals import PROPOSAL_TOOLS

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
        return [{"type": "usage", "in": event.usage.input_tokens, "out": event.usage.output_tokens}]
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
            viewer=core.viewer_supported,
            instructions=instructions,
            model_label=model_label,
        )
    except TypeError:
        # A pack registered by an embedding application may predate the
        # instructions parameter; its prompt simply stays fixed.
        agent = await pack.build(panel_runtime(core), model=model, viewer=core.viewer_supported)
    if persistent:
        from ifc_console_agents.storage import JsonThreadStore

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

    from ifc_console_agents.content import AgentContentGate

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
        from ifc_console_agents.environment import report

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
        from ifc_console_agents.workspace import describe, describe_plain_chat

        try:
            payload = (
                await describe(core, pack, instructions=instructions)
                if pack is not None
                else describe_plain_chat(core)
            )
        except Exception:
            log.exception("agent workspace for %s failed", name or "plain chat")
            return JSONResponse({"error": f"could not describe agent {name!r}"}, status_code=500)
        if pack is not None:
            try:
                from ifc_console_agents.content import content_access_payload

                library = await asyncio.to_thread(_library_entries, core)
                content = content_access_payload(_content_configuration(core, pack), library)
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
        from ifc_console_agents.blocks import BLOCKS

        return JSONResponse({"blocks": [block.info().model_dump(mode="json") for block in BLOCKS]})

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
            from ifc_console_agents.content import content_access_payload

            payload.update(content_access_payload(_content_configuration(core, pack), library))
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
            return JSONResponse({"error": "mode must be 'all' or 'selected'"}, status_code=400)
        raw_paths = body.get("paths", [])
        if not isinstance(raw_paths, list) or len(raw_paths) > 500:
            return JSONResponse(
                {"error": "paths must be a list with at most 500 items"}, status_code=400
            )
        from ifc_console_agents.content import normalize_content_path

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
        cancelled = await _invalidate_agent_threads(core, pack.info.name)
        core.audit.record(
            "agent_content_access_saved",
            agent=pack.info.name,
            mode=mode,
            files=len(selected),
            active_streams_cancelled=cancelled,
        )
        from ifc_console_agents.content import content_access_payload

        payload = content_access_payload(configured, library)
        payload["directory"] = str(core.agent_files.directory)
        payload["cancelled_runs"] = cancelled
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

        from ifc_console_agents.blueprints import AgentBlueprint, blueprint_name

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
                requested_name = f"{base[: 64 - len(suffix)].rstrip('-')}{suffix}"
                counter += 1
        payload["name"] = requested_name
        try:
            blueprint = AgentBlueprint.model_validate(payload)
            pack = core.agent_packs.save_blueprint(blueprint)
        except (ValidationError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        cancelled = await _invalidate_agent_threads(core, pack.info.name)
        core.audit.record(
            "custom_agent_saved",
            agent=pack.info.name,
            blocks=list(blueprint.blocks),
            active_streams_cancelled=cancelled,
        )
        return JSONResponse(
            {
                "agent": pack.info.model_dump(mode="json"),
                "cancelled_runs": cancelled,
            },
            status_code=201,
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
        cancelled = await _invalidate_agent_threads(core, name)
        core.audit.record(
            "custom_agent_deleted",
            agent=name,
            active_streams_cancelled=cancelled,
        )
        return JSONResponse({"ok": True, "removed": name, "cancelled_runs": cancelled})

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
            from ifc_console_agents.storage import JsonThreadStore

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
        from ifc_console_agents.storage import JsonThreadStore

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
            from ifc_console_agents.models import AgentMessage

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
                    history[-1] = last.model_copy(update={"text": f"{last.text}\n\n{note}".strip()})
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
        from ifc_console_agents.agent import _sealed

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
            active = {task for tasks in state.active_streams.values() for task in tasks}

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
        return JSONResponse({"ok": True, "removed_threads": removed, "cancelled_runs": cancelled})

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
        from ifc_console_agents.content import content_access_payload

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
        workflow_name = body.get("workflow")
        if workflow_name is not None and (
            not isinstance(workflow_name, str) or not _WORKFLOW_NAME.fullmatch(workflow_name)
        ):
            return JSONResponse({"error": "workflow must be a workflow name"}, status_code=400)
        if not isinstance(prompt, str) or (not prompt.strip() and not workflow_name):
            return JSONResponse({"error": "prompt must be non-empty text"}, status_code=400)
        if len(prompt) > _MAX_PROMPT_CHARS:
            return JSONResponse({"error": "prompt is too long"}, status_code=400)
        # A workflow attached to the conversation: its prompt, settings, scope,
        # and procedure become standing instructions on this thread, so the
        # same agent, tools, and approvals answer it turn after turn.
        workflow_spec = None
        workflow_scope: dict[str, Any] = {}
        workflow_text = ""
        if workflow_name:
            from ifc_console.core.results import ToolError

            from ifc_console_agents.workflow_runner import WorkflowRunError, viewer_scope
            from ifc_console_agents.workflows import (
                WorkflowRegistry,
                chat_instructions,
                chat_task_prompt,
            )

            registry = WorkflowRegistry(core.store.project_dir)
            try:
                workflow_spec = await asyncio.to_thread(registry.get, workflow_name)
            except ToolError as exc:
                status = 404 if exc.code == "NOT_FOUND" else 400
                return JSONResponse(
                    {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=status
                )
            requested_scope = str(body.get("workflow_scope") or "model").strip()
            if requested_scope not in {"model", "selection"}:
                return JSONResponse(
                    {"error": "workflow_scope must be model or selection"}, status_code=400
                )
            scope_mode = (
                "model"
                if workflow_spec.scope == "model"
                else "selection"
                if workflow_spec.scope == "selection"
                else requested_scope
            )
            try:
                workflow_scope = viewer_scope(core, scope_mode)
            except WorkflowRunError as exc:
                return JSONResponse(
                    {
                        "error": str(exc),
                        "hint": "Select one or more elements in the 3D view, then run again.",
                    },
                    status_code=400,
                )
            workflow_text = chat_instructions(workflow_spec, scope=workflow_scope)
            if not prompt.strip():
                prompt = chat_task_prompt(workflow_spec)
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

        resolved, error = resolve_provider_model(core, body)
        if error is not None:
            return error
        assert resolved is not None
        model = resolved.model
        base_url = resolved.base_url
        chosen = resolved.model_id
        options = resolved.options
        capabilities = resolved.capabilities

        state = _panel_state(core)
        session_instructions = "\n\n".join(
            part for part in ((additional_instructions or "").strip(), workflow_text) if part
        )
        content_paths = _content_configuration(core, pack)
        content_signature = (
            "content:all"
            if content_paths is None
            else "content:" + sha256("\x1f".join(sorted(content_paths)).encode("utf-8")).hexdigest()
        )
        autonomy = "auto" if core.ai_autonomy else "approval"
        signature = (
            resolved.provider_id,
            chosen,
            base_url,
            _pack_configuration_digest(pack),
            content_signature,
            "persistent" if persist_history else "ephemeral",
            sha256(session_instructions.encode("utf-8")).hexdigest()[:16],
            f"autonomy:{autonomy}",
            f"tools:{capabilities['tools_supported']}",
            f"vision:{capabilities['vision_supported']}",
            f"workflow:{workflow_spec.name if workflow_spec else ''}",
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
            elif (
                thread is None and thread_id and not _thread_matches_signature(thread_id, signature)
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
                        model_label=f"{resolved.provider_id}/{chosen}",
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
            provider=resolved.provider_id,
            model=chosen,
            thread=thread_id,
            workflow=workflow_spec.name if workflow_spec else "",
        )

        attachments = body.get("attachments")
        requested_attachments = attachments if isinstance(attachments, list) else []
        from ifc_console_agents.content import managed_content_path, normalize_content_path

        available_content = {
            str(row.get("path") or "") for row in await asyncio.to_thread(_library_entries, core)
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
        # "this wall" usually means the clicked one; carrying every IFC tab's
        # model-scoped selection saves the get_viewer_selection round while
        # keeping same-looking GlobalIds attributable to the right file.
        hub = core.viewer_hub
        selection_rows = hub.selection_rows() if hub.connected else []
        if selection_rows:
            selected_models = []
            for row in selection_rows:
                guids = list(row["guids"])
                shown = guids[:10]
                more = len(guids) - len(shown)
                selected_models.append(
                    f"{row['model']} (model_id={row['model_id']}): "
                    + ", ".join(shown)
                    + (f" and {more} more" if more > 0 else "")
                )
            attachment_note += (
                "\n\n[Viewer context: the user has selections in "
                f"{len(selection_rows)} IFC file(s): "
                + "; ".join(selected_models)
                + ". 'This' or 'selected' in the message means these model-scoped GlobalIds.]"
            )
        # Hand measurements the same treatment: "these measurements" must
        # resolve without a guessing round.
        measured_items, _ = hub.latest_measurements() if hub.connected else ([], None)
        if measured_items:
            attachment_note += (
                f"\n\n[Viewer context: {len(measured_items)} measurement(s) are on "
                "screen; get_viewer_measurements returns them with element anchors.]"
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
                    if workflow_spec is not None:
                        # What the conversation is standing on, so the panel
                        # can show the exact text the model was given.
                        yield _sse(
                            {
                                "type": "workflow_context",
                                "workflow": workflow_spec.name,
                                "title": workflow_spec.title,
                                "scope": workflow_scope.get("mode", "model"),
                                "selected": int(workflow_scope.get("count") or 0),
                                "instructions": workflow_text,
                            }
                        )
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
        from ifc_console_agents.models import ApprovalDecision

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
            return JSONResponse({"error": "this agent does not accept uploads"}, status_code=403)
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
        data = await _read_limited_body(request, _MAX_UPLOAD_BYTES)
        if data is None:
            return JSONResponse({"error": "file is larger than 25 MB"}, status_code=413)
        if not data:
            return JSONResponse({"error": "the file is empty"}, status_code=400)

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
                    "attachment": {
                        "path": relative_target,
                        "media": "image" if suffix in {".png", ".jpg", ".jpeg"} else "document",
                    },
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
            "attachment": {
                "path": relative_target,
                "media": "image" if suffix in {".png", ".jpg", ".jpeg"} else "document",
            },
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

        from ifc_console.core.results import ToolError

        from ifc_console_agents.skills import MAX_SKILL_BYTES, AgentSkillStore

        raw_name = Path(request.query_params.get("name") or "").name
        if not raw_name or Path(raw_name).suffix.lower() not in (".md", ".markdown"):
            return JSONResponse({"error": "pass name=<file> ending in .md"}, status_code=400)
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_SKILL_BYTES:
            return JSONResponse({"error": "skill is larger than 64 KB"}, status_code=413)
        data = await _read_limited_body(request, MAX_SKILL_BYTES)
        if data is None:
            return JSONResponse({"error": "skill is larger than 64 KB"}, status_code=413)
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
        return JSONResponse({"imported": row, "skills": await asyncio.to_thread(store.entries)})

    async def geometry_review(request) -> JSONResponse:
        """Run the bounded, read-only v2 geometry view for explicit targets."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        model = body.get("model")
        global_ids = body.get("global_ids")
        if not isinstance(model, str) or not model.strip():
            return JSONResponse(
                {"error": "model is required", "hint": "Pass the viewer selection model_id."},
                status_code=400,
            )
        if (
            not isinstance(global_ids, list)
            or len(global_ids) != 1
            or any(not isinstance(item, str) or not item for item in global_ids)
        ):
            return JSONResponse(
                {
                    "error": "global_ids must contain exactly one element GlobalId",
                    "hint": "Select one object for the workspace geometry review.",
                },
                status_code=400,
            )
        detail = str(body.get("detail") or "compact")
        if detail not in {"compact", "standard"}:
            return JSONResponse({"error": "detail must be compact or standard"}, status_code=400)
        measurement_set = str(body.get("measurement_set") or "standard")
        if measurement_set not in {"standard", "profile", "envelope", "fabrication"}:
            return JSONResponse({"error": "invalid measurement_set"}, status_code=400)

        from ifc_console.sdk import AsyncWorkbench, IfcConsoleError

        try:
            payload = await AsyncWorkbench(core).call(
                "analyze_element_geometry",
                global_ids=list(dict.fromkeys(global_ids)),
                model=model.strip(),
                detail=detail,
                measurement_set=measurement_set,
                frame="semantic",
                station_strategy="auto",
                include_alternatives=detail == "standard",
                include_sections=detail == "standard",
            )
        except IfcConsoleError as exc:
            return JSONResponse(
                {"error": exc.message, "code": exc.code, "hint": exc.hint},
                status_code=400,
            )
        core.audit.record(
            "geometry_workspace_review",
            model=model.strip(),
            elements=len(global_ids),
            detail=detail,
            measurement_set=measurement_set,
        )
        payload["review"] = {
            "read_only": True,
            "frame": "semantic",
            "station_strategy": "auto",
            "proposal_action": "separate confirmation required",
        }
        return JSONResponse(payload)

    async def skill_dry_run(request) -> JSONResponse:
        """Preview one structured measurement skill without proposing properties."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        name = body.get("name")
        model = body.get("model")
        selector = body.get("selector")
        global_ids = body.get("global_ids")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "name is required"}, status_code=400)
        if not isinstance(model, str) or not model.strip():
            return JSONResponse(
                {"error": "model is required", "hint": "Pass the viewer selection model_id."},
                status_code=400,
            )
        has_selector = isinstance(selector, str) and bool(selector.strip())
        has_ids = isinstance(global_ids, list) and bool(global_ids)
        if has_selector == has_ids:
            return JSONResponse(
                {"error": "pass exactly one of selector or global_ids"}, status_code=400
            )
        if has_ids and (
            len(global_ids) > 25
            or any(not isinstance(item, str) or not item for item in global_ids)
        ):
            return JSONResponse(
                {
                    "error": "global_ids must contain 1 to 25 element GlobalIds",
                    "hint": "Page larger reviews through apply_measurement_skill directly.",
                },
                status_code=400,
            )
        limit = body.get("limit", 25)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 25:
            return JSONResponse({"error": "limit must be from 1 to 25"}, status_code=400)
        confidence = body.get("minimum_confidence")
        if confidence is not None and confidence not in {"low", "medium", "high", "exact"}:
            return JSONResponse({"error": "invalid minimum_confidence"}, status_code=400)

        from ifc_console.sdk import AsyncWorkbench, IfcConsoleError

        arguments: dict[str, Any] = {
            "name": name.strip(),
            "model": model.strip(),
            "dry_run": True,
            "limit": limit,
            "include_evidence": body.get("include_evidence") is True,
        }
        if confidence is not None:
            arguments["minimum_confidence"] = confidence
        if has_selector:
            arguments["selector"] = selector.strip()
        else:
            arguments["global_ids"] = list(dict.fromkeys(global_ids))
        try:
            payload = await AsyncWorkbench(core).call("apply_measurement_skill", **arguments)
        except IfcConsoleError as exc:
            status = 404 if exc.code == "NOT_FOUND" else 400
            return JSONResponse(
                {"error": exc.message, "code": exc.code, "hint": exc.hint},
                status_code=status,
            )
        core.audit.record(
            "measurement_skill_dry_run",
            name=name.strip(),
            model=model.strip(),
            selector=selector.strip() if has_selector else None,
            elements=len(arguments.get("global_ids") or []),
        )
        payload["review"] = {
            "dry_run": True,
            "read_only": True,
            "proposal_action": "separate confirmation required",
        }
        return JSONResponse(payload)

    async def skills_record(request) -> JSONResponse:
        """Save the viewer's current measurements as a reusable skill.

        The skill stores intent: each value is matched against the measured
        element's analyzed dimensions, so an agent can repeat the pattern on
        similar elements even when their shapes differ.
        """
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        name = body.get("name")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "name is required"}, status_code=400)
        notes = body.get("notes")
        if notes is not None and (not isinstance(notes, str) or len(notes) > 2000):
            return JSONResponse(
                {"error": "notes must be text up to 2000 characters"}, status_code=400
            )
        overwrite = body.get("overwrite") is True

        hub = core.viewer_hub
        source = hub.measurement_source() if hub.connected else None
        items = list(source.measurements) if source is not None else []
        measured_at = source.measured_at if source is not None else None
        if not items:
            return JSONResponse(
                {
                    "error": "no measurements to record",
                    "hint": "measure in the viewer first; the newest tab's list is saved",
                },
                status_code=409,
            )
        model_id = source.measurement_model_id if source is not None else None
        if not isinstance(model_id, str) or not model_id:
            return JSONResponse(
                {
                    "error": "the measurement source has no model id",
                    "hint": "Reopen the measured model in the viewer and measure again.",
                },
                status_code=409,
            )

        from ifc_console.core.results import ToolError

        try:
            source_session = core.resolve_session(model_id)
        except ToolError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=409
            )

        from ifc_console_agents.recording import build_recorded_skill, measured_guids

        guids = measured_guids(items)
        if len(guids) > 25:
            return JSONResponse(
                {
                    "error": f"the measurements reference {len(guids)} elements; the limit is 25",
                    "hint": "Clear unrelated measurements, then record one pattern at a time.",
                },
                status_code=422,
            )
        analysis = None
        analysis_failures: list[dict[str, str]] = []
        if guids:
            from ifc_console.sdk import AsyncWorkbench

            # Read-only probe. The workbench wraps the live core and is never
            # closed here: closing it would close the console.
            revision = {
                "model_id": model_id,
                "fingerprint": source_session.fingerprint,
                "revision": source_session.revision,
            }
            analysis = {"model_revision": revision, "elements": []}
            workbench = AsyncWorkbench(core)
            # A standard v2 element can approach the operation envelope's
            # output limit by itself. Probe one GlobalId per call so a list
            # truncation cannot silently erase later exemplars.
            for guid in guids:
                try:
                    envelope = await workbench.call(
                        "analyze_element_geometry",
                        global_ids=[guid],
                        model=model_id,
                        detail="standard",
                        frame="semantic",
                        station_strategy="auto",
                    )
                except Exception as exc:
                    analysis_failures.append({"global_id": guid, "reason": str(exc)})
                    continue
                if not envelope.get("ok"):
                    problem = envelope.get("error") or {}
                    reason = problem.get("message") if isinstance(problem, dict) else problem
                    analysis_failures.append(
                        {"global_id": guid, "reason": str(reason or "analysis failed")}
                    )
                    continue
                data = envelope.get("data")
                records = data.get("elements") if isinstance(data, dict) else None
                if not isinstance(records, list) or not records:
                    analysis_failures.append(
                        {
                            "global_id": guid,
                            "reason": "geometry analysis returned no element record",
                        }
                    )
                    continue
                analysis["elements"].append(records[0])
                for key in (
                    "analysis_version",
                    "units",
                    "tessellation",
                    "analysis_options",
                    "budgets",
                ):
                    if key in data and key not in analysis:
                        analysis[key] = data[key]
                returned_revision = data.get("model_revision")
                if isinstance(returned_revision, dict):
                    analysis["model_revision"] = returned_revision
            analysis["matched"] = len(analysis["elements"])
            analysis["recording_failures"] = analysis_failures
        analysis_problem = (
            "; ".join(f"{row['global_id']}: {row['reason']}" for row in analysis_failures) or None
        )
        skill = build_recorded_skill(
            items=items,
            measured_at=measured_at,
            model_name=str(source_session.name or model_id),
            analysis=analysis,
            notes=notes or "",
        )
        description = body.get("description")
        description = (
            description.strip()[:200]
            if isinstance(description, str) and description.strip()
            else skill.description
        )
        applies_to = body.get("applies_to")
        applies_to = (
            applies_to.strip()[:200]
            if isinstance(applies_to, str) and applies_to.strip()
            else skill.applies_to
        )

        from ifc_console_agents.skills import AgentSkillStore

        store = AgentSkillStore(core.store.project_dir)
        try:
            row = await asyncio.to_thread(
                store.save,
                name.strip(),
                skill.content,
                description=description,
                applies_to=applies_to,
                overwrite=overwrite,
            )
        except ToolError as exc:
            status = 409 if exc.code == "FILE_EXISTS" else 400
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=status
            )
        core.audit.record(
            "skill_record",
            name=row["name"],
            path=row["path"],
            measurements=len(items),
            elements=len(guids),
            model=model_id,
            analyzed=len((analysis or {}).get("elements") or ()),
            analysis_failures=len(analysis_failures),
        )
        return JSONResponse(
            {
                "recorded": row,
                "classes": list(skill.classes),
                "intents": list(skill.intents),
                "unresolved_intents": list(skill.unresolved_intents),
                "analyzed": bool((analysis or {}).get("elements")),
                "analyzed_elements": len((analysis or {}).get("elements") or ()),
                "analysis_failures": analysis_failures,
                "analysis_problem": analysis_problem,
                "model_revision": (
                    analysis.get("model_revision")
                    if isinstance(analysis, dict)
                    else {
                        "model_id": model_id,
                        "fingerprint": source_session.fingerprint,
                        "revision": source_session.revision,
                    }
                ),
                "skills": await asyncio.to_thread(store.entries),
            }
        )

    async def list_workflows(_request) -> JSONResponse:
        """Every workflow this project can run, built-in and its own."""
        if not core.chat.enabled:
            return _disabled()
        from ifc_console_agents.skills import AgentSkillStore
        from ifc_console_agents.workflows import WorkflowRegistry

        registry = WorkflowRegistry(core.store.project_dir)
        rows, skills = await asyncio.gather(
            asyncio.to_thread(registry.entries),
            asyncio.to_thread(AgentSkillStore(core.store.project_dir).entries),
        )
        return JSONResponse(
            {
                "workflows": rows,
                "agents": [info.model_dump(mode="json") for info in core.agent_packs.active()],
                "skills": skills,
                "viewer": {
                    "connected": core.viewer_hub.connected,
                    "models": [
                        {
                            "id": row["model_id"],
                            "name": row["name"],
                            "active": row["active"],
                        }
                        for row in core.models.model_rows()
                    ],
                    "selections": core.viewer_hub.selection_rows(),
                },
            }
        )

    async def save_workflow(request) -> JSONResponse:
        """Create one reusable agent workflow from the compact browser form."""
        if not core.chat.enabled:
            return _disabled()
        from ifc_console.core.results import ToolError

        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

        title = str(body.get("title") or "").strip()[:100]
        task = str(body.get("prompt") or "").strip()[:20_000]
        instructions = str(body.get("instructions") or "").strip()[:8000]
        description = str(body.get("description") or "").strip()[:500]
        agent_name = str(body.get("agent") or "general").strip()
        skill_name = str(body.get("skill") or "").strip()
        scope = str(body.get("scope") or "either").strip()
        default_settings = body.get("settings") or {}
        if not title or not task:
            return JSONResponse(
                {"error": "name and task are required", "hint": "Describe the repeatable result."},
                status_code=400,
            )
        pack = core.agent_packs.get(agent_name)
        if pack is None:
            return JSONResponse({"error": f"no agent named {agent_name!r}"}, status_code=400)
        if scope not in {"model", "selection", "either"}:
            return JSONResponse(
                {"error": "scope must be model, selection, or either"}, status_code=400
            )
        if not isinstance(default_settings, dict):
            return JSONResponse({"error": "settings must be key/value pairs"}, status_code=400)
        if skill_name:
            from ifc_console_agents.skills import AgentSkillStore

            try:
                await asyncio.to_thread(AgentSkillStore(core.store.project_dir).read, skill_name)
            except ToolError as exc:
                return JSONResponse(
                    {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=400
                )
            task = (
                f"Load and follow the saved skill {skill_name!r}. Adapt its variable "
                f"values to the current workflow scope.\n\n{task}"
            )[:20_000]
        system_prompt = task[:20_000]

        from ifc_console_agents.workflows import (
            AgentStep,
            ExportStep,
            WorkflowRegistry,
            WorkflowSpec,
        )

        registry = WorkflowRegistry(core.store.project_dir)
        base = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "workflow"
        base = base[:63].rstrip("-")
        name = base
        existing = set(registry.all())
        counter = 2
        while name in existing:
            suffix = f"-{counter}"
            name = f"{base[: 63 - len(suffix)].rstrip('-')}{suffix}"
            counter += 1
        declared = getattr(pack, "declared_limits", AgentLimits())
        try:
            spec = WorkflowSpec(
                name=name,
                title=title,
                description=description or f"{task[:180].rstrip('.')}.",
                tags=("custom",),
                scope=scope,
                system_prompt=system_prompt,
                additional_instructions=instructions,
                settings=default_settings,
                steps=(
                    AgentStep(
                        id="run",
                        title="Run the procedure",
                        agent=pack.info.name,
                        prompt=(
                            "Carry out the workflow system prompt using the current "
                            "model scope and run settings. Return a complete report."
                        ),
                        max_tool_rounds=min(
                            60, max(8, int(getattr(declared, "max_tool_rounds", 12)))
                        ),
                        max_tool_calls=min(
                            400, max(32, int(getattr(declared, "max_tool_calls", 48)))
                        ),
                    ),
                    ExportStep(
                        id="report",
                        title="Save the result",
                        needs=("run",),
                        name=name,
                        body=f"# {title}\n\n{{{{ steps.run.text }}}}\n",
                    ),
                ),
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        try:
            path = await asyncio.to_thread(registry.save, spec)
        except ToolError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=409
            )
        core.audit.record(
            "custom_workflow_saved", workflow=spec.name, agent=agent_name, path=str(path)
        )
        row = spec.summary()
        row["origin"] = "project"
        return JSONResponse({"workflow": row, "path": str(path)}, status_code=201)

    async def update_workflow(request) -> JSONResponse:
        """Save editor changes as a project workflow override."""
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)

        from ifc_console.core.results import ToolError

        from ifc_console_agents.workflows import (
            AgentStep,
            WorkflowRegistry,
            WorkflowSpec,
        )

        name = str(body.get("workflow") or "").strip()
        registry = WorkflowRegistry(core.store.project_dir)
        try:
            spec = await asyncio.to_thread(registry.get, name)
        except ToolError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=404
            )

        title = str(body.get("title", spec.title)).strip()[:100]
        description = str(body.get("description", spec.description)).strip()[:500]
        system_prompt = str(body.get("system_prompt", spec.system_prompt)).strip()[:20_000]
        additional = str(body.get("additional_instructions", spec.additional_instructions)).strip()[
            :8000
        ]
        scope = str(body.get("scope", spec.scope)).strip()
        settings = body.get("settings", spec.settings)
        agent_name = str(body.get("agent") or "").strip()
        if not title:
            return JSONResponse({"error": "workflow name is required"}, status_code=400)
        if scope not in {"model", "selection", "either"}:
            return JSONResponse(
                {"error": "scope must be model, selection, or either"}, status_code=400
            )
        if not isinstance(settings, dict):
            return JSONResponse({"error": "settings must be key/value pairs"}, status_code=400)
        if agent_name and core.agent_packs.get(agent_name) is None:
            return JSONResponse({"error": f"no agent named {agent_name!r}"}, status_code=400)

        steps = list(spec.steps)
        if agent_name:
            for index, step in enumerate(steps):
                if isinstance(step, AgentStep):
                    steps[index] = step.model_copy(update={"agent": agent_name})
                    break
        try:
            updated = WorkflowSpec.model_validate(
                spec.model_copy(
                    update={
                        "title": title,
                        "description": description,
                        "system_prompt": system_prompt,
                        "additional_instructions": additional,
                        "settings": settings,
                        "scope": scope,
                        "steps": tuple(steps),
                    }
                ).model_dump()
            )
            path = await asyncio.to_thread(registry.save, updated, overwrite=True)
        except (ToolError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        core.audit.record(
            "workflow_updated", workflow=updated.name, agent=agent_name, path=str(path)
        )
        row = updated.summary()
        row["origin"] = "project"
        return JSONResponse({"workflow": row, "path": str(path)})

    async def run_workflow(request) -> Response:
        """Run one workflow and stream its steps.

        The event vocabulary is the chat panel's, plus the workflow-shaped
        events, so one renderer serves the workflow page and the agent panel.
        """
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        name = body.get("workflow")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "workflow must be a name"}, status_code=400)
        inputs = body.get("inputs") or {}
        if not isinstance(inputs, dict):
            return JSONResponse({"error": "inputs must be a JSON object"}, status_code=400)
        settings = body.get("settings") if "settings" in body else None
        if settings is not None and not isinstance(settings, dict):
            return JSONResponse({"error": "settings must be key/value pairs"}, status_code=400)
        scope = str(body.get("scope") or "model").strip()
        note = str(body.get("note") or "").strip()
        if scope not in {"model", "selection"}:
            return JSONResponse({"error": "scope must be model or selection"}, status_code=400)
        if len(note) > 2000:
            return JSONResponse({"error": "run guidance is too long"}, status_code=400)

        from ifc_console.core.results import ToolError

        from ifc_console_agents.workflow_runner import WorkflowRunner
        from ifc_console_agents.workflows import (
            AgentStep,
            WorkflowRegistry,
            validate_inputs,
            validate_settings,
        )

        registry = WorkflowRegistry(core.store.project_dir)
        try:
            spec = await asyncio.to_thread(registry.get, name.strip())
            validate_inputs(spec, inputs)
            validate_settings(spec, settings)
        except ToolError as exc:
            status = 404 if exc.code == "NOT_FOUND" else 400
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=status
            )
        if spec.scope == "model" and scope != "model":
            return JSONResponse(
                {"error": "this workflow runs over the whole model"}, status_code=400
            )
        if spec.scope == "selection":
            scope = "selection"
        if scope == "selection" and not core.viewer_hub.selection_rows():
            return JSONResponse(
                {
                    "error": "nothing is selected in the viewer",
                    "hint": "Select one or more elements in the 3D view, then run again.",
                },
                status_code=400,
            )

        # A workflow of tools, gates, and a report needs no provider at all.
        # Demanding a model for one would put an API key in front of work the
        # console can do on its own.
        resolved = None
        if any(isinstance(step, AgentStep) for step in spec.steps):
            resolved, error = resolve_provider_model(core, body)
            if error is not None:
                return error
            assert resolved is not None

        state = _panel_state(core)
        runner = WorkflowRunner(
            core,
            model=resolved.model if resolved else None,
            model_label=(f"{resolved.provider_id}/{resolved.model_id}" if resolved else ""),
            auto_approve=bool(core.ai_autonomy),
        )
        core.audit.record(
            "workflow_panel_request",
            workflow=spec.name,
            provider=resolved.provider_id if resolved else "none",
            model=resolved.model_id if resolved else "none",
            run=runner.run_id,
        )
        thread_key = f"workflow:{runner.run_id}"

        async def events():
            stream_task = asyncio.current_task()
            if stream_task is not None:
                async with state.lifecycle_lock:
                    state.active_streams.setdefault(thread_key, set()).add(stream_task)
            try:
                async for event in runner.stream(
                    spec, inputs, scope=scope, note=note, settings=settings
                ):
                    yield _sse(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # the panel must always finish cleanly
                log.exception("workflow %s failed", spec.name)
                yield _sse({"type": "error", "text": "internal workflow error"})
            finally:
                state.deny_owned(runner)
                if stream_task is not None:
                    async with state.lifecycle_lock:
                        active = state.active_streams.get(thread_key)
                        if active is not None:
                            active.discard(stream_task)
                            if not active:
                                state.active_streams.pop(thread_key, None)
            yield _sse({"type": "done"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def continue_workflow(request) -> Response:
        """Answer one follow-up question about a run the reader is looking at.

        A finished run is a conversation, not a receipt: the reader keeps the
        workflow's prompt and tools and simply asks the next question.
        """
        if not core.chat.enabled:
            return _disabled()
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
        name = body.get("workflow")
        if not isinstance(name, str) or not name.strip():
            return JSONResponse({"error": "workflow must be a name"}, status_code=400)
        message = str(body.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "ask a question first"}, status_code=400)
        if len(message) > 8000:
            return JSONResponse({"error": "the question is too long"}, status_code=400)
        settings = body.get("settings") if "settings" in body else None
        if settings is not None and not isinstance(settings, dict):
            return JSONResponse({"error": "settings must be key/value pairs"}, status_code=400)
        history = body.get("history") or []
        if not isinstance(history, list):
            return JSONResponse({"error": "history must be a list of turns"}, status_code=400)
        report = str(body.get("report") or "")
        note = str(body.get("note") or "").strip()[:2000]
        scope = str(body.get("scope") or "model").strip()
        if scope not in {"model", "selection"}:
            return JSONResponse({"error": "scope must be model or selection"}, status_code=400)

        from ifc_console.core.results import ToolError

        from ifc_console_agents.workflow_runner import WorkflowRunner
        from ifc_console_agents.workflows import WorkflowRegistry, validate_settings

        registry = WorkflowRegistry(core.store.project_dir)
        try:
            spec = await asyncio.to_thread(registry.get, name.strip())
            validate_settings(spec, settings)
        except ToolError as exc:
            status = 404 if exc.code == "NOT_FOUND" else 400
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=status
            )
        if scope == "selection" and not core.viewer_hub.selection_rows():
            return JSONResponse(
                {
                    "error": "nothing is selected in the viewer",
                    "hint": "Select elements in the 3D view, or ask about the whole model.",
                },
                status_code=400,
            )
        resolved, error = resolve_provider_model(core, body)
        if error is not None:
            return error
        assert resolved is not None

        state = _panel_state(core)
        runner = WorkflowRunner(
            core,
            model=resolved.model,
            model_label=f"{resolved.provider_id}/{resolved.model_id}",
            auto_approve=bool(core.ai_autonomy),
        )
        core.audit.record(
            "workflow_follow_up_request",
            workflow=spec.name,
            provider=resolved.provider_id,
            model=resolved.model_id,
            run=runner.run_id,
        )
        thread_key = f"workflow:{runner.run_id}"

        turns = [row for row in history if isinstance(row, dict)]

        async def events():
            stream_task = asyncio.current_task()
            if stream_task is not None:
                async with state.lifecycle_lock:
                    state.active_streams.setdefault(thread_key, set()).add(stream_task)
            try:
                async for event in runner.follow_up(
                    spec,
                    message,
                    report=report,
                    history=turns,
                    scope=scope,
                    note=note,
                    settings=settings,
                ):
                    yield _sse(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # the panel must always finish cleanly
                log.exception("workflow %s follow-up failed", spec.name)
                yield _sse({"type": "error", "text": "internal workflow error"})
            finally:
                state.deny_owned(runner)
                if stream_task is not None:
                    async with state.lifecycle_lock:
                        active = state.active_streams.get(thread_key)
                        if active is not None:
                            active.discard(stream_task)
                            if not active:
                                state.active_streams.pop(thread_key, None)
            yield _sse({"type": "done"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return [
        Route("/api/agents", list_agents, methods=["GET"]),
        Route("/api/agents/blocks", list_blocks, methods=["GET"]),
        Route("/api/agents/workflows", list_workflows, methods=["GET"]),
        Route("/api/agents/workflows/create", save_workflow, methods=["POST"]),
        Route("/api/agents/workflows/update", update_workflow, methods=["POST"]),
        Route("/api/agents/workflows/run", run_workflow, methods=["POST"]),
        Route("/api/agents/workflows/continue", continue_workflow, methods=["POST"]),
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
        Route("/api/agents/geometry/review", geometry_review, methods=["POST"]),
        Route("/api/agents/skills/dry-run", skill_dry_run, methods=["POST"]),
        Route("/api/agents/skills/record", skills_record, methods=["POST"]),
    ]


__all__ = ["AgentPanelState", "build_agent_panel_routes", "panel_limits", "panel_runtime"]
