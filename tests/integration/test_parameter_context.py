"""Real MCP calls preserve selected-object evidence and reject stale follow-up edits."""

from __future__ import annotations

import asyncio
import shutil
import threading

import ifcopenshell
import ifcopenshell.util.element as element_util
import pytest

from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio


async def _target(h):
    listing = await h.call("query_elements", query="IfcWall", limit=1)
    gid = listing["data"]["rows"][0]["global_id"]
    detail = await h.call("get_element", global_ids=[gid])
    return gid, detail["data"]["target_context"]


def _rename(gid, name="Reviewed wall"):
    return f"ifc.by_guid({gid!r}).Name = {name!r}"


async def test_guarded_extraction_is_read_only_and_carries_context(ask_harness):
    gid, context = await _target(ask_harness)
    result = await ask_harness.call(
        "execute_ifc_code", code=f"ifc.by_guid({gid!r}).Name", expected_context=context
    )
    assert result["ok"]
    assert result["data"]["mutated"] is False
    assert result["data"]["target_context"] == context
    assert result["meta"]["dirty"] is False


async def test_guarded_edit_returns_before_after_context_and_readback(harness_factory, work_model):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    result = await h.call("execute_ifc_code", code=_rename(gid), expected_context=context)
    assert result["ok"]
    assert result["data"]["input_context"] == context
    assert result["data"]["target_context"]["revision"] > context["revision"]
    readback = await h.call("get_element", global_ids=[gid])
    assert readback["data"]["target_context"] == result["data"]["target_context"]
    assert h.core.session.ifc.by_guid(gid).Name == "Reviewed wall"


async def test_stale_edit_preserves_existing_unsaved_state(harness_factory, work_model):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    assert (await h.call("execute_ifc_code", code=_rename(gid)))["ok"]
    session = h.core.session
    state = session.revision, session.change_count, session.dirty, session.ifc.to_string()
    result = await h.call(
        "execute_ifc_code", code=_rename(gid, "Must not run"), expected_context=context
    )
    assert result["error"]["code"] == "REVISION_CONFLICT"
    assert result["data"]["executed"] is False
    assert result["data"]["mismatches"] == ["revision"]
    assert (session.revision, session.change_count, session.dirty, session.ifc.to_string()) == state


async def test_file_switch_with_same_guids_rejects_original_context(
    harness_factory, work_model, tmp_path
):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    other = tmp_path / "other.ifc"
    shutil.copy2(work_model, other)
    await h.core.open_model(other)
    result = await h.call("execute_ifc_code", code=_rename(gid), expected_context=context)
    assert result["error"]["code"] == "REVISION_CONFLICT"
    assert "model_id" in result["data"]["mismatches"]
    assert not h.core.session.dirty


async def test_changed_fingerprint_rejected_even_with_same_revision(harness_factory, work_model):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    context["fingerprint"] = "previous-file-bytes"
    result = await h.call("execute_ifc_code", code=_rename(gid), expected_context=context)
    assert result["error"]["code"] == "REVISION_CONFLICT"
    assert result["data"]["mismatches"] == ["fingerprint"]
    assert not h.core.session.dirty


async def test_missing_target_prevents_any_code_execution(harness_factory, work_model):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    context["global_ids"] = ["missing-global-id"]
    result = await h.call("execute_ifc_code", code=_rename(gid), expected_context=context)
    assert result["error"]["code"] == "NOT_FOUND"
    assert result["data"]["missing"] == ["missing-global-id"]
    assert not h.core.session.dirty


async def test_failed_guarded_script_reports_possible_partial_edits(harness_factory, work_model):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    result = await h.call(
        "execute_ifc_code", code=_rename(gid) + "\n1 / 0", expected_context=context
    )
    assert result["error"]["code"] == "EXEC_ERROR"
    assert result["data"]["partial_changes_possible"] is True
    assert result["data"]["input_context"] == context
    assert result["data"]["target_context"]["revision"] > context["revision"]
    assert result["meta"]["dirty"] is True
    assert h.core.session.ifc.by_guid(gid).Name == "Reviewed wall"


class _Viewer:
    async def send_text(self, text):
        pass


async def test_selection_change_rejected_without_retargeting(harness_factory, work_model):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, _ = await _target(h)
    hub = h.core.viewer_hub
    client = hub.register(_Viewer())
    await hub.handle_frame(client, {"type": "selection", "guids": [gid]})
    selection = await h.call("get_viewer_selection")
    context = selection["data"]["target_context"]
    assert context["global_ids"] == [gid]
    assert context["selection"]["client_id"] == client.id
    first = await h.call("execute_ifc_code", code=_rename(gid), expected_context=context)
    assert first["ok"]
    context = first["data"]["target_context"]
    assert context["selection"] == selection["data"]["target_context"]["selection"]
    revision = h.core.session.revision
    await hub.handle_frame(client, {"type": "selection", "guids": []})
    result = await h.call(
        "execute_ifc_code", code=_rename(gid, "Must not retarget"), expected_context=context
    )
    assert result["error"]["code"] == "REVISION_CONFLICT"
    assert result["data"]["mismatches"] == ["selection"]
    assert h.core.session.revision == revision
    assert h.core.session.ifc.by_guid(gid).Name == "Reviewed wall"


