"""A model keeps one identity until its in-memory content changes."""

from __future__ import annotations

import pytest

from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio

def _rename(gid: str, name: str = "Renamed") -> str:
    return f"ifc.by_guid({gid!r}).Name = {name!r}"


def _identity(h) -> tuple:
    s = h.core.session
    return (s.fingerprint, s.load_nonce, s.revision, h.core.viewer_hub.model_etag())


async def _first_wall(h) -> str:
    listing = await h.call("query_elements", query="IfcWall", limit=1)
    return listing["data"]["rows"][0]["global_id"]


async def test_saving_keeps_the_identity_and_the_reviewed_context(
    harness_factory, work_model
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    gid = await _first_wall(h)
    assert (await h.call("execute_ifc_code", code=_rename(gid)))["ok"]
    context = (await h.call("get_element", global_ids=[gid]))["data"]["target_context"]
    before = _identity(h)

    saved = await h.call("save_ifc_file")

    assert saved["ok"] is True
    assert _identity(h) == before
    assert saved["data"]["content_sha256"] == h.core.session.source_sha256
    again = await h.call(
        "execute_ifc_code",
        code=f"ifc.by_guid({gid!r}).Name",
        expected_context=context,
    )
    assert again["ok"] is True


async def test_an_edit_after_a_save_moves_the_revision(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    gid = await _first_wall(h)
    await h.call("execute_ifc_code", code=_rename(gid))
    await h.call("save_ifc_file")
    before = _identity(h)

    await h.call("execute_ifc_code", code=_rename(gid, "Twice"))

    after = _identity(h)
    assert after[:2] == before[:2]
    assert after[2] == before[2] + 1
    assert after[3] != before[3]


async def test_entering_edit_mode_keeps_the_identity(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    before = _identity(h)

    copy = await h.core.enter_edit_mode(by="test")

    assert copy is not None
    assert h.core.session.path == copy.path
    assert _identity(h) == before


async def test_a_reload_starts_a_new_identity(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    before = _identity(h)

    await h.core.session.reload()

    after = _identity(h)
    assert after[0] == before[0]
    assert after[1] != before[1]
    assert after[3] != before[3]


async def test_project_info_follows_the_file_after_a_save(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    first = await h.call("get_ifc_project_info")
    gid = await _first_wall(h)
    await h.call("execute_ifc_code", code=_rename(gid))
    await h.call("save_ifc_file")

    second = await h.call("get_ifc_project_info")

    assert second["data"]["file"]["size_bytes"] == h.core.session.path.stat().st_size
    assert second["data"]["file"]["mtime"] >= first["data"]["file"]["mtime"]
