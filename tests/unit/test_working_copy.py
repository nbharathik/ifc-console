"""Edit mode works in a copy; the file the user opened is never written."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.policy.modes import Mode


@pytest.mark.asyncio
async def test_entering_edit_mode_copies_the_open_file(core, work_model: Path):
    await core.open_model(work_model)
    original = work_model.read_bytes()

    copy = await core.enter_edit_mode(by="test")

    assert copy is not None
    assert core.policy.mode is Mode.EDIT
    assert copy.origin == work_model
    assert copy.path.exists() and copy.path != work_model
    assert copy.path.read_bytes() == original
    # the session now edits the copy, and saving is no longer a decision
    assert core.session.path == copy.path
    assert core.session.origin_path == work_model
    assert core.policy.allow_copy_save is True
    assert core.session_meta()["ai_save_allowed"] is True


@pytest.mark.asyncio
async def test_saving_writes_the_copy_and_leaves_the_original(core, work_model: Path):
    await core.open_model(work_model)
    before = work_model.read_bytes()
    copy = await core.enter_edit_mode(by="test")
    assert copy is not None

    session = core.session
    await session.run(lambda: session.ifc.create_entity("IfcSite", GlobalId="0" * 22))
    session.mark_dirty()
    session.record_change("added a site", tool="test")
    assert session.change_count == 1

    result = await core.save_model(by="test")

    assert result["working_copy"] is True
    assert Path(result["path"]) == copy.path
    assert work_model.read_bytes() == before
    assert copy.path.read_bytes() != before
    # a save clears what was waiting for it, and needs no backup of the copy
    assert session.change_count == 0
    assert session.dirty is False
    assert result["backup_path"] is None


@pytest.mark.asyncio
async def test_reload_keeps_the_working_copy(core, work_model: Path):
    await core.open_model(work_model)
    copy = await core.enter_edit_mode(by="test")
    assert copy is not None

    await core.session.reload()

    assert core.session.working_copy is not None
    assert core.session.path == copy.path
    assert core.policy.mode is Mode.EDIT


@pytest.mark.asyncio
async def test_opening_another_file_drops_the_copy(core, work_model: Path, tmp_path: Path):
    await core.open_model(work_model)
    await core.enter_edit_mode(by="test")

    other = tmp_path / "other.ifc"
    other.write_bytes(work_model.read_bytes())
    await core.open_model(other, discard_dirty=True)

    assert core.session.working_copy is None
    assert core.session.path == other
    assert core.policy.allow_copy_save is False


@pytest.mark.asyncio
async def test_working_copy_can_be_turned_off(core, work_model: Path):
    core.store.settings.files.working_copy = False
    await core.open_model(work_model)

    assert await core.enter_edit_mode(by="test") is None
    assert core.session.working_copy is None
    assert core.session.path == work_model
    assert core.policy.mode is Mode.EDIT


@pytest.mark.asyncio
async def test_changes_are_counted_and_summarized(core, work_model: Path):
    await core.open_model(work_model)
    session = core.session
    for n in range(3):
        session.record_change(f"change {n}", tool="execute_ifc_code")

    summary = session.changes_summary(limit=2)

    assert summary["count"] == 3
    assert summary["recent"] == ["change 1", "change 2"]
    assert core.session_meta()["changes"] == 3


def test_the_store_prunes_old_copies(tmp_path: Path):
    from ifc_console.session.working_copy import WorkingCopyStore

    origin = tmp_path / "model.ifc"
    origin.write_text("ISO-10303-21;\n", encoding="utf-8")
    store = WorkingCopyStore(tmp_path / "working", retention=2)

    copies = [store.create(origin) for _ in range(4)]

    assert copies[-1].path.exists()
    assert len(store.entries()) <= 2
