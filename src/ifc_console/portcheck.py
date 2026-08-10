"""Who is on that port? Authenticated identification for conflict errors.

MCP clients pin http://127.0.0.1:<port>/mcp, so when the port is occupied it
matters a lot whether the occupant is your own running ifc-console session
(fine: clients talk to it) or some other application. The probe uses a fresh
nonce and keyed proof, so it never sends the bearer token to an unverified
listener.
"""

from __future__ import annotations

import json
import secrets
import socket
import urllib.request

from ifc_console.http_identity import (
    IDENTITY_NONCE_BYTES,
    IDENTITY_PATH,
    IDENTITY_RESPONSE_LIMIT,
    identity_matches,
)

# Occupant kinds, from best to worst.
FREE = "free"
IFC_CONSOLE = "ifc-console"  # an ifc-console session that accepts our token
IFC_CONSOLE_OTHER = "ifc-console-other"  # ifc-console, but a different/unknown token
FOREIGN = "foreign"  # something else entirely


def classify_http(status_code: int, body: str) -> tuple[str, str]:
    """Classify an HTTP answer from 127.0.0.1:<port>/api/status."""
    is_ours = "ifc-console" in body
    if status_code == 200 and is_ours:
        return IFC_CONSOLE, "your running ifc-console session (same token)"
    if is_ours:
        return IFC_CONSOLE_OTHER, "an ifc-console session with a different token"
    return FOREIGN, f"an application that is not ifc-console (HTTP {status_code})"


def port_status(port: int, token: str | None = None) -> tuple[str, str]:
    """(kind, human detail) for 127.0.0.1:<port>.

    Bind check first (the actual question is "can ifc-console use it"), then an
    identification request against whatever is listening.
    """
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return FREE, f"{port} free"
        except OSError:
            pass

    payload, nonce = _probe_identity(port)
    if payload is None:
        return FOREIGN, "an application that is not ifc-console (no HTTP answer)"
    if payload.get("name") != "ifc-console":
        return FOREIGN, "an application that is not ifc-console"
    if token and identity_matches(payload, token, nonce, port):
        return IFC_CONSOLE, "your running ifc-console session (same token)"
    return IFC_CONSOLE_OTHER, "an unverified ifc-console listener (different token)"


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the Authorization header to another host."""

    def redirect_request(self, *args, **kwargs):
        return None


def _probe_identity(port: int) -> tuple[dict | None, str]:
    nonce = secrets.token_hex(IDENTITY_NONCE_BYTES)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{IDENTITY_PATH}?nonce={nonce}",
        headers={"Accept": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
    try:
        with opener.open(request, timeout=2) as response:
            raw = response.read(IDENTITY_RESPONSE_LIMIT + 1)
            content_type = response.headers.get("Content-Type", "")
        if len(raw) > IDENTITY_RESPONSE_LIMIT:
            return None, nonce
        if content_type.partition(";")[0].strip().casefold() != "application/json":
            return None, nonce
        payload = json.loads(raw.decode("utf-8"))
        return (payload if isinstance(payload, dict) else None), nonce
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        return None, nonce


def conflict_hint(kind: str, port: int) -> str:
    """One actionable sentence for an occupied port."""
    if kind == IFC_CONSOLE:
        return (
            "that session is already serving your clients; for a second "
            f"session pick another port (--port) and remember clients pin "
            f"http://127.0.0.1:{port}/mcp"
        )
    if kind == IFC_CONSOLE_OTHER:
        return (
            "another ifc-console answers there but rejects this token; check "
            "IFC_CONSOLE_HOME / `ifc-console token show`, or use --port"
        )
    return (
        "a different application owns this port; the stdio bridge will not "
        "send it the bearer token. Move ifc-console "
        "permanently with `ifc-console settings set server.port <n>` and "
        "re-add clients (`ifc-console mcp-config`)"
    )


__all__ = [
    "FREE",
    "IFC_CONSOLE",
    "IFC_CONSOLE_OTHER",
    "FOREIGN",
    "classify_http",
    "conflict_hint",
    "port_status",
]
