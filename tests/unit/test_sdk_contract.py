"""Executable contract for the documented Python SDK and plugin API."""

from __future__ import annotations

import json
import os

import pytest

from tests.sdk_contract_util import SDK_GOLDEN_PATH, build_sdk_contract, dump_sdk_contract


def test_sdk_contract_matches_golden():
    contract = build_sdk_contract()
    rendered = dump_sdk_contract(contract)
    if os.environ.get("UPDATE_GOLDEN") == "1":
        SDK_GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        SDK_GOLDEN_PATH.write_text(rendered, encoding="utf-8")
        pytest.skip("SDK golden contract updated")
    assert SDK_GOLDEN_PATH.is_file(), (
        "no SDK golden contract; generate it with python scripts/snapshot_api.py"
    )
    golden = json.loads(SDK_GOLDEN_PATH.read_text(encoding="utf-8"))
    current = json.loads(rendered)
    assert current == golden, (
        "the documented SDK or plugin API contract changed. If intended, run "
        "python scripts/snapshot_api.py and review the SemVer impact."
    )
