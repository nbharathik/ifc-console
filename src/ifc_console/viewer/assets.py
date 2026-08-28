"""Locate the browser workspace bundled with :mod:`ifc_console`.

The viewer is part of the main application: a plain ``ifc-console`` install
must be able to open the browser without resolving a companion distribution.
Keeping this tiny boundary still gives release checks and downstream embedders
one canonical way to find the reviewed static files.
"""

from __future__ import annotations

from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"
INSTALL_HINT = "the bundled viewer assets are incomplete; reinstall ifc-console"


def static_dir() -> Path:
    """Return the directory containing the bundled browser application."""
    return STATIC_DIR


def available() -> bool:
    """Whether this installation contains its required browser entry point."""
    return (STATIC_DIR / "index.html").is_file()


def require_static_dir() -> Path:
    if not available():
        raise FileNotFoundError(INSTALL_HINT)
    return STATIC_DIR
