"""MCP server construction: FastMCP instance, transports, token auth."""

from __future__ import annotations

import functools
import hmac
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from mcp.server.fastmcp import FastMCP

from ifc_console.mcp.envelope import ToolError, err, from_tool_error

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.mcp")

INSTRUCTIONS = """\
You are connected to ifc-console, a standalone IFC (BIM) workbench. One IFC file
is loaded per session; every response's `meta` tells you the model, schema,
mode, dirty flag, and fingerprint (if the fingerprint changes, re-orient).

Session modes (the USER controls them in their terminal; you cannot):
- ask (the default): query freely; anything that would change the model or
  write a file fails with ASK_MODE_BLOCKED. Writing and showing code is
  always fine; to actually make changes, ask the user to switch to edit
  mode (/mode edit in the ifc-console terminal), then retry.
- edit: mutations and saves run; saving still makes automatic backups.

Workflow:
1. get_session_status / get_ifc_project_info to orient.
2. Prefer structured tools (query_elements, get_element, get_psets,
   get_spatial_structure) over code; they are cheaper and safer.
3. Check get_schema_docs before writing code against unfamiliar entities.
4. execute_ifc_code for anything the tools don't cover. Namespace: ifc,
   ifcopenshell, ifc_api, element_util, selector_util, unit_util, query(sel),
   get_ifc_file(). stdout is captured; a final bare expression is returned.
   For mutating runs, fill `description` with one line of intent; the user
   sees it in their terminal and audit log.
5. After mutations the model is dirty (meta.dirty=true); finish the batch
   with save_ifc_file. Don't save after every micro-edit.
6. Errors come back as {ok:false, error:{code, message, hint}}; follow the
   hint instead of retrying blindly.

Selector examples for query_elements: `IfcWall` · `IfcWall, IfcSlab` ·
`IfcWall, material=concrete` · `IfcWall, Pset_WallCommon.FireRating=F30` ·
`IfcElement, Name=/W.*/`.

The 3D viewer is optional. When the user enables it, three extra tools join
the tool list: get_viewer_selection (what the user click-selected, their way
of saying "this wall"), highlight_elements (your way of pointing for them),
and get_viewer_screenshot (see the scene; use highlight + screenshot to
verify visual claims). If those tools are absent or report
VIEWER_NOT_CONNECTED, the user can start the viewer by typing /viewer in the
ifc-console terminal; do not retry without it.

Never attempt OS, network, or file-system access from execute_ifc_code; that
class of code is blocked unless the user explicitly enables it.
"""


def enveloped(core: AppCore, tool_name: str) -> Callable:
    """Wrap a tool coroutine: ToolError → err envelope; audit + event on every call."""

    def decorate(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> str:
            start = time.perf_counter()
            ok_flag, detail = True, ""
            try:
                return await fn(*args, **kwargs)
            except ToolError as exc:
                ok_flag, detail = False, exc.code
                return from_tool_error(exc, core.session_meta())
            except Exception as exc:  # never crash the protocol
                log.exception("tool %s failed", tool_name)
                ok_flag, detail = False, "INTERNAL_ERROR"
                return err(
                    "INTERNAL_ERROR",
                    f"{type(exc).__name__}: {exc}",
                    "This is an ifc-console bug; the audit log has details.",
                    core.session_meta(),
                )
            finally:
                core.tool_event(
                    tool_name,
                    ok=ok_flag,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    detail=detail,
                )

        return wrapper

    return decorate


def build_mcp(core: AppCore) -> FastMCP:
    """Core tools always; the viewer tool category only while the viewer is
    enabled (attach_mcp keeps it in sync as /viewer toggles at runtime)."""
    from ifc_console.mcp import tools_exec, tools_files, tools_query

    mcp = FastMCP("ifc-console", instructions=INSTRUCTIONS)
    tools_query.register(mcp, core)
    tools_exec.register(mcp, core)
    tools_files.register(mcp, core)
    core.attach_mcp(mcp)
    return mcp


class TokenAuthMiddleware:
    """Bearer-token gate for /mcp, /api, /ws and the viewer shell.

    Accepts `Authorization: Bearer <token>` or `?t=<token>` (the viewer URL
    form). Static viewer assets stay public: they are generic vendor JS/WASM
    plus our SPA source and contain nothing session-specific. Lifespan and
    unprotected paths pass through untouched.
    """

    PROTECTED = ("/mcp", "/api", "/ws", "/viewer")
    PUBLIC = ("/viewer/static",)

    def __init__(self, app: Any, token: str, protected: tuple[str, ...] = PROTECTED):
        self.app = app
        self.token = token
        self.protected = protected

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if any(path == p or path.startswith(p + "/") for p in self.PUBLIC):
            await self.app(scope, receive, send)
            return
        if not any(path == p or path.startswith(p + "/") for p in self.protected):
            await self.app(scope, receive, send)
            return
        if self._authorized(scope):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps(
            {
                "error": "unauthorized",
                "hint": "pass Authorization: Bearer <session token>; /copy <client> "
                "in the ifc-console terminal copies a complete client setup",
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _authorized(self, scope: dict) -> bool:
        expected = f"Bearer {self.token}".encode()
        for name, value in scope.get("headers", []):
            if name == b"authorization" and hmac.compare_digest(value, expected):
                return True
        qs = parse_qs(scope.get("query_string", b"").decode(errors="ignore"))
        return any(hmac.compare_digest(t, self.token) for t in qs.get("t", []))


def build_http_app(core: AppCore, mcp: FastMCP) -> Any:
    """Starlette app: streamable HTTP MCP at /mcp, status + viewer routes,
    everything sensitive behind the token middleware."""
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    from ifc_console.viewer.routes import build_static_app, build_viewer_routes

    app = mcp.streamable_http_app()

    async def status(_request: Request) -> JSONResponse:
        s = core.session
        return JSONResponse(
            {
                "server": {"name": "ifc-console"},
                "meta": core.session_meta(),
                "model": s.name,
                "schema": s.schema,
                "mode": core.policy.mode.value,
                "dirty": s.dirty,
                "fingerprint": s.fingerprint,
                "etag": core.viewer_hub.model_etag(),
                "selection": list(core.viewer_hub.selection),
                "viewer": {
                    "enabled": core.viewer.enabled,
                    "connected": core.viewer.connected,
                },
            }
        )

    extra: list[Any] = [Route("/api/status", status, methods=["GET"])]
    extra.extend(build_viewer_routes(core))
    extra.append(Mount("/viewer/static", app=build_static_app(), name="viewer-static"))
    app.router.routes[0:0] = extra
    return TokenAuthMiddleware(app, core.token)


def make_uvicorn_server(app: Any, port: int) -> Any:
    """A uvicorn Server suitable for co-hosting on an existing event loop."""
    import uvicorn

    class NoSignalServer(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # the TUI owns signals
            pass

    config = uvicorn.Config(
        app,
        host="127.0.0.1",  # hard-locked loopback; never expose the token remotely
        port=port,
        log_config=None,
        access_log=False,
        lifespan="on",
    )
    return NoSignalServer(config)
