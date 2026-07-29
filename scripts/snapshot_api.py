"""Regenerate the public-API golden contract (tests/golden/api_contract.json).

Run after an intended API change, then review the diff like any contract
change: python scripts/snapshot_api.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.contract_util import GOLDEN_PATH, build_contract, dump_contract  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        contract = asyncio.run(build_contract(Path(tmp) / "home"))
    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(dump_contract(contract), encoding="utf-8")
    print(f"wrote {GOLDEN_PATH} ({len(contract['tools'])} tools)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
