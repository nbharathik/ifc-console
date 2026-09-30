"""External agent engines (OpenCode, Codex, Goose, ...) behind the panel.

An engine speaks the Agent Client Protocol over stdio. ``HarnessAgent`` puts
one behind the same ``stream()`` surface as the bundled ``Agent``; the
console's tools reach it as an injected MCP server.
"""

from __future__ import annotations

from ifc_console.agents.harness.agent import HarnessAgent, HarnessPack, session_instructions
from ifc_console.agents.harness.engine import (
    PROVIDER_PREFIX,
    TEMPLATES,
    EngineRegistry,
    EngineSpec,
    engine_name,
    is_harness_provider,
    mcp_servers_for,
    template_settings,
)
from ifc_console.agents.harness.runtime import HarnessRuntime

__all__ = [
    "PROVIDER_PREFIX",
    "TEMPLATES",
    "EngineRegistry",
    "EngineSpec",
    "HarnessAgent",
    "HarnessPack",
    "HarnessRuntime",
    "engine_name",
    "is_harness_provider",
    "mcp_servers_for",
    "session_instructions",
    "template_settings",
]
