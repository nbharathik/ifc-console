"""Who is on that port? Identification for friendly conflict errors.

MCP clients pin http://127.0.0.1:<port>/mcp, so when the port is occupied it
matters a lot whether the occupant is your own running ifc-console session
(fine: clients talk to it) or some other application (bad: clients would
send their requests, bearer token included, to a stranger). The probe asks
the occupant to identify itself before we word the error.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

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

    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/status")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            body = response.read(4096).decode("utf-8", errors="replace")
            return classify_http(response.status, body)
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(4096).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return classify_http(exc.code, body)
    except Exception:
        return FOREIGN, "an application that is not ifc-console (no HTTP answer)"


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
        "a different application owns this port, and MCP clients pointing "
        "here would send it their requests and bearer token; move ifc-console "
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
