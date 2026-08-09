"""The chat tool loop.

The provider streams; when it asks for tools we run them against the live
session and feed the envelopes back. Tool calls go through the same functions
the MCP layer serves, so the ask/edit gate, the guards, and the audit log all
apply exactly as they do for an external client.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import threading
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from ifc_console.chat import SYSTEM_PROMPT
from ifc_console.chat.providers import (
    PROVIDERS,
    Provider,
    ProviderError,
    redact,
    resolve_key,
    stream,
    validate_base_url,
)
from ifc_console.core.operations import OperationImage

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.chat")

# A tool result is context, not a report: enough to answer with, not enough to
# fill the window. The model can always call again with a tighter query.
TOOL_RESULT_LIMIT = 6000
_QUEUE_DONE = object()


async def tool_schemas(core: AppCore) -> list[dict[str, Any]]:
    """Provider-neutral schemas for every tool this session exposes."""
    return [
        {
            "name": definition.name,
            "description": definition.description[:1024],
            "input_schema": definition.input_schema,
        }
        for definition in core.operation_service.definitions()
    ]


def _clip(text: str, limit: int | None = None) -> str:
    limit = limit or TOOL_RESULT_LIMIT
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…[truncated; narrow the query or lower limit]"


def _argument_error(fn: Any, name: str, arguments: dict) -> str | None:
    """A model naming an argument that does not exist deserves the real list."""
    try:
        allowed = set(inspect.signature(fn).parameters)
    except (TypeError, ValueError):
        return None
    unknown = sorted(set(arguments) - allowed)
    if not unknown:
        return None
    return f"bad arguments for {name}: {unknown} is not accepted. Valid: {sorted(allowed)}"


async def run_tool(core: AppCore, name: str, arguments: str) -> tuple[str, dict[str, Any]]:
    """Execute one tool call, returning (text for the model, event payload)."""
    try:
        parsed = json.loads(arguments or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("arguments must be a JSON object")
    except Exception as exc:
        message = f"invalid tool arguments: {exc}"
        return message, {"ok": False, "summary": message}

    spec = core.operations.get(name)
    if spec is None:
        message = f"no tool named {name!r}"
        return message, {"ok": False, "summary": message}

    mistake = _argument_error(spec.handler, name, parsed)
    if mistake:
        return mistake, {"ok": False, "summary": "bad arguments"}

    result = await core.operation_service.call(name, parsed)
    if hasattr(result, "model_dump"):
        payload = result.model_dump()
    elif isinstance(result, dict):
        payload = result
    else:
        content = []
        for item in result if isinstance(result, (list, tuple)) else [result]:
            if isinstance(item, OperationImage):
                content.append({"type": "image", "format": item.format, "bytes": len(item.data)})
            else:
                content.append(item)
        payload = {"ok": True, "data": {"content": content}, "meta": {}}
    text = json.dumps(payload, default=str)
    if payload.get("ok"):
        summary = "ok"
        meta = payload.get("meta") or {}
        if meta.get("returned") is not None:
            summary = f"{meta['returned']} row(s)"
    else:
        summary = (payload.get("error") or {}).get("code", "failed")
    return _clip(text), {"ok": bool(payload.get("ok")), "summary": summary}


def _provider(core: AppCore, requested: str | None) -> Provider:
    name = (requested or core.chat.provider or "openai").lower()
    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(f"unknown provider {name!r}; one of {sorted(PROVIDERS)}")
    return provider


async def converse(
    core: AppCore,
    *,
    turns: list[dict[str, Any]],
    provider_id: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    system: str | None = None,
    use_tools: bool = True,
    options: dict[str, Any] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Stream one exchange, running tool calls until the model stops asking."""
    settings = core.settings.chat
    provider = _provider(core, provider_id)
    base = validate_base_url(
        base_url or core.chat.base_url or provider.base_url,
        local_only=settings.local_only,
    )
    key = resolve_key(provider, api_key or core.chat.key_for(provider.id))
    if provider.needs_key and not key:
        env = " or ".join(provider.key_env) or "an API key"
        raise ProviderError(f"no API key for {provider.label}; set {env} or paste one in the panel")
    chosen = (model or core.chat.model or provider.suggested_model).strip()
    if not chosen:
        raise ProviderError(f"pick a model for {provider.label} first")

    tools = await tool_schemas(core) if use_tools else []
    prompt = (system or "").strip() or SYSTEM_PROMPT
    prompt = f"{prompt}\n\nSession: {json.dumps(core.session_meta(), default=str)}"
    conversation = list(turns)
    options = options or {}

    core.audit.record(
        "chat_request",
        provider=provider.id,
        model=chosen,
        tools=len(tools),
        turns=len(conversation),
    )

    for round_index in range(max(1, settings.max_tool_rounds)):
        calls: list[dict[str, Any]] = []
        text_parts: list[str] = []
        round_stream = _stream_round(
            provider,
            base_url=base,
            key=key,
            model=chosen,
            system=prompt,
            turns=conversation,
            tools=tools,
            options=options,
            timeout=float(settings.timeout_s),
            local_only=settings.local_only,
        )
        try:
            async for event in round_stream:
                if event["type"] == "tool_calls":
                    calls = event["calls"]
                    continue
                if event["type"] == "content":
                    text_parts.append(event["text"])
                yield event
        finally:
            await round_stream.aclose()

        if not calls:
            return

        conversation.append({"role": "assistant", "text": "".join(text_parts), "tool_calls": calls})
        for index, call in enumerate(calls):
            # the panel pairs result to call by id; a model that calls the same
            # tool twice in one round would otherwise leave a chip spinning.
            # Filling a missing id here also keeps the two message shapes, which
            # both reference it, agreeing on one value.
            call_id = call["id"] = call.get("id") or f"call-{round_index}-{index}"
            yield {
                "type": "tool_call",
                "id": call_id,
                "name": call["name"],
                "arguments": call["arguments"][:400],
            }
            text, info = await run_tool(core, call["name"], call["arguments"])
            yield {"type": "tool_result", "id": call_id, "name": call["name"], **info}
            conversation.append(
                {"role": "tool", "tool_call_id": call["id"], "name": call["name"], "text": text}
            )
        if round_index == settings.max_tool_rounds - 1:
            yield {
                "type": "error",
                "text": (
                    f"stopped after {settings.max_tool_rounds} tool rounds; "
                    "ask a narrower question or raise chat.max_tool_rounds"
                ),
            }


