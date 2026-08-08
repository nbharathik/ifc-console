"""The public SDK: scriptable BIM without a server or a terminal."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ifc_console.sdk import IfcConsoleError, Workbench


@pytest.fixture
def wb(tmp_path: Path, minimal_ifc4_path: Path):
    model = tmp_path / "work.ifc"
    shutil.copy2(minimal_ifc4_path, model)
    with Workbench.open(model, home=tmp_path / "home") as workbench:
        yield workbench


def test_workbench_is_exported_from_the_package():
    import ifc_console

    assert ifc_console.Workbench is Workbench
    assert "Workbench" in dir(ifc_console)


def test_open_loads_the_model(wb: Workbench):
    assert wb.model["loaded"] is True
    assert wb.model["schema"] == "IFC4"
    assert wb.mode == "ask"
    assert len(wb.model["content_sha256"]) == 64
    assert wb.context.active_model.content_sha256 == wb.model["content_sha256"]


def test_query_returns_rows(wb: Workbench):
    walls = wb.query("IfcWall")
    assert len(walls) == 3
    assert {"global_id", "class", "name"} <= set(walls[0])


def test_project_info_and_tree(wb: Workbench):
    assert wb.info()["project"]["name"] == "Duplex"
    assert wb.tree()["tree"]["class"] == "IfcProject"


def test_psets_reach_the_property_values(wb: Workbench):
    wall = next(w for w in wb.query("IfcWall") if w["name"] == "Wall-1")
    result = wb.psets(wall["global_id"])
    psets = result["results"][0]["psets"]
    assert psets["Pset_WallCommon"]["FireRating"] == "F30"


def test_tool_errors_become_exceptions_with_a_code(wb: Workbench):
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.query("IfcWall, ((broken")
    assert excinfo.value.code == "INVALID_QUERY"
    assert excinfo.value.hint


def test_call_returns_the_raw_envelope_instead_of_raising(wb: Workbench):
    envelope = wb.call("query_elements", query="NotAnIfcClass")
    assert set(envelope) >= {"ok", "meta"}
    assert envelope["meta"]["model"]


def test_tools_are_provider_neutral_schemas(wb: Workbench):
    tools = wb.tools()
    names = {tool["name"] for tool in tools}
    assert {"query_elements", "get_element", "execute_ifc_code"} <= names
    one = next(t for t in tools if t["name"] == "query_elements")
    assert one["input_schema"]["type"] == "object"
    assert "query" in one["input_schema"]["properties"]
    assert one["description"]
    assert one["output_schema"]["title"] == "Envelope"
    assert "rows" in one["data_schema"]["properties"]


def test_typed_operation_definitions_and_results(wb: Workbench):
    definitions = {item.name: item for item in wb.operation_definitions()}
    assert definitions["query_elements"].data_schema is not None

    query = wb.query_result("IfcWall")
    validation = wb.validation_result()
    assert len(query.rows) == 3
    assert validation.valid is True


def test_workspace_context_tracks_model_revisions(wb: Workbench):
    before = wb.context
    assert before.workspace_id.startswith("workspace-")
    assert before.active_model.model_id == wb.model["model_id"]
    assert before.active_model.revision_id is not None

    wb.set_mode("edit")
    wb.run_code(
        "ifc_api.root.create_entity(ifc, ifc_class='IfcWall', name='Revision')",
        "add one wall",
    )
    after = wb.context
    assert after.workspace_id == before.workspace_id
    assert after.active_model.revision_id != before.active_model.revision_id
    assert after.active_model.dirty is True


def test_validation_jobs_and_artifacts_use_typed_sdk(wb: Workbench, tmp_path: Path):
    submitted = wb.submit_validation_job()
    snapshots = list(wb.watch_job(submitted.job_id, poll_interval=0.01))
    completed = snapshots[-1]

    assert completed.state.value == "succeeded"
    assert snapshots[-1].progress == 100
    assert completed.summary["passed"] is True
    assert wb.job(completed.job_id).progress == 100
    assert completed in wb.jobs()
    json_ref = next(
        artifact for artifact in completed.artifacts if artifact.media_type == "application/json"
    )
    assert '"passed": true' in wb.read_artifact_text(json_ref.artifact_id)
    target = wb.export_artifact(json_ref.artifact_id, tmp_path / "sdk-report.json")
    assert target.is_file()
    assert wb.pin_artifact(json_ref.artifact_id).artifact_id == json_ref.artifact_id
    assert wb.plan_artifact_gc(older_than_days=1).candidate_count == 0
    assert wb.unpin_artifact(json_ref.artifact_id) is True
    with pytest.raises(IfcConsoleError) as unconfirmed:
        wb.collect_artifacts(wb.plan_artifact_gc(older_than_days=1))
    assert unconfirmed.value.code == "APPROVAL_REQUIRED"


def test_validation_job_refuses_a_dirty_sdk_model(wb: Workbench):
    wb.set_mode("edit")
    wb.run_code(
        "ifc_api.root.create_entity(ifc, ifc_class='IfcWall', name='Dirty')",
        "make model dirty",
    )
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.submit_validation_job()
    assert excinfo.value.code == "UNSAVED_CHANGES"


def test_safe_property_changes_require_caller_approval_and_can_restore(wb: Workbench):
    wall = next(item for item in wb.query("IfcWall") if item["name"] == "Wall-1")
    preview = wb.preview_property_change(
        wall["global_id"],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value="F60",
    )
    assert preview.change_set.changes[0].before == "F30"
    assert wb.change_set(preview.change_set_id) == preview
    approval = wb.approve_change_set(
        preview.change_set_id, approved_by="sdk-test", reason="verified preview"
    )

    with pytest.raises(IfcConsoleError) as blocked:
        wb.commit_change_set(preview.change_set_id, approval_id=approval.approval_id)
    assert blocked.value.code == "ASK_MODE_BLOCKED"

    wb.set_mode("edit")
    commit = wb.commit_change_set(preview.change_set_id, approval_id=approval.approval_id)
    assert commit.result.schema_valid is True
    assert wb.commit_record(commit.commit_id) == commit
    assert (
        wb.psets(wall["global_id"])["results"][0]["psets"]["Pset_WallCommon"]["FireRating"] == "F60"
    )

    restored = wb.restore_commit(commit.commit_id, confirm=True)
    assert restored.result.restored_sha256 == commit.result.previous_sha256
    assert (
        wb.psets(wall["global_id"])["results"][0]["psets"]["Pset_WallCommon"]["FireRating"] == "F30"
    )


def test_every_advertised_tool_is_callable(wb: Workbench):
    """tools() and call() must describe the same surface."""
    advertised = {tool["name"] for tool in wb.tools()}
    assert advertised <= set(wb.core.tool_functions)


def test_ask_mode_blocks_mutating_code(wb: Workbench):
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.run_code("ifc_api.root.create_entity(ifc, ifc_class='IfcWall')", "add a wall")
    assert excinfo.value.code == "ASK_MODE_BLOCKED"


def test_edit_mode_runs_the_api_and_saves(wb: Workbench, tmp_path: Path):
    wb.set_mode("edit")
    result = wb.run_code(
        "ifc_api.root.create_entity(ifc, ifc_class='IfcWall', name='SDK'); len(by_class('IfcWall'))",
        "add one wall",
    )
    assert result["result"] == "4"
    assert wb.model["dirty"] is True
    saved = wb.save(tmp_path / "out.ifc")
    assert Path(saved["path"]).exists()


def test_read_only_code_still_runs_in_ask_mode(wb: Workbench):
    result = wb.run_code("len(by_class('IfcWall'))")
    assert result["result"] == "3"


def test_schema_docs_work_without_the_knowledge_index(wb: Workbench):
    docs = wb.schema_docs(entity="IfcWall")
    assert docs["entity"] == "IfcWall"
    assert "Pset_WallCommon" in docs["applicable_psets"]


def test_settings_overrides_stay_in_memory(tmp_path: Path, minimal_ifc4_path: Path):
    home = tmp_path / "home"
    with Workbench.open(minimal_ifc4_path, home=home, settings={"knowledge.enabled": False}) as wb:
        assert wb.core.settings.knowledge.enabled is False
    written = (
        (home / "settings.json").read_text(encoding="utf-8")
        if (home / "settings.json").exists()
        else ""
    )
    assert '"enabled": false' not in written, "an override must not reach the user file"


def test_unknown_setting_is_refused(tmp_path: Path):
    with pytest.raises(IfcConsoleError):
        Workbench.open(None, home=tmp_path / "home", settings={"nope.nothing": 1})


def test_workbench_opens_without_a_model(tmp_path: Path):
    with Workbench.open(home=tmp_path / "home") as wb:
        assert wb.model["loaded"] is False
        with pytest.raises(IfcConsoleError) as excinfo:
            wb.query("IfcWall")
        assert excinfo.value.code == "NO_MODEL_LOADED"


def test_sdk_runtime_does_not_import_the_external_mcp_package(tmp_path: Path):
    script = textwrap.dedent(
        f"""
        import asyncio
        import builtins

        real_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == "mcp" or name.startswith("mcp."):
                raise AssertionError(f"external MCP import on SDK path: {{name}}")
            return real_import(name, *args, **kwargs)
        builtins.__import__ = guarded_import

        from ifc_console.sdk import AsyncWorkbench

        async def main():
            wb = await AsyncWorkbench.create(home={str(tmp_path / "subprocess-home")!r})
            try:
                result = await wb.status()
                assert result["model"]["loaded"] is False
                assert wb.operation_definitions()
            finally:
                wb.close()

        asyncio.run(main())
        """
    )
    env = os.environ.copy()
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_attached_models_are_listed(wb: Workbench, minimal_ifc4_path: Path):
    wb.attach(minimal_ifc4_path)
    listing = wb.models()
    assert len(listing["models"]) == 2


def test_close_is_idempotent(tmp_path: Path):
    wb = Workbench.open(home=tmp_path / "home")
    wb.close()
    with pytest.raises(RuntimeError):
        wb.status()


# ---------------------------------------------------------------- wb.ask(...)
def test_ask_runs_a_tool_and_returns_the_answer(wb: Workbench, monkeypatch):
    """The SDK drives the same loop the chat panel does, with any provider."""
    rounds = iter(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "c1", "name": "query_elements", "arguments": '{"query": "IfcWall"}'}
                    ],
                }
            ],
            [
                {"type": "content", "text": "The model has three walls."},
                {"type": "usage", "in": 120, "out": 8},
            ],
        ]
    )

    def fake_stream(provider, **kwargs):
        yield from next(rounds)

    monkeypatch.setattr("ifc_console.chat.agent.stream", fake_stream)
    result = wb.ask("how many walls?", model="test-model", api_key="sk-test")
    assert result["text"] == "The model has three walls."
    assert result["tool_calls"][0]["name"] == "query_elements"
    assert result["tool_calls"][0]["ok"] is True
    assert result["usage"] == {"in": 120, "out": 8}
    assert [turn["role"] for turn in result["turns"]] == ["user", "assistant"]


def test_ask_reports_events_as_they_stream(wb: Workbench, monkeypatch):
    def fake_stream(provider, **kwargs):
        yield {"type": "content", "text": "hello"}

    monkeypatch.setattr("ifc_console.chat.agent.stream", fake_stream)
    seen = []
    wb.ask("hi", model="m", api_key="k", on_event=seen.append)
    assert seen[0]["type"] == "content"


def test_ask_without_a_key_says_so(wb: Workbench, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(Exception) as excinfo:
        wb.ask("hi", model="m")
    assert "API key" in str(excinfo.value)
