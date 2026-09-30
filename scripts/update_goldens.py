"""Regenerate the golden contract files after an intended public-API change.

Run from the repository root:

    uv run --no-sync python scripts/update_goldens.py

Every difference in the resulting diff is a SemVer decision: new tools and
error codes are additive, renames and removals are breaking.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path.relative_to(ROOT)}")


async def main() -> None:
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as scratch:
        root = Path(scratch)
        os.environ["IFC_CONSOLE_HOME"] = str(root / "home")
        _write(GOLDEN_PATH, dump_contract(await build_contract(root / "home")))
        _write(
            AGENT_GOLDEN_PATH,
            dump_contract(await build_contract(root / "agents-home", with_agents=True)),
        )
        _write(SDK_GOLDEN_PATH, dump_sdk_contract(build_sdk_contract()))
        _write(AGENT_SDK_GOLDEN_PATH, dump_sdk_contract(build_agent_sdk_contract()))


if __name__ == "__main__":
    asyncio.run(main())
