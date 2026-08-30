"""Chat HTTP routes: the panel shell, provider discovery, and the SSE stream.

Same rules as the viewer: token-gated by TokenAuthMiddleware, 404 while chat
is disabled, and the page shell is the one path a browser may fetch with only
the fragment token.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from ifc_console.core.results import ToolError
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ifc_console_agents import assets
from ifc_console_agents.chat import SYSTEM_PROMPT
from ifc_console_agents.chat.agent import converse
from ifc_console_agents.chat.providers import (
    PROVIDERS,
    ProviderError,
    key_source,
    list_models,
    validate_base_url,
)

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.chat")

_MAX_TURNS = 200
_MAX_TURN_CHARS = 100_000
_MAX_TRANSCRIPT_CHARS = 1_000_000
_MAX_SYSTEM_CHARS = 100_000
_MAX_MODEL_CHARS = 500
_MAX_URL_CHARS = 2048
_MAX_KEY_CHARS = 4096

# The panel talks only to this server; the provider call happens server side,
# which is what keeps keys out of the browser and the CSP this tight.
_CSP = (
    "default-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' blob: data:; "
    "script-src 'self'; "
    "style-src 'self'; "
    "base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'none'"
)


def _disabled() -> JSONResponse:
    return JSONResponse(
        {
            "error": "chat_disabled",
            "hint": "type /agent in the ifc-console terminal to open the Agent workspace",
        },
        status_code=404,
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


async def _json_object(request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse({"error": "request body must be a JSON object"}, status_code=400)
    return body, None


def _optional_text(body: dict[str, Any], key: str, limit: int) -> str | None:
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    if len(value) > limit:
        raise ValueError(f"{key} is too long")
    return value


def _number_option(
    body: dict[str, Any], key: str, minimum: float, maximum: float
) -> int | float | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum:g} and {maximum:g}")
    return value


def _integer_option(body: dict[str, Any], key: str, minimum: int, maximum: int) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def build_chat_routes(core: AppCore) -> list[Route]:
    async def chat_shell(_request) -> Response:
        if not core.chat.enabled:
            return _disabled()
        return FileResponse(
            assets.require_static_dir() / "chat.html",
            headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-cache"},
        )

    async def workflows_shell(_request) -> Response:
        if not core.chat.enabled:
            return _disabled()
        return FileResponse(
            assets.require_static_dir() / "workflows.html",
            headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-cache"},
        )

    async def providers(_request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        rows = []
        for provider in PROVIDERS.values():
            rows.append(
                {
                    "id": provider.id,
                    "label": provider.label,
                    "family": provider.family,
                    "base_url": provider.base_url,
                    "needs_key": provider.needs_key,
                    "key_env": list(provider.key_env),
                    "key_from_env": key_source(provider),
                    "has_key": bool(key_source(provider) or core.chat.key_for(provider.id)),
                    "suggested_model": provider.suggested_model,
                    "note": provider.note,
                }
            )
        settings = core.settings.chat
        return JSONResponse(
            {
                "providers": rows,
                "selected": {
                    "provider": core.chat.provider,
                    "model": core.chat.model,
                    "base_url": core.chat.base_url,
                },
                "defaults": {
                    "system": SYSTEM_PROMPT,
                    "tools": settings.tools,
                    "max_tool_rounds": settings.max_tool_rounds,
                    "local_only": settings.local_only,
                },
                "session": core.session_meta(),
                "viewer": {"enabled": core.viewer.enabled},
            }
        )

    async def credentials(request) -> JSONResponse:
        """Manage provider secrets without ever returning secret material."""
        if not core.chat.enabled:
            return _disabled()
        from ifc_console_agents import credentials as credential_store

        if request.method == "GET":
            return JSONResponse(
                {
                    "available": credential_store.keyring_available(),
                    "providers": credential_store.stored_providers(core.store.home),
                    "storage": "operating-system credential store",
                }
            )
        body, error = await _json_object(request)
        if error is not None:
            return error
        assert body is not None
        try:
            provider_id = (_optional_text(body, "provider", 50) or "").lower()
            action = (_optional_text(body, "action", 20) or "store").lower()
            key = _optional_text(body, "api_key", _MAX_KEY_CHARS)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if provider_id not in PROVIDERS:
            return JSONResponse({"error": "unknown provider"}, status_code=400)
        try:
            if action == "delete":
                removed = credential_store.delete_api_key(core.store.home, provider_id)
                core.chat.keys.pop(provider_id, None)
                core.audit.record("provider_key_deleted", provider=provider_id)
                return JSONResponse({"ok": True, "stored": False, "removed": removed})
            if action != "store":
                return JSONResponse({"error": "action must be store or delete"}, status_code=400)
            if not key:
                return JSONResponse({"error": "api_key is required"}, status_code=400)
            credential_store.set_api_key(core.store.home, provider_id, key)
            core.chat.keys.pop(provider_id, None)
            core.audit.record("provider_key_stored", provider=provider_id)
            return JSONResponse({"ok": True, "stored": True})
        except ToolError as exc:
            return JSONResponse(
                {"error": exc.message, "code": exc.code, "hint": exc.hint}, status_code=501
            )

    async def models(request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        body, error = await _json_object(request)
        if error is not None:
            return error
        assert body is not None
        try:
            provider_id = (_optional_text(body, "provider", 50) or "").lower()
            supplied_key = _optional_text(body, "api_key", _MAX_KEY_CHARS)
            supplied_url = _optional_text(body, "base_url", _MAX_URL_CHARS)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        provider = PROVIDERS.get(provider_id)
        if provider is None:
            return JSONResponse({"error": "unknown provider"}, status_code=400)
        from ifc_console_agents.chat.providers import resolve_key

        key = resolve_key(provider, supplied_key or core.chat.key_for(provider.id))
        import asyncio

        try:
            base_url = validate_base_url(
                supplied_url or provider.base_url,
                local_only=core.settings.chat.local_only,
            )
            names = await asyncio.to_thread(
                list_models,
                provider,
                key,
                base_url,
                local_only=core.settings.chat.local_only,
            )
        except ProviderError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(
            {
                "models": list(names),
                "model_details": getattr(names, "details", {}),
            }
        )

    async def remember(request) -> JSONResponse:
        """Hold a key and the model choice for this run only. Never written."""
        if not core.chat.enabled:
            return _disabled()
        body, error = await _json_object(request)
        if error is not None:
            return error
        assert body is not None
        try:
            provider = (_optional_text(body, "provider", 50) or "").lower()
            model = _optional_text(body, "model", _MAX_MODEL_CHARS)
            api_key = _optional_text(body, "api_key", _MAX_KEY_CHARS)
            supplied_base_url = _optional_text(body, "base_url", _MAX_URL_CHARS)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        if provider and provider not in PROVIDERS:
            return JSONResponse({"error": "unknown provider"}, status_code=400)
        raw_base_url = None
        if supplied_base_url is not None:
            raw_base_url = supplied_base_url.strip()
            try:
                normalized_base_url = (
                    validate_base_url(
                        raw_base_url,
                        local_only=core.settings.chat.local_only,
                    )
                    if raw_base_url
                    else ""
                )
            except ProviderError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        if provider:
            core.chat.provider = provider
        if model is not None:
            core.chat.model = model.strip()
        if raw_base_url is not None:
            core.chat.base_url = normalized_base_url
        if api_key:
            core.chat.keys[provider or core.chat.provider] = api_key.strip()
        return JSONResponse({"ok": True, "provider": core.chat.provider, "model": core.chat.model})

    async def session_mode(request) -> JSONResponse:
        """Change the human-owned session mode from the local chat surface."""
        if not core.chat.enabled:
            return _disabled()
        body, error = await _json_object(request)
        if error is not None:
            return error
        assert body is not None
        # Two axes, either of which may be sent on its own: what the
        # assistant may touch, and whether it asks first.
        mode = body.get("mode")
        autonomy = body.get("autonomy")
        if mode is not None and mode not in {"ask", "edit"}:
            return JSONResponse({"error": "mode must be ask or edit"}, status_code=400)
        if autonomy is not None and autonomy not in {"approval", "auto"}:
            return JSONResponse(
                {"error": "autonomy must be approval or auto"}, status_code=400
            )
        if mode is None and autonomy is None:
            return JSONResponse({"error": "mode or autonomy is required"}, status_code=400)
        from ifc_console.policy.modes import Mode

        if (
            mode == "edit"
            and core.policy.mode is not Mode.EDIT
            and body.get("confirmed") is not True
        ):
            return JSONResponse(
                {"error": "edit mode requires explicit confirmation"}, status_code=409
            )
        if (
            autonomy == "auto"
            and not core.ai_autonomy
            and body.get("confirmed") is not True
        ):
            return JSONResponse(
                {"error": "auto autonomy requires explicit confirmation"},
                status_code=409,
            )
        if mode is not None:
            core.set_mode(Mode(mode), by="chat-panel")
        if autonomy is not None:
            core.set_ai_autonomy(autonomy == "auto", by="chat-panel")
        return JSONResponse(
            {
                "ok": True,
                "mode": core.policy.mode.value,
                "ai_autonomy": core.ai_autonomy,
                # Never true for an assistant: saving is the user's alone.
                "ai_save_allowed": core.policy.allow_ai_save,
                "dirty": core.session.dirty,
            }
        )

    async def session_save(request) -> JSONResponse:
        """Write the in-memory model to its file, on the user's say-so.

        The assistant has no route to this. It is reachable only from a
        control the person operating the console clicked.
        """
        if not core.chat.enabled:
            return _disabled()
        session = core.session
        if not session.loaded or session.path is None:
            return JSONResponse({"error": "no model is open"}, status_code=409)
        if not session.dirty:
            return JSONResponse({"ok": True, "saved": False, "dirty": False})
        try:
            await core.save_model(by="chat-panel")
        except Exception as exc:  # surfaced to the person who pressed save
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse(
            {"ok": True, "saved": True, "dirty": core.session.dirty, "path": str(session.path)}
        )

    async def stream(request) -> Response:
        if not core.chat.enabled:
            return _disabled()
        body, error = await _json_object(request)
        if error is not None:
            return error
        assert body is not None
        raw_turns = body.get("turns")
        if not isinstance(raw_turns, list) or len(raw_turns) > _MAX_TURNS:
            return JSONResponse(
                {"error": f"turns must be a list with at most {_MAX_TURNS} entries"},
                status_code=400,
            )
        turns: list[dict[str, str]] = []
        transcript_chars = 0
        for turn in raw_turns:
            if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
                return JSONResponse({"error": "each turn has an invalid role"}, status_code=400)
            text = turn.get("text", "")
            if not isinstance(text, str) or len(text) > _MAX_TURN_CHARS:
                return JSONResponse({"error": "a chat turn is too long"}, status_code=400)
            transcript_chars += len(text)
            if transcript_chars > _MAX_TRANSCRIPT_CHARS:
                return JSONResponse({"error": "chat transcript is too long"}, status_code=400)
            turns.append({"role": turn["role"], "text": text})
        if not turns:
            return JSONResponse({"error": "no messages"}, status_code=400)

        try:
            provider_id = _optional_text(body, "provider", 50)
            model = _optional_text(body, "model", _MAX_MODEL_CHARS)
            base_url = _optional_text(body, "base_url", _MAX_URL_CHARS)
            api_key = _optional_text(body, "api_key", _MAX_KEY_CHARS)
            system = _optional_text(body, "system", _MAX_SYSTEM_CHARS)
            temperature = _number_option(body, "temperature", 0, 2)
            top_p = _number_option(body, "top_p", 0, 1)
            max_tokens = _integer_option(body, "max_tokens", 1, 1_000_000)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        use_tools = body.get("tools", core.settings.chat.tools)
        if not isinstance(use_tools, bool):
            return JSONResponse({"error": "tools must be true or false"}, status_code=400)
        tools_supported = body.get("tools_supported")
        if tools_supported is not None and not isinstance(tools_supported, bool):
            return JSONResponse(
                {"error": "tools_supported must be true, false, or omitted"},
                status_code=400,
            )

        async def events():
            try:
                async for event in converse(
                    core,
                    turns=turns,
                    provider_id=provider_id,
                    model=model,
                    base_url=base_url,
                    api_key=api_key,
                    system=system,
                    use_tools=use_tools,
                    tools_supported=tools_supported,
                    options={
                        "temperature": temperature,
                        "top_p": top_p,
                        "max_tokens": max_tokens,
                    },
                ):
                    yield _sse(event)
            except ProviderError as exc:
                yield _sse({"type": "error", "text": str(exc)})
            except Exception:  # the panel must always finish cleanly
                log.exception("chat stream failed")
                yield _sse({"type": "error", "text": "internal chat error"})
            yield _sse({"type": "done"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return [
        Route("/chat", chat_shell, methods=["GET"]),
        Route("/workflows", workflows_shell, methods=["GET"]),
        Route("/api/chat/providers", providers, methods=["GET"]),
        Route("/api/chat/credentials", credentials, methods=["GET", "POST"]),
        Route("/api/chat/models", models, methods=["POST"]),
        Route("/api/chat/select", remember, methods=["POST"]),
        Route("/api/session/mode", session_mode, methods=["POST"]),
        Route("/api/session/save", session_save, methods=["POST"]),
        Route("/api/chat/stream", stream, methods=["POST"]),
    ]
