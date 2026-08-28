"""Deprecated compatibility namespace for :mod:`ifc_console_agents`.

This one-release import bridge will be removed in IFC Console 0.2.
"""

from __future__ import annotations

import importlib as _importlib
import sys as _sys
import warnings as _warnings

_warnings.warn(
    "ifc_console.agents is deprecated and will be removed in 0.2; "
    "import from ifc_console_agents instead (use "
    "ifc_console.ifc.ai_provenance for provenance)",
    DeprecationWarning,
    stacklevel=2,
)

_MODULES = {
    "agent": "ifc_console_agents.agent",
    "approvals": "ifc_console_agents.approvals",
    "blocks": "ifc_console_agents.blocks",
    "blueprints": "ifc_console_agents.blueprints",
    "builtin": "ifc_console_agents.builtin",
    "builtin.docs": "ifc_console_agents.builtin.docs",
    "builtin.measure": "ifc_console_agents.builtin.measure",
    "content": "ifc_console_agents.content",
    "delegation": "ifc_console_agents.delegation",
    "environment": "ifc_console_agents.environment",
    "files": "ifc_console_agents.files",
    "models": "ifc_console_agents.models",
    "packs": "ifc_console_agents.packs",
    "panel": "ifc_console_agents.panel",
    "presets": "ifc_console_agents.presets",
    "proposals": "ifc_console_agents.proposals",
    "provenance": "ifc_console.ifc.ai_provenance",
    "providers": "ifc_console_agents.providers",
    "runner": "ifc_console_agents.runner",
    "skills": "ifc_console_agents.skills",
    "storage": "ifc_console_agents.storage",
    "workspace": "ifc_console_agents.workspace",
}

for _suffix, _target in _MODULES.items():
    _module = _importlib.import_module(_target)
    _sys.modules[f"{__name__}.{_suffix}"] = _module
    if "." not in _suffix:
        globals()[_suffix] = _module

for _suffix in ("docs", "measure"):
    setattr(globals()["builtin"], _suffix, _sys.modules[f"{__name__}.builtin.{_suffix}"])

from ifc_console_agents import *  # noqa: E402,F403
from ifc_console_agents import __all__  # noqa: E402,F401
