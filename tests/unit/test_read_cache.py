"""Fingerprint-keyed read cache and the model-open memory budget."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.mcp.envelope import ToolError
from ifc_console.session.model import ModelSession

pytestmark = pytest.mark.asyncio


async def test_cached_read_hits_until_revision_bump(core, work_model: Path):
    await core.open_model(work_model)
    calls = 0

    def build():
        nonlocal calls
        calls += 1
        return {"n": calls}

    first, hit1 = await core.cached_read("probe", build)
    again, hit2 = await core.cached_read("probe", build)
    assert (first, hit1) == ({"n": 1}, False)
    assert again == {"n": 1}
    assert hit2 is True
    assert calls == 1

    core.session.mark_dirty()  # any mutation bumps the revision
    fresh, hit3 = await core.cached_read("probe", build)
    assert fresh == {"n": 2}
    assert hit3 is False


async def test_cached_read_key_distinguishes_arguments(core, work_model: Path):
    await core.open_model(work_model)
    a, _ = await core.cached_read("probe", lambda: "a", key=("a",))
    b, _ = await core.cached_read("probe", lambda: "b", key=("b",))
    assert (a, b) == ("a", "b")


async def test_open_budget_refuses_oversized_file(tmp_path: Path):
    big = tmp_path / "big.ifc"
    big.write_bytes(b"0" * (2 * 1_048_576))
    session = ModelSession()
    try:
        with pytest.raises(ToolError) as excinfo:
            await session.open(big, max_mb=1)
        assert excinfo.value.code == "MODEL_TOO_LARGE"
        assert not session.loaded
    finally:
        session.close()


async def test_open_budget_zero_disables_guard(work_model: Path):
    session = ModelSession()
    try:
        await session.open(work_model, max_mb=0)
        assert session.loaded
    finally:
        session.close()


async def test_budget_survives_reload(tmp_path: Path, work_model: Path):
    """The remembered budget still guards a file grown between open and reload."""
    session = ModelSession()
    try:
        await session.open(work_model, max_mb=1)
        work_model.write_bytes(b"0" * (2 * 1_048_576))
        with pytest.raises(ToolError) as excinfo:
            await session.reload()
        assert excinfo.value.code == "MODEL_TOO_LARGE"
    finally:
        session.close()
