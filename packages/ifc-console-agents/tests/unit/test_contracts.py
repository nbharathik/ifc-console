"""Independent public contracts for the agents SDK and MCP contribution."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from tests.contract_util import AGENT_GOLDEN_PATH, build_contract, dump_contract
from tests.sdk_contract_util import (
    AGENT_SDK_GOLDEN_PATH,
    build_agent_sdk_contract,
    dump_sdk_contract,
)


@pytest.mark.asyncio
async def test_combined_mcp_contract_matches_golden(tmp_path: Path):
    current = await build_contract(tmp_path / "home", with_agents=True)
    rendered = dump_contract(current)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        AGENT_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGENT_GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip("agents MCP golden contract updated")
    assert json.loads(rendered) == json.loads(
        AGENT_GOLDEN_PATH.read_text(encoding="utf-8")
    )


def test_agent_sdk_contract_matches_golden():
    rendered = dump_sdk_contract(build_agent_sdk_contract())
    if os.environ.get("UPDATE_GOLDEN") == "1":
        AGENT_SDK_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        AGENT_SDK_GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip("agents SDK golden contract updated")
    assert json.loads(rendered) == json.loads(
        AGENT_SDK_GOLDEN_PATH.read_text(encoding="utf-8")
    )
