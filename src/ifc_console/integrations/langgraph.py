"""Deprecated compatibility alias for the agents LangGraph adapter."""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import warnings as _warnings

_warnings.warn(
    "ifc_console.integrations.langgraph is deprecated and will be removed in 0.2; "
    "import ifc_console_agents.integrations.langgraph instead",
    DeprecationWarning,
    stacklevel=2,
)
_sys.modules[__name__] = _importlib.import_module(
    "ifc_console_agents.integrations.langgraph"
)
