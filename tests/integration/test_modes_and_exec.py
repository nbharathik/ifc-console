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


# -- edit mode in a working copy -------------------------------------------------
async def test_edits_and_saves_land_in_the_working_copy(harness_factory, work_model) -> None:
    """The whole point: the assistant can finish the job, the original survives."""
    before = _digest(work_model)
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    copy = await h.core.enter_edit_mode(by="test")
    assert copy is not None

    changed = await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
        description="add a wall",
    )

    assert changed["ok"] is True
    assert changed["meta"]["dirty"] is True
    assert changed["meta"]["changes"] == 1
    assert changed["meta"]["working_copy"]["origin_name"] == work_model.name
    # the note must not send the user off to save before they can see anything
    assert "the viewer already shows this change" in changed["data"]["note"]

    saved = await h.call("save_ifc_file")

    assert saved["ok"] is True
    assert saved["data"]["path"] == str(copy.path)
    assert saved["data"]["working_copy"] is True
    assert saved["data"]["backup_path"] is None  # the origin is the snapshot
    assert saved["meta"]["dirty"] is False
    assert saved["meta"]["changes"] == 0
    assert _digest(work_model) == before


async def test_a_working_copy_session_cannot_save_anywhere_else(
    harness_factory, work_model, tmp_path
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    await h.core.enter_edit_mode(by="test")
    await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
        description="add a wall",
    )

    out = await h.call("save_ifc_file", output_path=str(tmp_path / "elsewhere.ifc"))

    assert out["ok"] is False
    assert out["error"]["code"] == "AI_SAVE_DISABLED"
    assert not (tmp_path / "elsewhere.ifc").exists()


async def test_the_geometry_toolkit_reaches_generated_code(ask_harness) -> None:
    """Complex objects need vectors, matrices and a tessellator, not entities alone."""
    out = await ask_harness.call(
        "execute_ifc_code",
        code=(
            "import numpy as np\n"
            "from shapely.geometry import Polygon\n"
            "wall = ifc.by_type('IfcWall')[0]\n"
            "m = placement_util.get_local_placement(wall.ObjectPlacement)\n"
            "profile = Polygon([(0, 0), (3, 0), (3, 0.2), (0, 0.2)])\n"
            "[m.shape, round(profile.area, 3), float(np.linalg.norm(m[:3, 3]))]"
        ),
    )
    assert out["ok"] is True
    assert out["data"]["classification"] == "QUERY"
    assert "(4, 4)" in out["data"]["result"]


async def test_an_unlisted_library_is_not_refused(ask_harness) -> None:
    """The import policy is open: the model is not held to a curated list."""
    out = await ask_harness.call(
        "execute_ifc_code",
        code="import xml.etree.ElementTree as ET\nET.Element('ok').tag",
    )

    assert out["ok"] is True
    assert out["data"]["result"] == "'ok'"


async def test_a_blocked_capability_says_why_before_the_code_matters(
    ask_harness,
) -> None:
    """Blocked is about the capability, not about the package being unpopular."""
    out = await ask_harness.call("execute_ifc_code", code="import httpx")

    assert out["ok"] is False
    # importing the network is SYSTEM-class code, refused in ask mode outright
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"


async def test_the_tool_description_names_the_libraries_before_any_code(
    ask_harness,
) -> None:
    """The point: knowing this costs no tokens, finding out the hard way does."""
    listing = await ask_harness.session.list_tools()
    exec_tool = next(t for t in listing.tools if t.name == "execute_ifc_code")

    for name in ("numpy", "shapely", "trimesh", "`np`", "save_ifc_file"):
        assert name in exec_tool.description, name
    assert "Blocked" in exec_tool.description

    capabilities = await ask_harness.call("describe_capabilities")
    environment = capabilities["data"]["code_environment"]
    assert environment["policy"] == "open"
    assert "trimesh" in environment["installed"]["geometry and numerics"]
    assert environment["injected"]["geom"].startswith("ifcopenshell.geom")
