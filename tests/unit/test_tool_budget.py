"""What a client pays in context for listing the tools, held to a budget.

Every character here is read by a model at the start of every session, so the
ceilings are deliberate: raising one is a decision, not a side effect.
"""

from __future__ import annotations

import json

from ifc_console.mcp import catalog
from ifc_console.mcp.server import INSTRUCTIONS, build_mcp

# 0.1.4 listed 70 tools in 94,625 characters and sent 10,146 more as instructions.
BASELINE_CHARS = 104_771
# The lean profile is the cheap way in: 17k today, a sixth of the 0.1.4 listing.
LEAN_CHARS = 20_000
# The full profile lists all 71 tools; it measures 67.8k, about 65% of 0.1.4.
FULL_CHARS = 68_000
INSTRUCTION_CHARS = 3_000
DESCRIPTION_CHARS = 700
# execute_ifc_code spells out the libraries this installation has.
DESCRIPTION_EXCEPTIONS = {"execute_ifc_code": 2_500}
PARAMETER_CHARS = 200


async def _tools(core):
    return await build_mcp(core).list_tools()


def _wire(tools) -> int:
    payload = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]
    return len(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))


async def test_the_lean_listing_fits_its_budget(core) -> None:
    lean = catalog.for_wire(await _tools(core), catalog.LEAN)

    total = _wire(lean) + len(INSTRUCTIONS)

    assert [tool.name for tool in lean] == list(catalog.LEAN_CORE)
    assert total <= LEAN_CHARS, f"lean listing is {total} characters, budget {LEAN_CHARS}"


async def test_the_full_listing_fits_its_budget(core) -> None:
    full = catalog.for_wire(await _tools(core), catalog.FULL)

    total = _wire(full) + len(INSTRUCTIONS)

    assert total <= FULL_CHARS, f"full listing is {total} characters, budget {FULL_CHARS}"


def test_the_instructions_fit_their_budget() -> None:
    assert len(INSTRUCTIONS) <= INSTRUCTION_CHARS, (
        f"instructions are {len(INSTRUCTIONS)} characters, budget {INSTRUCTION_CHARS}"
    )


async def test_every_description_stays_short(core) -> None:
    over = {
        tool.name: len(tool.description or "")
        for tool in await _tools(core)
        if len(tool.description or "") > DESCRIPTION_EXCEPTIONS.get(tool.name, DESCRIPTION_CHARS)
    }

    assert not over, f"descriptions over budget: {over}"


async def test_every_parameter_description_stays_short(core) -> None:
    over: dict[str, int] = {}
    for tool in await _tools(core):
        for name, spec in (tool.inputSchema.get("properties") or {}).items():
            size = len(str(spec.get("description") or ""))
            if size > PARAMETER_CHARS:
                over[f"{tool.name}.{name}"] = size

    assert not over, f"parameter descriptions over budget: {over}"
