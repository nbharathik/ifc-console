"""The document Q&A preset.

The agent is data: see :mod:`ifc_console_agents.presets`. This module keeps the
names existing code imports.
"""

from __future__ import annotations

from typing import Any

from ifc_console_agents.agent import Agent
from ifc_console_agents.blocks import BLOCK_BY_NAME
from ifc_console_agents.presets import DOCS, PresetPack

BLOCKS = DOCS.blocks
ROLE = DOCS.role
INSTRUCTIONS = ROLE
READ_TOOLS = tuple(
    dict.fromkeys(tool for name in BLOCKS for tool in BLOCK_BY_NAME[name].tools)
)

PACK = PresetPack(DOCS)


async def build_agent(
    runtime: Any,
    *,
    model: Any,
    viewer: bool = False,
    instructions: str = "",
    model_label: str = "",
) -> Agent:
    """The document Q&A agent over a LocalRuntime or ConsoleRuntime."""
    return await PACK.build(
        runtime,
        model=model,
        viewer=viewer,
        instructions=instructions,
        model_label=model_label,
    )


__all__ = ["BLOCKS", "INSTRUCTIONS", "PACK", "READ_TOOLS", "ROLE", "build_agent"]
