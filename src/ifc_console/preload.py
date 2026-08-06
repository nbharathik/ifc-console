"""Background warm-up of the heavy backend imports.

The MCP SDK, uvicorn/starlette, and ifcopenshell together dominate startup
time. Importing them on a thread while the terminal UI boots keeps the
console responsive and brings the server up seconds earlier.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_thread: threading.Thread | None = None
# Held shut until the main thread is done with its own first-time imports.
# pydantic (and other lazy-__getattr__ packages) are not safe to enter from
# two threads at once: the loser sees a half-built module and raises.
_gate = threading.Event()


def _import_backend() -> None:
    _gate.wait()
    try:
        import starlette.requests  # noqa: F401
        import starlette.responses  # noqa: F401
        import starlette.routing  # noqa: F401
        import uvicorn  # noqa: F401

        import ifc_console.policy.guards  # noqa: F401
        from ifc_console.mcp import (  # noqa: F401
            prompts,
            resources,
            server,
            tools_analysis,
            tools_exec,
            tools_files,
            tools_query,
            tools_viewer,
        )
    except Exception:
        # swallowed here; the real import in the caller reports the failure
        pass


def start() -> threading.Thread:
    """Begin warming once; safe to call repeatedly."""
    global _thread
    with _lock:
        if _thread is None:
            _thread = threading.Thread(
                target=_import_backend, name="ifc-console-preload", daemon=True
            )
            _thread.start()
        return _thread


def release() -> None:
    """Let the warm-up proceed. Call once the caller's own imports are done."""
    _gate.set()


def wait() -> None:
    """Block until the warm-up finishes (starts it if needed)."""
    thread = start()
    release()
    thread.join()
