"""Builds the public-API contract snapshot the golden test enforces.

The contract is what SemVer protects: tool names, input/output schemas,
annotations, the envelope shape, and the error-code registry. Descriptions
are deliberately excluded so wording can improve without a contract bump.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

GOLDEN_PATH = Path(__file__).parent / "golden" / "api_contract.json"


async def build_contract(home: Path) -> dict[str, Any]:
    from ifc_console.app import AppCore
    from ifc_console.mcp.envelope import ERROR_CODES, Envelope
    from ifc_console.mcp.server import build_mcp
    from ifc_console.settings import SettingsStore

    store = SettingsStore(home=home, project_dir=home, env={})
    core = AppCore(store, viewer=True)  # viewer on: snapshot the full surface
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
                    "input_schema": getattr(tool, "inputSchema", None),
                    "output_schema": getattr(tool, "outputSchema", None),
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
