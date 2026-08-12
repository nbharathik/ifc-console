"""Where the optional viewer and chat browser bundle lives.

The three.js and web-ifc assets ship in ``ifc-console-viewer``, pulled in by
the ``viewer`` extra. The base ``ifc-console`` wheel therefore contains none
of those third-party browser files.
"""

from __future__ import annotations

import functools
from pathlib import Path

INSTALL_HINT = (
    "the viewer assets are not installed; add them with "
    '`uv tool install "ifc-console[viewer]"` or `pip install "ifc-console[viewer]"`'
)

# Source checkouts can resolve the workspace package before it is installed,
# which keeps local tests and ``uv run`` convenient.
_IN_TREE = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "ifc-console-viewer"
    / "src"
    / "ifc_console_viewer"
    / "static"
)


@functools.lru_cache(maxsize=1)
def static_dir() -> Path | None:
    """The bundle directory, or ``None`` when the viewer extra is absent."""
    try:
        import ifc_console_viewer

        candidate = ifc_console_viewer.static_dir()
        if (candidate / "index.html").is_file():
            return candidate
    except Exception:
        # Missing or incomplete optional packages must not stop core startup.
        pass
    if (_IN_TREE / "index.html").is_file():
        return _IN_TREE
    return None


def available() -> bool:
    return static_dir() is not None


def require_static_dir() -> Path:
    directory = static_dir()
    if directory is None:
        raise FileNotFoundError(INSTALL_HINT)
    return directory
