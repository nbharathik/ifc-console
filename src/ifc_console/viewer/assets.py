"""Where the viewer's static bundle lives.

The 8 MB of three.js, WASM, and SPA source ships as a separate distribution
(`ifc-console-viewer`, pulled in by the `viewer` extra) so the base install
stays small. Nothing else in ifc-console depends on it.
"""

from __future__ import annotations

import functools
from pathlib import Path

INSTALL_HINT = (
    "the viewer assets are not installed; add them with "
    "`uv tool install \"ifc-console[viewer]\"` or `pip install \"ifc-console[viewer]\"`"
)

# A source checkout can serve the assets straight from the repo, so tests and
# `uv run` work without installing the companion package first.
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
    """The bundle directory, or None when the extra is not installed."""
    try:
        import ifc_console_viewer

        candidate = ifc_console_viewer.static_dir()
        if (candidate / "index.html").is_file():
            return candidate
    except Exception:
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
