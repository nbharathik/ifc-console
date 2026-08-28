"""Regenerate the MCP and Python SDK public-API golden contracts.

Run after an intended API change, then review the diff like any contract
change: python scripts/snapshot_api.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.contract_util import (  # noqa: E402
    AGENT_GOLDEN_PATH,
    GOLDEN_PATH,
    build_contract,
    dump_contract,
)
from tests.sdk_contract_util import (  # noqa: E402
    AGENT_SDK_GOLDEN_PATH,
    SDK_GOLDEN_PATH,
    build_agent_sdk_contract,
    build_sdk_contract,
    dump_sdk_contract,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        contract = asyncio.run(build_contract(Path(tmp) / "home"))
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(dump_contract(contract), encoding="utf-8")
    print(f"wrote {GOLDEN_PATH} ({len(contract['tools'])} tools)")
    with tempfile.TemporaryDirectory() as tmp:
        agent_mcp_contract = asyncio.run(
            build_contract(Path(tmp) / "home", with_agents=True)
        )
    AGENT_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_GOLDEN_PATH.write_text(
        dump_contract(agent_mcp_contract), encoding="utf-8"
    )
    print(
        f"wrote {AGENT_GOLDEN_PATH} "
        f"({len(agent_mcp_contract['tools'])} tools)"
    )
    sdk_contract = build_sdk_contract()
    SDK_GOLDEN_PATH.write_text(dump_sdk_contract(sdk_contract), encoding="utf-8")
    print(
        f"wrote {SDK_GOLDEN_PATH} "
        f"({len(sdk_contract['exports'])} exports, "
        f"{len(sdk_contract['models'])} models)"
    )
    agent_contract = build_agent_sdk_contract()
    AGENT_SDK_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    AGENT_SDK_GOLDEN_PATH.write_text(
        dump_sdk_contract(agent_contract), encoding="utf-8"
    )
    print(
        f"wrote {AGENT_SDK_GOLDEN_PATH} "
        f"({len(agent_contract['exports'])} exports, "
        f"{len(agent_contract['models'])} models)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
