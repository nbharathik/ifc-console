"""Chat HTTP routes: the panel shell, provider discovery, and the SSE stream.

Same rules as the viewer: token-gated by TokenAuthMiddleware, 404 while chat
is disabled, and the page shell is the one path a browser may fetch with only
the fragment token.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ifc_console.chat import SYSTEM_PROMPT
from ifc_console.chat.agent import converse
from ifc_console.chat.providers import PROVIDERS, ProviderError, key_source, list_models
from ifc_console.viewer import assets

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.chat")

# The panel talks only to this server; the provider call happens server side,
# which is what keeps keys out of the browser and the CSP this tight.
_CSP = (
    "default-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' blob: data:; "
    "script-src 'self'; "
    "style-src 'self'"
)


def _disabled() -> JSONResponse:
    return JSONResponse(
        {
            "error": "chat_disabled",
            "hint": "type /chat in the ifc-console terminal to turn the panel on",
        },
        status_code=404,
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def build_chat_routes(core: AppCore) -> list[Route]:
    async def chat_shell(_request) -> Response:
        if not core.chat.enabled:
            return _disabled()
        directory = assets.static_dir()
        if directory is None:
            return JSONResponse(
                {"error": "chat_not_installed", "hint": assets.INSTALL_HINT}, status_code=501
            )
        return FileResponse(
            directory / "chat.html",
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

    async def models(request) -> JSONResponse:
        if not core.chat.enabled:
            return _disabled()
        body = await request.json()
        provider = PROVIDERS.get((body.get("provider") or "").lower())
        if provider is None:
            return JSONResponse({"error": "unknown provider"}, status_code=400)
        from ifc_console.chat.providers import resolve_key

        key = resolve_key(provider, body.get("api_key") or core.chat.key_for(provider.id))
        import asyncio

        try:
            names = await asyncio.to_thread(
                list_models, provider, key, body.get("base_url") or None
            )
        except ProviderError as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)
        return JSONResponse({"models": names})

    async def remember(request) -> JSONResponse:
        """Hold a key and the model choice for this run only. Never written."""
        if not core.chat.enabled:
            return _disabled()
        body = await request.json()
        provider = (body.get("provider") or "").lower()
        if provider and provider not in PROVIDERS:
            return JSONResponse({"error": "unknown provider"}, status_code=400)
        if provider:
            core.chat.provider = provider
        if body.get("model") is not None:
            core.chat.model = str(body["model"]).strip()
        if body.get("base_url") is not None:
            core.chat.base_url = str(body["base_url"]).strip()
        if body.get("api_key"):
            core.chat.keys[provider or core.chat.provider] = str(body["api_key"]).strip()
        return JSONResponse({"ok": True, "provider": core.chat.provider, "model": core.chat.model})

    async def stream(request) -> Response:
        if not core.chat.enabled:
            return _disabled()
        body = await request.json()
        turns = [
            {"role": turn.get("role", "user"), "text": str(turn.get("text", ""))}
            for turn in body.get("turns") or []
            if turn.get("role") in ("user", "assistant")
        ]
        if not turns:
            return JSONResponse({"error": "no messages"}, status_code=400)

        async def events():
            try:
                async for event in converse(
                    core,
                    turns=turns,
                    provider_id=body.get("provider"),
                    model=body.get("model"),
                    base_url=body.get("base_url"),
                    api_key=body.get("api_key"),
                    system=body.get("system"),
                    use_tools=bool(body.get("tools", core.settings.chat.tools)),
                    options={
                        "temperature": body.get("temperature"),
                        "top_p": body.get("top_p"),
                        "max_tokens": body.get("max_tokens"),
                    },
                ):
                    yield _sse(event)
            except ProviderError as exc:
                yield _sse({"type": "error", "text": str(exc)})
            except Exception as exc:  # the panel must always finish cleanly
                log.exception("chat stream failed")
                yield _sse({"type": "error", "text": f"{type(exc).__name__}: {exc}"})
            yield _sse({"type": "done"})

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return [
        Route("/chat", chat_shell, methods=["GET"]),
        Route("/api/chat/providers", providers, methods=["GET"]),
        Route("/api/chat/models", models, methods=["POST"]),
        Route("/api/chat/select", remember, methods=["POST"]),
        Route("/api/chat/stream", stream, methods=["POST"]),
    ]
