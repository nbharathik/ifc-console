"""Deprecated compatibility namespace for :mod:`ifc_console_agents.devkit`.

This one-release import bridge will be removed in IFC Console 0.2.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import warnings as _warnings

_warnings.warn(
    "ifc_console.devkit is deprecated and will be removed in 0.2; "
    "import from ifc_console_agents.devkit instead",
    DeprecationWarning,
    stacklevel=2,
)

for _suffix in ("checks", "rehearsal", "scenario", "serve"):
    _module = _importlib.import_module(f"ifc_console_agents.devkit.{_suffix}")
    _sys.modules[f"{__name__}.{_suffix}"] = _module
    globals()[_suffix] = _module

from ifc_console_agents.devkit import *  # noqa: E402,F403
from ifc_console_agents.devkit import __all__  # noqa: E402,F401
