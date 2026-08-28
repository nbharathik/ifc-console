"""Deprecated compatibility namespace for :mod:`ifc_console_agents.chat`.

This one-release import bridge will be removed in IFC Console 0.2.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import warnings as _warnings

_warnings.warn(
    "ifc_console.chat is deprecated and will be removed in 0.2; "
    "import from ifc_console_agents.chat instead",
    DeprecationWarning,
    stacklevel=2,
)

for _suffix in ("agent", "providers", "routes"):
    _module = _importlib.import_module(f"ifc_console_agents.chat.{_suffix}")
    _sys.modules[f"{__name__}.{_suffix}"] = _module
    globals()[_suffix] = _module

from ifc_console_agents.chat import SYSTEM_PROMPT, ChatState  # noqa: E402

__all__ = ["ChatState", "SYSTEM_PROMPT"]
