"""Locate and serve the browser assets bundled with the agents extension."""

from __future__ import annotations

from pathlib import Path

from starlette.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"
INSTALL_HINT = "the bundled agent assets are incomplete; reinstall ifc-console-agents"


def static_dir() -> Path:
    """Return the directory containing the agent browser panel."""
    return STATIC_DIR


def available() -> bool:
    """Whether this installation contains its required browser entry point."""
    return (STATIC_DIR / "chat.html").is_file() and (STATIC_DIR / "chat.js").is_file()


def require_static_dir() -> Path:
    if not available():
        raise FileNotFoundError(INSTALL_HINT)
    return STATIC_DIR


def static_app() -> StaticFiles:
    """Build the ASGI application mounted at ``/agents/static``."""
    return StaticFiles(directory=require_static_dir(), check_dir=True)


__all__ = ["available", "require_static_dir", "static_app", "static_dir"]
