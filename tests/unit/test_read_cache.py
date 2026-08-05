"""Fingerprint-keyed read cache and the model-open memory budget."""

from __future__ import annotations

import asyncio
import shutil
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


async def test_cached_read_is_scoped_to_the_session(core, work_model: Path, tmp_path: Path):
    await core.open_model(work_model)
    active = core.session
    annex_path = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex_path)
    annex_id = await core.open_model(annex_path, attach=True)
    annex = core.models.require(annex_id)
    annex.fingerprint = active.fingerprint
    annex.revision = active.revision

    first, _ = await core.cached_read(
        "identity", lambda: str(active.path), session=active
    )
    second, hit = await core.cached_read(
        "identity", lambda: str(annex.path), session=annex
    )
    assert first == str(active.path)
    assert second == str(annex.path)
    assert hit is False


async def test_concurrent_attach_deduplicates_the_same_path(
    core, work_model: Path, tmp_path: Path
):
    await core.open_model(work_model)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)

    model_ids = await asyncio.gather(
        core.open_model(annex, attach=True),
        core.open_model(annex, attach=True),
    )
    assert model_ids[0] == model_ids[1]
    assert len(core.models.sessions) == 2


async def test_concurrent_replacements_leave_one_consistent_model(
    core, work_model: Path, tmp_path: Path
):
    await core.open_model(work_model)
    second = tmp_path / "second.ifc"
    third = tmp_path / "third.ifc"
    shutil.copy2(work_model, second)
    shutil.copy2(work_model, third)

    model_ids = await asyncio.gather(core.open_model(second), core.open_model(third))
    assert len(set(model_ids)) == 2
    assert len(core.models.sessions) == 1
    assert core.models.active_id in model_ids


async def test_active_switch_waits_for_a_pinned_operation(
    core, work_model: Path, tmp_path: Path
):
    await core.open_model(work_model)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    annex_id = await core.open_model(annex, attach=True)

    async with core.active_session():
        switch = asyncio.create_task(core.set_active_model(annex_id))
        await asyncio.sleep(0)
        assert switch.done() is False
    await switch
    assert core.models.active_id == annex_id


async def test_opening_a_resident_model_replaces_the_previous_active(
    core, work_model: Path, tmp_path: Path
):
    await core.open_model(work_model)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    annex_id = await core.open_model(annex, attach=True)

    assert await core.open_model(annex) == annex_id
    assert set(core.models.sessions) == {annex_id}
    assert core.models.active_id == annex_id


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
