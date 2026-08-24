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
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ifc_console.agents.agent import Agent, _summary
from ifc_console.agents.files import MAX_REFERENCE_BYTES
from ifc_console.agents.models import AgentLimits

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.agents")

_MAX_THREADS = 20
_MAX_PROMPT_CHARS = 100_000
_MAX_UPLOAD_BYTES = MAX_REFERENCE_BYTES


@dataclass
class PanelThread:
    pack: str
    agent: Agent
    signature: tuple[str, ...]


class AgentPanelState:
    """Per-console-run panel threads and the shared runtime view."""

    def __init__(self) -> None:
        self.threads: OrderedDict[str, PanelThread] = OrderedDict()
        self.runtime: Any = None

    def remember(self, thread_id: str, thread: PanelThread) -> None:
        self.threads[thread_id] = thread
        self.threads.move_to_end(thread_id)
        while len(self.threads) > _MAX_THREADS:
            self.threads.popitem(last=False)


def _panel_state(core: AppCore) -> AgentPanelState:
    state = getattr(core, "agent_panel", None)
    if state is None:
        state = AgentPanelState()
        core.agent_panel = state
    return state


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


def _event_payloads(event: Any) -> list[dict[str, Any]]:
    """One typed AgentEvent as chat-panel SSE payloads."""
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
                "arguments": json.dumps(event.arguments or {}, default=str)[:400],
            }
        ]
    if event.type == "tool_call_finished":
        result = event.result or {}
        payloads = [
            {
                "type": "tool_result",
                "id": event.tool_call_id,
                "name": event.tool_name,
                "ok": bool(result.get("ok")),
                "summary": _summary(result),
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

        agent.thread_store = JsonThreadStore(
            core.store.project_dir / ".ifc-console" / "agents" / "threads"
        )
    limits = agent.limits
    rounds = core.settings.chat.max_tool_rounds
    agent.limits = AgentLimits(
        max_tool_rounds=rounds,
        max_tool_calls=max(limits.max_tool_calls, rounds * 4),
        timeout_s=float(core.settings.chat.timeout_s),
        max_tool_result_chars=limits.max_tool_result_chars,
        parallel_read_only=limits.parallel_read_only,
    )
    return PanelThread(pack=pack.info.name, agent=agent, signature=signature)


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
        pack = core.agent_packs.get(name)
        if pack is None:
            active = ", ".join(info.name for info in core.agent_packs.active()) or "(none)"
            return JSONResponse(
                {"error": f"no agent named {name!r}", "hint": f"active: {active}"},
                status_code=404,
            )
        instructions = request.query_params.get("instructions") or ""
        if len(instructions) > 12_000:
            return JSONResponse({"error": "instructions are too long"}, status_code=400)
        from ifc_console.agents.workspace import describe

        try:
            payload = await describe(core, pack, instructions=instructions)
        except Exception:
            log.exception("agent workspace for %s failed", name)
            return JSONResponse(
                {"error": f"could not describe agent {name!r}"}, status_code=500
            )
        if "files" in pack.info.features:
            try:
                payload["files"] = await asyncio.to_thread(
                    core.agent_files.entries, core.project_knowledge.sources()
                )
            except Exception:
                log.warning("workspace file listing failed", exc_info=True)
                payload["files"] = []
        else:
            payload["files"] = []
        return JSONResponse(payload)

    async def list_blocks(_request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        from ifc_console.agents.blocks import BLOCKS

        return JSONResponse(
            {"blocks": [block.info().model_dump(mode="json") for block in BLOCKS]}
        )

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
        if not isinstance(thread_id, str) or not thread_id.startswith("panel-") or len(thread_id) > 100:
            return JSONResponse({"error": "invalid panel thread id"}, status_code=400)
        state = _panel_state(core)
        state.threads.pop(thread_id, None)
        from ifc_console.agents.storage import JsonThreadStore

        removed = await JsonThreadStore(
            core.store.project_dir / ".ifc-console" / "agents" / "threads"
        ).delete(thread_id)
        core.audit.record("agent_panel_thread_deleted", thread=thread_id)
        return JSONResponse({"ok": True, "removed": removed})

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
        signature = (
            provider.id,
            chosen,
            base_url,
            pack.info.model_dump_json(),
            "persistent" if persist_history else "ephemeral",
            sha256(session_instructions.encode("utf-8")).hexdigest()[:16],
        )
        requested_thread_id = (
            body.get("thread_id") if isinstance(body.get("thread_id"), str) else None
        )
        if requested_thread_id and (
            not requested_thread_id.startswith("panel-") or len(requested_thread_id) > 100
        ):
            return JSONResponse({"error": "invalid panel thread id"}, status_code=400)
        thread_id = requested_thread_id
        thread = state.threads.get(thread_id) if thread_id else None
        if thread is not None and (thread.pack != pack.info.name or thread.signature != signature):
            thread = None
        if thread is None:
            if not persist_history or not requested_thread_id:
                thread_id = f"panel-{uuid4().hex[:12]}"
            try:
                thread = await _build_thread(
                    core,
                    pack,
                    signature=signature,
                    model=model,
                    persistent=persist_history,
                    instructions=session_instructions,
                    model_label=f"{provider.id}/{chosen}",
                )
            except Exception:
                log.exception("agent pack %s failed to build", pack.info.name)
                return JSONResponse(
                    {"error": f"agent {pack.info.name!r} failed to start"}, status_code=500
                )
        assert thread_id is not None
        state.remember(thread_id, thread)
        core.audit.record(
            "agent_panel_request",
            agent=pack.info.name,
            provider=provider.id,
            model=chosen,
            thread=thread_id,
        )

        attachments = body.get("attachments")
        attachment_paths = (
            [str(item) for item in attachments if isinstance(item, str)][:8]
            if isinstance(attachments, list)
            else []
        )
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

        async def events():
            yield _sse({"type": "thread", "id": thread_id, "agent": pack.info.name})
            try:
                async for event in thread.agent.stream(
                    prompt.strip() + attachment_note,
                    thread_id=thread_id,
                    options=options,
                    images=images,
                ):
                    for payload in _event_payloads(event):
                        yield _sse(payload)
            except Exception:  # the panel must always finish cleanly
                log.exception("agent panel stream failed")
                yield _sse({"type": "error", "text": "internal agent error"})
            yield _sse({"type": "done"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def upload(request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        from ifc_console.knowledge.ingest import SUPPORTED_SUFFIXES

        name = request.query_params.get("agent") or ""
        pack = core.agent_packs.get(name)
        if pack is None or "files" not in pack.info.features:
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
            target = await asyncio.to_thread(core.agent_files.save_upload, raw_name, data)
        except ToolError as exc:
            return JSONResponse(
                {"error": str(exc), "code": exc.code, "hint": exc.hint}, status_code=400
            )
        relative_target = target.relative_to(core.store.project_dir).as_posix()
        try:
            report = await asyncio.to_thread(core.project_knowledge.ingest, [target])
        except ToolError as exc:
            core.audit.record(
                "agent_panel_upload",
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
            "agent_panel_upload", agent=name, file=str(target), records=report["records"]
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

    return [
        Route("/api/agents", list_agents, methods=["GET"]),
        Route("/api/agents/blocks", list_blocks, methods=["GET"]),
        Route("/api/agents/workspace", agent_workspace, methods=["GET"]),
        Route("/api/agents/capabilities", list_capabilities, methods=["GET"]),
        Route("/api/agents/custom", save_custom_agent, methods=["POST"]),
        Route("/api/agents/custom/delete", delete_custom_agent, methods=["POST"]),
        Route("/api/agents/thread/delete", delete_thread, methods=["POST"]),
        Route("/api/agents/files", list_files, methods=["GET"]),
        Route("/api/agents/stream", stream, methods=["POST"]),
        Route("/api/agents/upload", upload, methods=["POST"]),
    ]


__all__ = ["AgentPanelState", "build_agent_panel_routes", "panel_runtime"]
