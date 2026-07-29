"""The executable public-API contract.

Fails when tool names, schemas, annotations, the envelope shape, or the
error-code registry change. If the change is intended, regenerate the golden
file and treat it as a SemVer decision:

    UPDATE_GOLDEN=1 python -m pytest tests/unit/test_api_contract.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.contract_util import GOLDEN_PATH, build_contract, dump_contract

pytestmark = pytest.mark.asyncio


async def test_public_api_matches_golden(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    contract = await build_contract(tmp_path / "home")
    rendered = dump_contract(contract)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip("golden contract updated")
    assert GOLDEN_PATH.is_file(), (
        "no golden contract; generate it with UPDATE_GOLDEN=1 pytest "
        "tests/unit/test_api_contract.py"
    )
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    current = json.loads(rendered)
    assert current["error_codes"] == golden["error_codes"], (
        "error-code registry changed; renames/removals are breaking. If intended, "
        "set UPDATE_GOLDEN=1 to regenerate and record the SemVer decision."
    )
    assert current["envelope"] == golden["envelope"], (
        "the envelope schema changed; this is the public contract. If intended, "
        "set UPDATE_GOLDEN=1 to regenerate and record the SemVer decision."
    )
    golden_tools = {tool["name"]: tool for tool in golden["tools"]}
    current_tools = {tool["name"]: tool for tool in current["tools"]}
    removed = sorted(set(golden_tools) - set(current_tools))
    assert not removed, f"tools removed (breaking): {removed}"
    for name, tool in current_tools.items():
        if name in golden_tools:
            assert tool == golden_tools[name], (
                f"tool {name} changed its schema or annotations. If intended, set "
                "UPDATE_GOLDEN=1 to regenerate and record the SemVer decision."
            )
    # brand-new tools are additive and fine; they land in the golden on update
