"""Locate the viewer and chat static bundle included with ifc-console."""

from __future__ import annotations

import functools
from pathlib import Path

INSTALL_HINT = "the bundled viewer assets are missing; reinstall `ifc-console`"

_STATIC = Path(__file__).resolve().parent / "static"


@functools.lru_cache(maxsize=1)
def static_dir() -> Path | None:
    """The bundled asset directory, or None for an incomplete installation."""
    if (_STATIC / "index.html").is_file():
        return _STATIC
    return None


def available() -> bool:
    return static_dir() is not None


def require_static_dir() -> Path:
    directory = static_dir()
    if directory is None:
        raise FileNotFoundError(INSTALL_HINT)
    return directory
