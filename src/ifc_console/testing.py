"""Deprecated compatibility alias for :mod:`ifc_console_agents.testing`."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import warnings as _warnings

_warnings.warn(
    "ifc_console.testing is deprecated and will be removed in 0.2; "
    "import ifc_console_agents.testing instead",
    DeprecationWarning,
    stacklevel=2,
)
_sys.modules[__name__] = _importlib.import_module("ifc_console_agents.testing")
