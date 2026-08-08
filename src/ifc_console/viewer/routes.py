"""Viewer HTTP + WebSocket routes.

Mounted into the same Starlette app as /mcp. Every route except the static
assets is token-gated by TokenAuthMiddleware; on top of that, everything here
answers 404 while the viewer is disabled so a session without --viewer exposes
no viewer surface at all.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

from ifc_console.core.results import ToolError
from ifc_console.ifc.elements import element_detail
from ifc_console.ifc.query import element_row, search_elements
from ifc_console.viewer import assets

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.viewer")

# Resolved through the companion package; None when the extra is missing.
STATIC_DIR = assets.static_dir()

# How long a fresh socket may sit silent before the hello must have arrived.
_HELLO_TIMEOUT = 5.0

# Viewer search: short terms match too much to be worth a round trip, and the
# result list is a panel, not a report.
_SEARCH_MIN = 2
_SEARCH_LIMIT = 200
_SEARCH_MAX = 1000

# The SPA makes zero non-localhost requests; the CSP makes that a guarantee.
# script-src needs 'unsafe-eval': web-ifc's embind glue builds its invoker
# functions with new Function(...) at runtime. Scripts still load exclusively
# from 'self' on a token-gated loopback origin.
_CSP = (
    "default-src 'self'; "
    "connect-src 'self' ws://127.0.0.1:* ws://localhost:*; "
    "img-src 'self' blob: data:; "
    "worker-src 'self' blob:; "
    "script-src 'self' 'unsafe-eval' 'wasm-unsafe-eval'; "
    "style-src 'self'"
)


def _disabled_response() -> JSONResponse:
    return JSONResponse(
        {
            "error": "viewer_disabled",
            "hint": "start ifc-console with --viewer, or type /viewer in the terminal",
        },
        status_code=404,
    )


def _tool_error_response(exc: ToolError, status: int) -> JSONResponse:
    return JSONResponse(
        {"error": exc.code, "message": exc.message, "hint": exc.hint},
        status_code=status,
    )


def build_viewer_routes(core: AppCore) -> list[Any]:
    """Routes for the SPA shell, model/element APIs, and the live WebSocket."""

    async def viewer_shell(request) -> Response:
        if not core.viewer.enabled:
            return _disabled_response()
        directory = assets.static_dir()
        if directory is None:
            return JSONResponse({"error": "viewer_not_installed", "hint": assets.INSTALL_HINT}, 501)
        return FileResponse(
            directory / "index.html",
            headers={"Content-Security-Policy": _CSP, "Cache-Control": "no-cache"},
        )

    def _requested_session(request):
        """The model the tab asked for; the active one unless it names another."""
        model_id = request.query_params.get("model")
        if not model_id:
            return core.session
        return core.models.get(model_id)

    async def model_ifc(request) -> Response:
        if not core.viewer.enabled:
            return _disabled_response()
        session = _requested_session(request)
        if session is None:
            return JSONResponse(
                {
                    "error": "MODEL_NOT_FOUND",
                    "hint": "the model was detached; reload the viewer",
                },
                status_code=404,
            )
        if not session.loaded:
            return JSONResponse(
                {"error": "NO_MODEL_LOADED", "hint": "open a model in the terminal"},
                status_code=404,
            )
        max_mb = core.settings.viewer.max_model_mb
        if session.size_bytes > max_mb * 1_048_576:
            return JSONResponse(
                {
                    "error": "MODEL_TOO_LARGE",
                    "message": f"model is {session.size_bytes / 1_048_576:.0f} MB, "
                    f"viewer limit is {max_mb} MB",
                    "hint": "raise viewer.max_model_mb in settings if you really want to try",
                },
                status_code=413,
            )
        etag = core.viewer_hub.model_etag(session) or ""
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        # Fast path: an unmodified .ifc on disk already holds exactly the bytes
        # the viewer wants, and sendfile beats re-serializing by ~100x on a
        # large model. Anything else (dirty, saved elsewhere, .ifczip/.ifcxml)
        # falls through to the serializer.
        if session.path.suffix.lower() == ".ifc" and session.matches_disk():
            return FileResponse(session.path, media_type="application/x-step", headers=headers)
        data = core.viewer_hub.cached_model_bytes(etag)
        if data is None:
            # Single-flight: N tabs opening at once must not each queue a full
            # serialization onto the single model worker.
            async with core.viewer_hub.model_bytes_lock:
                data = core.viewer_hub.cached_model_bytes(etag)
                if data is None:
                    # Serialization runs on the model worker: the file object is
                    # not thread-safe and edits must not interleave with it.
                    data = await session.run(
                        lambda: session.ifc.to_string().encode("utf-8"), timeout=600
                    )
                    core.viewer_hub.cache_model_bytes(etag, data)
        return Response(data, media_type="application/x-step", headers=headers)

    async def element(request) -> Response:
        if not core.viewer.enabled:
            return _disabled_response()
        session = _requested_session(request)
        if session is None or not session.loaded:
            return JSONResponse({"error": "NO_MODEL_LOADED"}, status_code=404)
        guid = request.path_params["guid"]

        def job() -> dict | None:
            try:
                entity = session.ifc.by_guid(guid)
            except Exception:
                return None
            if entity is None:
                return None
            return element_detail(
                entity,
                (
                    "attributes",
                    "psets",
                    "qtos",
                    "type",
                    "container",
                    "materials",
                    "decomposition",
                ),
            )

        try:
            detail = await session.run(job, timeout=60)
        except ToolError as exc:
            return _tool_error_response(exc, status=503)
        if detail is None:
            return JSONResponse({"error": "ELEMENT_NOT_FOUND", "guid": guid}, status_code=404)
        return JSONResponse(detail)

    async def search(request) -> Response:
        if not core.viewer.enabled:
            return _disabled_response()
        session = _requested_session(request)
        if session is None or not session.loaded:
            return JSONResponse({"error": "NO_MODEL_LOADED"}, status_code=404)
        term = (request.query_params.get("q") or "").strip()
        if len(term) < _SEARCH_MIN:
            return JSONResponse({"query": term, "mode": "text", "results": [], "total": 0})
        try:
            limit = int(request.query_params.get("limit") or _SEARCH_LIMIT)
        except ValueError:
            limit = _SEARCH_LIMIT
        limit = max(1, min(limit, _SEARCH_MAX))

        def job() -> dict:
            matches, mode = search_elements(session.ifc, term)
            total = len(matches)
            rows = [
                element_row(entity, ("name", "type_name", "storey")) for entity in matches[:limit]
            ]
            return {
                "query": term,
                "mode": mode,
                "total": total,
                "truncated": total > limit,
                "results": rows,
            }

        try:
            payload = await session.run(job, timeout=60)
        except ToolError as exc:
            return _tool_error_response(exc, status=503)
        return JSONResponse(payload)

    async def ws_endpoint(websocket: WebSocket) -> None:
        if not core.viewer.enabled:
            await websocket.close(code=4404)
            return
        await websocket.accept()
        hub = core.viewer_hub
        client = None
        try:
            # Browsers cannot attach an Authorization header to a WS upgrade,
            # so the first frame MUST be a hello carrying the session token;
            # nothing is served before it verifies.
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=_HELLO_TIMEOUT)
                frame = json.loads(raw)
            except (asyncio.TimeoutError, json.JSONDecodeError):
                await websocket.close(code=4401)
                return
            token = frame.get("token") if isinstance(frame, dict) else None
            if (
                not isinstance(frame, dict)
                or frame.get("type") != "hello"
                or not isinstance(token, str)
                or not hmac.compare_digest(token, core.token)
            ):
                await websocket.close(code=4401)
                return
            client = hub.register(websocket)
            await client.send(hub.status_payload())
            while True:
                raw = await websocket.receive_text()
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(frame, dict):
                    await hub.handle_frame(client, frame)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("viewer websocket failed")
        finally:
            if client is not None:
                hub.unregister(client)

    return [
        Route("/viewer", viewer_shell, methods=["GET"]),
        Route("/api/model.ifc", model_ifc, methods=["GET"]),
        Route("/api/elements/{guid}", element, methods=["GET"]),
        Route("/api/search", search, methods=["GET"]),
        WebSocketRoute("/ws", ws_endpoint),
    ]


class _RevalidatingStatic(StaticFiles):
    """StaticFiles that always revalidates.

    The asset URLs never change, so without this a browser keeps serving the
    viewer it cached before an ifc-console upgrade. `no-cache` still allows a
    304, which costs nothing on loopback.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def build_static_app() -> StaticFiles:
    """Static assets (JS/CSS/WASM). Public by design: they are generic vendor
    code plus our SPA source and contain nothing session-specific."""
    return _RevalidatingStatic(directory=assets.require_static_dir())