async def test_guard_is_checked_after_earlier_queued_model_work(
    harness_factory, work_model, monkeypatch
):
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    gid, context = await _target(h)
    session = h.core.session
    started, release = threading.Event(), threading.Event()
    queued = asyncio.Event()
    original_run = session.run

    def prior_edit():
        started.set()
        release.wait(10)
        session.mark_dirty()

    prior = asyncio.create_task(original_run(prior_edit))
    await asyncio.to_thread(started.wait, 10)

    async def observed_run(fn, **kwargs):
        queued.set()
        return await original_run(fn, **kwargs)

    monkeypatch.setattr(session, "run", observed_run)
    pending = asyncio.create_task(
        h.core.operation_service.call(
            "execute_ifc_code", {"code": _rename(gid), "expected_context": context}
        )
    )
    try:
        await asyncio.wait_for(queued.wait(), timeout=10)
    finally:
        release.set()
    await prior
    result = await pending
    assert not result.ok
    assert result.error.code == "REVISION_CONFLICT"
    assert session.revision == context["revision"] + 1
    assert session.ifc.by_guid(gid).Name != "Reviewed wall"


async def test_guarded_parameter_upsert_and_quantity_move_preserve_working_copy(
    harness_factory, work_model
):
    """Synthetic reviewed mapping rehearses retries/readback, not PDF interpretation."""
    original_bytes = work_model.read_bytes()
    h = await harness_factory(model=work_model)
    copy = await h.core.enter_edit_mode(by="test")
    assert copy is not None
    gid, _ = await _target(h)
    write = (
        f"element = ifc.by_guid({gid!r})\n"
        "sets = element_util.get_psets(element, psets_only=True, should_inherit=False)\n"
        "existing = sets.get('IFCConsole_TestEvidence')\n"
        "pset = ifc.by_id(existing['id']) if existing else "
        "ifc_api.run('pset.add_pset', ifc, product=element, name='IFCConsole_TestEvidence')\n"
        "ifc_api.run('pset.edit_pset', ifc, pset=pset, properties="
        "{'ReviewedLength': 4.25, 'SourceReference': 'Synthetic test: PDF p.2; model units'})"
    )
    migrate = (
        f"element = ifc.by_guid({gid!r})\n"
        "sets = element_util.get_psets(element, should_inherit=False)\n"
        "source = sets['IFCConsole_TestEvidence']\n"
        "existing = sets.get('Qto_WallBaseQuantities')\n"
        "qto = ifc.by_id(existing['id']) if existing else "
        "ifc_api.run('pset.add_qto', ifc, product=element, name='Qto_WallBaseQuantities')\n"
        "if 'ReviewedLength' in source:\n"
        "    ifc_api.run('pset.edit_qto', ifc, qto=qto, "
        "properties={'Length': source['ReviewedLength']})\n"
        "    verified = element_util.get_psets(element, qtos_only=True, should_inherit=False)\n"
        "    assert verified['Qto_WallBaseQuantities']['Length'] == source['ReviewedLength']\n"
        "    ifc_api.run('pset.edit_pset', ifc, pset=ifc.by_id(source['id']), "
        "properties={'ReviewedLength': None})"
    )
    for code in (write, write, migrate, migrate):
        _, context = await _target(h)
        result = await h.call("execute_ifc_code", code=code, expected_context=context)
        assert result["ok"], result
    readback = await h.call("get_psets", global_ids=[gid])
    result_sets = readback["data"]["results"][0]
    assert result_sets["qtos"]["Qto_WallBaseQuantities"]["Length"] == 4.25
    assert "ReviewedLength" not in result_sets["psets"]["IFCConsole_TestEvidence"]
    saved = await h.call("save_ifc_file")
    assert saved["ok"] and saved["data"]["working_copy"]
    assert saved["data"]["path"] == str(copy.path)
    disk = ifcopenshell.open(str(copy.path))
    element = disk.by_guid(gid)
    disk_sets = element_util.get_psets(element, should_inherit=False)
    assert disk_sets["Qto_WallBaseQuantities"]["Length"] == 4.25
    assert disk_sets["IFCConsole_TestEvidence"]["SourceReference"].startswith("Synthetic test:")
    own_sets = [
        rel.RelatingPropertyDefinition.Name
        for rel in element.IsDefinedBy
        if rel.is_a("IfcRelDefinesByProperties")
    ]
    assert own_sets.count("IFCConsole_TestEvidence") == 1
    assert own_sets.count("Qto_WallBaseQuantities") == 1
    assert work_model.read_bytes() == original_bytes
