"""Static assets for the ifc-console 3D viewer.

Shipped separately so the base install stays small: the bundle is three.js,
the web-ifc WASM parser, and the viewer SPA. Install it with
`pip install ifc-console[viewer]`.
"""

from pathlib import Path

__version__ = "0.1.4"
__all__ = ["__version__", "static_dir"]


def static_dir() -> Path:
    """Directory holding index.html, the SPA modules, and vendor assets."""
    return Path(__file__).resolve().parent / "static"
