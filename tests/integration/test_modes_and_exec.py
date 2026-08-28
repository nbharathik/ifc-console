"""Mode gating (ask blocks, edit allows), exec REPL semantics, save round-trip."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -- execute_ifc_code: ask mode (the default) -----------------------------------
async def test_exec_query_runs_in_ask_mode(ask_harness) -> None:
    out = await ask_harness.call(
        "execute_ifc_code", code="len(ifc.by_type('IfcWall'))"
    )
    assert out["ok"] is True
    assert out["data"]["result"] == "3"
    assert out["data"]["classification"] == "QUERY"
    assert out["data"]["mutated"] is False


async def test_exec_stdout_captured(ask_harness) -> None:
    out = await ask_harness.call("execute_ifc_code", code="print('hello')\nprint(1+1)")
    assert out["data"]["stdout"] == "hello\n2\n"


async def test_exec_mutation_blocked_in_ask_mode(ask_harness, work_model) -> None:
    before = _digest(work_model)
    out = await ask_harness.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"
    assert "/mode edit" in out["error"]["hint"]  # the AI is told to ask the user
    assert _digest(work_model) == before


async def test_exec_guard_backstop_in_ask_mode(ask_harness, work_model) -> None:
    """Code the classifier misses still cannot mutate: the runtime guard blocks it."""
    before = _digest(work_model)
    out = await ask_harness.call(
        "execute_ifc_code",
        code="getattr(ifc, 'create' + '_entity')('IfcWall')",
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "EXEC_BLOCKED"
    assert _digest(work_model) == before


async def test_exec_syntax_error(ask_harness) -> None:
    out = await ask_harness.call("execute_ifc_code", code="for x in :")
    assert out["ok"] is False
    assert out["error"]["code"] == "EXEC_ERROR"


async def test_exec_runtime_error_has_traceback(ask_harness) -> None:
    out = await ask_harness.call("execute_ifc_code", code="1 / 0")
    assert out["ok"] is False
    assert out["error"]["code"] == "EXEC_ERROR"
    assert "ZeroDivisionError" in out["data"]["traceback"]


# -- edit mode ------------------------------------------------------------------
async def test_edit_mode_runs_without_prompt(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    out = await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    assert out["ok"] is True
    assert out["data"]["mutated"] is True
    assert out["meta"]["dirty"] is True
    assert out["meta"]["ai_save_allowed"] is False
    assert "only the user" in out["data"]["note"]


async def test_cancelled_mutation_still_marks_the_model_dirty(
    harness_factory, work_model, monkeypatch
) -> None:
    """Stopping a run does not stop the worker; a clean flag would lose the edit."""
    import asyncio
    import threading

    from ifc_console.session import executor

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    session = h.core.session
    running, release = threading.Event(), threading.Event()
    original_run = executor.run

    def blocking_run(*args, **kwargs):
        running.set()
        release.wait(10)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(executor, "run", blocking_run)
    call = asyncio.create_task(
        h.core.tool_functions["execute_ifc_code"](
            code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')"
        )
    )
    await asyncio.to_thread(running.wait, 10)
    call.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    for _ in range(3000):
        if not session.poisoned:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("cancelled model worker did not finish")

    assert session.dirty is True
    assert session.ifc.by_type("IfcWall")


async def test_mode_switch_applies_live(ask_harness) -> None:
    """The user flips the mode in their terminal; the next call obeys it."""
    code = "ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')"
    out = await ask_harness.call("execute_ifc_code", code=code)
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"
    ask_harness.set_mode(Mode.EDIT)
    out = await ask_harness.call("execute_ifc_code", code=code)
    assert out["ok"] is True and out["data"]["mutated"] is True
    ask_harness.set_mode(Mode.ASK)
    out = await ask_harness.call("execute_ifc_code", code=code)
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"


# -- save round-trip ------------------------------------------------------------
async def test_save_roundtrip_and_backup(harness_factory, work_model, tmp_path) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    # rename a wall, then save in place
    listing = await h.call("query_elements", query="IfcWall", limit=1)
    gid = listing["data"]["rows"][0]["global_id"]
    code = (
        f"w = ifc.by_guid({gid!r})\n"
        "ifc_api.run('attribute.edit_attributes', ifc, product=w, "
        "attributes={'Name': 'Renamed'})"
    )
    edit = await h.call("execute_ifc_code", code=code)
    assert edit["ok"] is True

    saved = await h.call("save_ifc_file")
    assert saved["ok"] is True
    assert saved["data"]["backup_path"]  # a backup was made
    assert Path(saved["data"]["backup_path"]).exists()
    assert saved["meta"]["dirty"] is False

    # reopen the saved file and confirm the change persisted
    import ifcopenshell

    reopened = ifcopenshell.open(str(work_model))
    assert reopened.by_guid(gid).Name == "Renamed"


async def test_save_blocked_in_ask_mode(ask_harness, work_model) -> None:
    before = _digest(work_model)
    out = await ask_harness.call("save_ifc_file")
    assert out["ok"] is False
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"
    assert _digest(work_model) == before


async def test_ai_save_is_blocked_by_default_but_memory_edits_remain(
    harness_factory, work_model
) -> None:
    before = _digest(work_model)
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    changed = await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    assert changed["ok"] is True
    assert changed["meta"]["dirty"] is True

    saved = await h.call("save_ifc_file")

    assert saved["ok"] is False
    assert saved["error"]["code"] == "AI_SAVE_DISABLED"
    assert "/save" in saved["error"]["hint"]
    assert _digest(work_model) == before
    assert h.core.session.dirty is True


async def test_generated_code_cannot_serialize_ifc_while_ai_save_is_off(
    harness_factory, work_model, tmp_path
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    output = tmp_path / "forbidden.ifc"

    direct = await h.call("execute_ifc_code", code=f"ifc.write(r'{output}')")
    assert direct["ok"] is False
    assert direct["error"]["code"] == "AI_SAVE_DISABLED"
    assert not output.exists()

    dynamic = await h.call(
        "execute_ifc_code",
        code=f"getattr(ifc, 'write')(r'{output}')",
    )
    assert dynamic["ok"] is False
    assert dynamic["error"]["code"] == "AI_SAVE_DISABLED"
    assert not output.exists()


async def test_system_code_stays_blocked_while_ai_save_is_off(
    harness_factory, work_model, tmp_path
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    h.core.policy.allow_system_access = True
    output = tmp_path / "forbidden.ifc"

    result = await h.call(
        "execute_ifc_code",
        code=f"open(r'{output}', 'w').write(ifc.to_string())",
    )

    assert result["ok"] is False
    assert result["error"]["code"] == "AI_SAVE_DISABLED"
    assert not output.exists()


async def test_save_refuses_an_external_source_change(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    work_model.write_bytes(work_model.read_bytes() + b"\n")
    changed = _digest(work_model)

    out = await h.call("save_ifc_file")

    assert out["ok"] is False
    assert out["error"]["code"] == "REVISION_CONFLICT"
    assert _digest(work_model) == changed


async def test_save_as_refuses_overwrite(harness_factory, work_model, tmp_path) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    existing = tmp_path / "exists.ifc"
    existing.write_text("do not clobber")
    out = await h.call("save_ifc_file", output_path=str(existing))
    assert out["ok"] is False
    assert out["error"]["code"] == "FILE_EXISTS"
    assert existing.read_text() == "do not clobber"


async def test_save_as_cannot_overwrite_another_resident_model(
    harness_factory, work_model, tmp_path
) -> None:
    import shutil

    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    await h.core.open_model(annex, attach=True)

    out = await h.call("save_ifc_file", output_path=str(annex), overwrite=True)
    assert out["ok"] is False
    assert out["error"]["code"] == "FILE_EXISTS"
    assert "resident model" in out["error"]["message"]
