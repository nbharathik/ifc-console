"""Workspace tools end to end: find, attach, list, switch, per-model reads."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio

IDS_FIXTURE = Path(__file__).parents[1] / "fixtures" / "wall_firerating.ids"


@pytest.fixture
def project(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    """A folder holding two models and an IDS, like a real handover."""
    root = tmp_path / "project"
    root.mkdir()
    shutil.copy2(minimal_ifc4_path, root / "tower-architecture.ifc")
    shutil.copy2(minimal_ifc4_path, root / "tower-structure.ifc")
    shutil.copy2(IDS_FIXTURE, root / "employer-requirements.ids")
    return root


async def test_find_files_ranks_bim_data_first_and_opens_nothing(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    out = await h.call("find_files")
    assert out["ok"] is True
    files = out["data"]["files"]
    kinds = {row["kind"] for row in files}
    assert {"ifc", "ids"} <= kinds
    # documents never bury the models: everything BIM comes before them
    opens = [row["opens_as"] for row in files]
    if "document" in opens:
        assert opens.index("document") > opens.index("model")
        first_document = opens.index("document")
        assert all(kind == "document" for kind in opens[first_document:])
    assert len(h.core.models.sessions) == 1  # nothing was loaded by searching

    ids_only = await h.call("find_files", kinds=["ids"])
    row = ids_only["data"]["files"][0]
    assert row["consumed_by"] == "validate_ids"
    assert row["specifications"] == 1


async def test_model_ids_are_short_enough_to_retype(harness_factory, tmp_path, minimal_ifc4_path):
    """An LLM has to type these back; ISO 19650 file names are not ids."""
    root = tmp_path / "iso"
    root.mkdir()
    for role in ("A", "S"):
        shutil.copy2(minimal_ifc4_path, root / f"ABC-XYZ-ZZ-XX-M3-{role}-0001.ifc")
    h = await harness_factory(model=root / "ABC-XYZ-ZZ-XX-M3-A-0001.ifc")
    out = await h.call("attach", path=str(root / "ABC-XYZ-ZZ-XX-M3-S-0001.ifc"))
    assert out["data"]["model_id"] == "str"
    assert out["meta"]["model_id"] == "arc"

    rows = await h.call("query_elements", query="IfcWall", model="str")
    assert rows["ok"] is True


async def test_find_files_flags_ambiguous_candidates(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    out = await h.call("find_files", query="tower", kinds=["ifc"])
    assert out["meta"]["ambiguous"] is True
    assert "ask the user" in out["data"]["hint"]


async def test_unknown_kind_is_rejected(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    out = await h.call("find_files", kinds=["dwg"])
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"


async def test_attach_ids_hands_the_path_to_the_llm(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    out = await h.call("attach", path=str(project / "employer-requirements.ids"))
    assert out["ok"] is True
    assert out["data"]["attached"] == "file"
    assert out["data"]["kind"] == "ids"
    assert out["data"]["next"] == "call validate_ids with this path"
    assert out["meta"]["attachments"] == 1

    listed = await h.call("list_models")
    assert listed["data"]["attachments"][0]["alias"] == "employer-requirements"

    # the alias is enough; the LLM never has to repeat the path
    report = await h.call("validate_ids", ids_path="employer-requirements")
    assert report["ok"] is True
    assert report["data"]["totals"]["specifications"] == 1


async def test_attach_second_model_is_read_only(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc", mode=Mode.EDIT)
    out = await h.call("attach", path=str(project / "tower-structure.ifc"))
    assert out["data"]["attached"] == "model"
    assert out["data"]["active"] is False
    assert out["data"]["writable"] is False
    assert out["meta"]["models"] == 2
    assert out["meta"]["model_id"] == "tower-architecture"  # the active one is unchanged

    listed = await h.call("list_models")
    rows = {row["model_id"]: row for row in listed["data"]["models"]}
    assert rows["tower-architecture"]["active"] is True
    assert rows["tower-structure"]["writable"] is False

    # a write aimed at the attached model cannot even be expressed: saving
    # always targets the active model, and the read-only flag backs it up
    attached = h.core.models.require("tower-structure")
    from ifc_console.mcp.envelope import ToolError

    with pytest.raises(ToolError) as excinfo:
        attached.require_writable()
    assert excinfo.value.code == "MODEL_READ_ONLY"


async def test_reads_can_target_an_attached_model(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    await h.call("attach", path=str(project / "tower-structure.ifc"))

    out = await h.call("query_elements", query="IfcWall", model="tower-structure")
    assert out["ok"] is True
    assert out["meta"]["total"] == 3
    # meta.model keeps naming the active model; read_from says where the rows came from
    assert out["meta"]["model"] == "tower-architecture.ifc"
    assert out["meta"]["read_from"] == "tower-structure"

    info = await h.call("get_ifc_project_info", model="tower-structure")
    assert info["data"]["schema"] == "IFC4"

    missing = await h.call("query_elements", query="IfcWall", model="nope")
    assert missing["ok"] is False
    assert missing["error"]["code"] == "MODEL_NOT_FOUND"


async def test_set_active_model_moves_the_write_focus(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    await h.call("attach", path=str(project / "tower-structure.ifc"))

    out = await h.call("set_active_model", model_id="tower-structure")
    assert out["ok"] is True
    assert out["data"]["previous"] == "tower-architecture"
    assert out["meta"]["model_id"] == "tower-structure"
    assert h.core.models.require("tower-architecture").read_only is True
    assert h.core.session.read_only is False


async def test_detach_frees_a_model_and_a_file(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    await h.call("attach", path=str(project / "tower-structure.ifc"))
    await h.call("attach", path=str(project / "employer-requirements.ids"))

    assert (await h.call("detach", id="tower-structure"))["ok"] is True
    assert (await h.call("detach", id="employer-requirements"))["ok"] is True
    assert len(h.core.models.sessions) == 1
    assert h.core.models.attachments == {}

    missing = await h.call("detach", id="ghost")
    assert missing["error"]["code"] == "NOT_FOUND"


async def test_attaching_stays_inside_the_allowed_roots(harness_factory, project: Path, tmp_path):
    outsider = tmp_path.parent / "outside.ids"
    outsider.write_bytes(IDS_FIXTURE.read_bytes())
    h = await harness_factory(model=project / "tower-architecture.ifc")
    out = await h.call("attach", path=str(outsider))
    assert out["ok"] is False
    assert out["error"]["code"] == "PATH_NOT_ALLOWED"


async def test_single_model_session_keeps_its_old_shape(harness_factory, work_model: Path):
    """The default path must look exactly as it did before the workspace."""
    h = await harness_factory(model=work_model)
    out = await h.call("get_session_status")
    assert out["meta"]["model"] == "work.ifc"
    assert "models" not in out["meta"]  # quiet until a second file is in play
    assert "attachments" not in out["meta"]
    assert out["meta"]["model_id"] == "work"

    orient = await h.call("orient")
    assert "workspace" not in orient["data"]["status"]
    assert "read_from" not in orient["meta"]

    rows = await h.call("query_elements", query="IfcWall")
    assert "read_from" not in rows["meta"]  # quiet when the read is the active model


async def test_orient_surfaces_attachments(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    await h.call("attach", path=str(project / "employer-requirements.ids"))
    orient = await h.call("orient")
    workspace = orient["data"]["status"]["workspace"]
    assert workspace["active"] == "tower-architecture"
    assert workspace["attachments"][0]["consumed_by"] == "validate_ids"


async def test_open_ifc_file_still_replaces_the_active_model(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    out = await h.call("open_ifc_file", path=str(project / "tower-structure.ifc"))
    assert out["ok"] is True
    assert len(h.core.models.sessions) == 1
    assert h.core.session.name == "tower-structure.ifc"


async def test_budget_of_one_restores_strict_single_model(harness_factory, project: Path):
    h = await harness_factory(model=project / "tower-architecture.ifc")
    h.core.models.max_resident = 1
    out = await h.call("attach", path=str(project / "tower-structure.ifc"))
    assert out["ok"] is False
    assert out["error"]["code"] == "WORKSPACE_BUDGET"
    assert "workspace.max_resident" in out["error"]["hint"]
