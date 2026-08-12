"""Static browser assets for the optional ifc-console viewer."""

from pathlib import Path

__version__ = "0.1.4"
__all__ = ["__version__", "static_dir"]


def static_dir() -> Path:
    """Return the directory holding the viewer application and vendor assets."""
    return Path(__file__).resolve().parent / "static"
