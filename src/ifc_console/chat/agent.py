"""The chat tool loop.

The provider streams; when it asks for tools we run them against the live
session and feed the envelopes back. Tool calls go through the same functions
the MCP layer serves, so the ask/edit gate, the guards, and the audit log all
apply exactly as they do for an external client.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from ifc_console.chat import SYSTEM_PROMPT
from ifc_console.chat.providers import (
    PROVIDERS,
    Provider,
    ProviderError,
    astream,
    resolve_key,
    stream,
    validate_base_url,
)
from ifc_console.core.operations import OperationImage

if TYPE_CHECKING:
    from ifc_console.app import AppCore

# A tool result is context, not a report: enough to answer with, not enough to
# fill the window. The model can always call again with a tighter query.
TOOL_RESULT_LIMIT = 6000
# What the panel shows under a tool call. Smaller than the model's copy: a
# reader skims it, and the console keeps the full envelope.
TOOL_PREVIEW_LIMIT = 1600
_PREVIEW_ROWS = 50


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


def _light(value: Any, depth: int = 0) -> Any:
    """The same value with image bytes replaced by a count.

    Base64 pixels are for the model, never for the panel: one screenshot would
    otherwise put a megabyte of text into the transcript.
    """
    if depth > 6:
        return "..."
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "images" and isinstance(item, (list, tuple)):
                out[key] = f"{len(item)} image(s)"
            else:
                out[key] = _light(item, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        rows = list(value)
        shown = [_light(item, depth + 1) for item in rows[:_PREVIEW_ROWS]]
        # Never a silent cut: a reader who sees 50 rows must know there were
        # 400, or they will read the preview as the whole answer.
        if len(rows) > _PREVIEW_ROWS:
            shown.append(f"...{len(rows) - _PREVIEW_ROWS} more not shown")
        return shown
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + "..."
    return value


def _inspectable(value: Any, depth: int = 0) -> Any:
    """A complete panel result with binary image payloads made harmless."""
    if depth > 12:
        return "..."
    if isinstance(value, Mapping):
        return {
            key: (
                f"{len(item)} image(s)"
                if key == "images" and isinstance(item, (list, tuple))
                else _inspectable(item, depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_inspectable(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"{len(value)} binary byte(s)"
    return value


def _error_text(error: Mapping[str, Any]) -> str:
    """One failure, written the way the console would say it out loud."""
    parts = [str(error.get("message") or "").strip()]
    if error.get("hint"):
        parts.append(f"Hint: {str(error['hint']).strip()}")
    extra = {
        key: value for key, value in error.items() if key not in {"code", "message", "hint"}
    }
    if extra:
        parts.append(json.dumps(_light(extra), default=str, ensure_ascii=False, indent=1))
    body = "\n\n".join(part for part in parts if part)
    return body or str(error.get("code") or "the tool failed without a message")


def tool_event(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The panel-facing view of one tool result.

    The panel draws a tool where it ran, so it needs more than "ok": the row
    count, the error the console reported, and a readable slice of the data.
    The bounded console envelope is kept as structured output as well. The
    browser only turns that into DOM when the reader opens the call, so a
    complete inspectable result does not make streamed rendering expensive.
    """
    ok = bool(payload.get("ok"))
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}
    rows = meta.get("returned") if isinstance(meta, Mapping) else None
    if ok:
        summary = f"{rows} row(s)" if rows is not None else "ok"
        detail = ""
        text = json.dumps(_light(payload.get("data")), default=str, ensure_ascii=False, indent=1)
    else:
        summary = str(error.get("code") or "failed")
        detail = str(error.get("message") or error.get("hint") or "")
        # A failure is prose, not a record. Rendering it as JSON turned the
        # parser's own line breaks into a wall of literal \n for the reader.
        text = _error_text(error)
    if len(text) > TOOL_PREVIEW_LIMIT:
        text = text[:TOOL_PREVIEW_LIMIT] + "\n... truncated"
    return {
        "ok": ok,
        "summary": summary,
        "rows": rows if isinstance(rows, int) else None,
        "detail": detail[:400],
        "preview": text,
        # Operation envelopes have already been capped by output_char_limit.
        # Round-trip through JSON so custom scalar types cannot break SSE.
        "output": json.loads(
            json.dumps(_inspectable(payload), default=str, ensure_ascii=False)
        ),
    }


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


def _failure(code: str, message: str) -> dict[str, Any]:
    """A tool that never reached the console still reports like one."""
    return tool_event({"ok": False, "error": {"code": code, "message": message}})


async def run_tool(core: AppCore, name: str, arguments: str) -> tuple[str, dict[str, Any]]:
    """Execute one tool call, returning (text for the model, event payload)."""
    try:
        parsed = json.loads(arguments or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("arguments must be a JSON object")
    except Exception as exc:
        message = f"invalid tool arguments: {exc}"
        return message, _failure("bad_arguments", message)

    spec = core.operations.get(name)
    if spec is None:
        message = f"no tool named {name!r}"
        return message, _failure("unknown_tool", message)

    mistake = _argument_error(spec.handler, name, parsed)
    if mistake:
        return mistake, _failure("bad_arguments", mistake)

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
    return _clip(text), tool_event(payload)


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
    tools_supported: bool | None = None,
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

    tools = await tool_schemas(core) if use_tools and tools_supported is not False else []
    prompt = (system or "").strip() or SYSTEM_PROMPT
    if use_tools and tools_supported is False:
        prompt += (
            "\n\nThis model is configured without tool calling. Do not claim to have "
            "queried, measured, viewed, or changed the IFC model. Explain when a "
            "request needs a tool-capable model."
        )
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
        round_stream = astream(
            provider,
            stream_factory=stream,
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
