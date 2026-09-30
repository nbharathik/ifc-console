"""Which tool profile a request asks for.

A profile only changes what `tools/list` shows. The choice comes, in order, from
the `X-IFC-Console-Tools` header (the bridge sets it), a `/mcp/<profile>` path,
and the `mcp.tool_profile` setting.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from ifc_console.mcp.catalog import PROFILES

HEADER = b"x-ifc-console-tools"
PATH_PREFIX = "/mcp/"

_requested: ContextVar[str | None] = ContextVar("ifc_console_tool_profile", default=None)


def request_profile(scope: dict[str, Any]) -> str | None:
    """The profile a request names, or None. Unknown names are ignored."""
    for name, value in scope.get("headers", []):
        if name.lower() == HEADER:
            wanted = value.decode("latin-1", errors="replace").strip().lower()
            if wanted in PROFILES:
                return wanted
    path = scope.get("path", "")
    if path.startswith(PATH_PREFIX):
        wanted = path[len(PATH_PREFIX) :].strip("/").lower()
        if wanted in PROFILES:
            return wanted
    return None


def rewrite_path(scope: dict[str, Any]) -> dict[str, Any]:
    """`/mcp/lean` reaches the same endpoint as `/mcp`."""
    path = scope.get("path", "")
    if not path.startswith(PATH_PREFIX):
        return scope
    if path[len(PATH_PREFIX) :].strip("/").lower() not in PROFILES:
        return scope
    return {**scope, "path": "/mcp", "raw_path": b"/mcp"}


def bind(profile: str | None):
    """Make `profile` the current request's choice; returns the reset token."""
    return _requested.set(profile)


def unbind(token) -> None:
    _requested.reset(token)


def current(default: str) -> str:
    """The profile for the request being served, else the configured default."""
    chosen = _requested.get()
    if chosen in PROFILES:
        return chosen
    return default if default in PROFILES else PROFILES[0]
