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
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
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


async def test_save_as_refuses_overwrite(harness_factory, work_model, tmp_path) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    existing = tmp_path / "exists.ifc"
    existing.write_text("do not clobber")
    out = await h.call("save_ifc_file", output_path=str(existing))
    assert out["ok"] is False
    assert out["error"]["code"] == "FILE_EXISTS"
    assert existing.read_text() == "do not clobber"