async def _stream_round(provider: Provider, **kwargs) -> AsyncIterator[dict[str, Any]]:
    """Run one blocking provider stream on a thread, yield its events here.

    The consumer can walk away at any moment: the browser's Stop button aborts
    the fetch, which closes this generator. So the thread is never waited on and
    never blocks on a full queue; it is told to stop through `cancel` and exits
    on its own, closing the provider socket as it unwinds.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel = threading.Event()
    slots = threading.Semaphore(64)

    def emit(event: Any) -> bool:
        while not cancel.is_set():
            if slots.acquire(timeout=0.05):
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:  # loop closed under us
                    slots.release()
                    return False
                return True
        return False

    def pump() -> None:
        try:
            for event in stream(provider, cancel=cancel, **kwargs):
                if cancel.is_set():
                    return
                if not emit(event):
                    return
        except ProviderError as exc:
            emit({"type": "error", "text": str(exc)})
        except Exception as exc:  # never leave the panel hanging
            log.exception("chat provider failed")
            secret = str(kwargs.get("key") or "")
            emit(
                {
                    "type": "error",
                    "text": f"{type(exc).__name__}: {redact(str(exc), (secret,))}",
                }
            )
        finally:
            emit(_QUEUE_DONE)

    threading.Thread(target=pump, name="ifc-console-chat", daemon=True).start()
    try:
        while True:
            event = await queue.get()
            slots.release()
            if event is _QUEUE_DONE:
                return
            yield event
    finally:
        cancel.set()
