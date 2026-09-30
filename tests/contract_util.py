"""Builds the public-API contract snapshot the golden test enforces.

The contract is what SemVer protects: tool names, input/output schemas,
annotations, the envelope shape, and the error-code registry. Descriptions,
on tools and on their parameters, are deliberately excluded so wording can
improve without a contract bump.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_PATH = Path(__file__).parent / "golden" / "api_contract.json"
AGENT_GOLDEN_PATH = Path(__file__).parent / "agents" / "golden" / "api_contract.json"


def without_descriptions(node: Any) -> Any:
    """A schema with its prose removed, so rewording never churns the golden.

    Only a schema node's own `description` goes; a property that happens to be
    called `description` is a property and stays.
    """
    if isinstance(node, list):
        return [without_descriptions(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "description" and isinstance(value, str):
            continue
        if key in ("properties", "$defs", "definitions", "patternProperties") and isinstance(
            value, dict
        ):
            out[key] = {name: without_descriptions(sub) for name, sub in value.items()}
        else:
            out[key] = without_descriptions(value)
    return out


async def build_contract(home: Path, *, with_agents: bool = False) -> dict[str, Any]:
    from ifc_console.app import AppCore
    from ifc_console.extensions import ExtensionManager
    from ifc_console.mcp.envelope import ERROR_CODES, Envelope
    from ifc_console.mcp.server import build_mcp
    from ifc_console.settings import SettingsStore

    entries: tuple[Any, ...] = ()
    if with_agents:
        from ifc_console.agents.extension import AgentExtension

        class _AgentsEntryPoint:
            name = "agents"
            value = "ifc_console.agents.extension:AgentExtension"
            dist = None

            @staticmethod
            def load():
                return AgentExtension

        entries = (_AgentsEntryPoint(),)
    store = SettingsStore(home=home, project_dir=home, env={})
    core = AppCore(
        store,
        viewer=True,
        extension_manager=ExtensionManager(entries),
    )
    try:
        mcp = build_mcp(core)
        tools = []
        for tool in sorted(await mcp.list_tools(), key=lambda t: t.name):
            annotations = getattr(tool, "annotations", None)
            tools.append(
                {
                    "name": tool.name,
                    "read_only": getattr(annotations, "readOnlyHint", None),
                    "destructive": getattr(annotations, "destructiveHint", None),
                    "input_schema": without_descriptions(getattr(tool, "inputSchema", None)),
                    "output_schema": without_descriptions(getattr(tool, "outputSchema", None)),
                }
            )
        return {
            "envelope": Envelope.model_json_schema(),
            "error_codes": sorted(ERROR_CODES),
            "tools": tools,
        }
    finally:
        core.shutdown()


def dump_contract(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
