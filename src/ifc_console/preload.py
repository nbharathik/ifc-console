"""Background warm-up of the heavy imports the console needs at startup.

Two independent costs dominate a cold start: the terminal UI stack (textual
and rich, about 0.5 s) and the backend stack (MCP SDK, uvicorn/starlette,
ifcopenshell). Both are warmed on threads while the main thread builds
settings and AppCore, so the console paints roughly twice as fast.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

_lock = threading.Lock()
_threads: list[threading.Thread] = []
_backend: threading.Thread | None = None
_ui: threading.Thread | None = None
# Held shut until the main thread is done with its own first-time imports.
# pydantic (and other lazy-__getattr__ packages) are not safe to enter from
# two threads at once: the loser sees a half-built module and raises.
_gate = threading.Event()


def _import_ui() -> None:
    """Warm textual/rich. Ungated: neither touches pydantic or ifc_console."""
    try:
        import textual.app  # noqa: F401
        import textual.widgets  # noqa: F401
    except Exception:
        pass


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


def _spawn(target: Callable[[], None], name: str) -> threading.Thread:
    thread = threading.Thread(target=target, name=name, daemon=True)
    _threads.append(thread)
    thread.start()
    return thread


def start(*, ui: bool = False) -> threading.Thread:
    """Begin warming once; safe to call repeatedly, in any order.

    ``ui=True`` also warms the terminal UI stack, for callers that go on to
    run the console. Returns the backend thread.
    """
    global _backend, _ui
    with _lock:
        if _backend is None:
            _backend = _spawn(_import_backend, "ifc-console-preload")
        if ui and _ui is None:
            _ui = _spawn(_import_ui, "ifc-console-preload-ui")
        return _backend


def release() -> None:
    """Let the warm-up proceed. Call once the caller's own imports are done."""
    _gate.set()


def wait() -> None:
    """Block until the warm-up finishes (starts it if needed)."""
    start()
    release()
    for thread in list(_threads):
        thread.join()
