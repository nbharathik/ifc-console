"""The public SDK: scriptable BIM without a server or a terminal."""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from importlib import resources
from pathlib import Path

import pytest

from ifc_console.sdk import AsyncWorkbench, IfcConsoleError, Workbench, _Loop


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


def test_batch_record_is_exported_from_the_package():
    import ifc_console
    from ifc_console.core.batches import BatchRecord

    assert ifc_console.BatchRecord is BatchRecord


def test_workflow_contracts_are_exported_from_the_package():
    import ifc_console
    from ifc_console.core.workflows import (
        WorkflowInputSpec,
        WorkflowPlan,
        WorkflowQueryOperation,
        WorkflowRecord,
        WorkflowSpec,
        WorkflowStepSpec,
    )

    assert ifc_console.WorkflowPlan is WorkflowPlan
    assert ifc_console.WorkflowRecord is WorkflowRecord
    assert ifc_console.WorkflowSpec is WorkflowSpec
    assert ifc_console.WorkflowInputSpec is WorkflowInputSpec
    assert ifc_console.WorkflowStepSpec is WorkflowStepSpec
    assert ifc_console.WorkflowQueryOperation is WorkflowQueryOperation


def test_plugin_and_envelope_contracts_are_exported_from_the_package():
    import ifc_console
    from ifc_console.core.results import Envelope
    from ifc_console.plugins import PluginAPI, PluginManifest

    assert ifc_console.Envelope is Envelope
    assert ifc_console.PluginAPI is PluginAPI
    assert ifc_console.PluginManifest is PluginManifest


def test_package_declares_inline_typing():
    assert resources.files("ifc_console").joinpath("py.typed").is_file()


def test_open_loads_the_model(wb: Workbench):
    assert wb.model["loaded"] is True
    assert wb.model["schema"] == "IFC4"
    assert wb.mode == "ask"
    assert len(wb.model["content_sha256"]) == 64
    assert wb.context.active_model.content_sha256 == wb.model["content_sha256"]


def test_session_settings_are_validated_and_do_not_persist(wb: Workbench):
    changed = wb.configure(
        {
            "exec.output_char_limit": 12_000,
            "files.allow_ai_save": True,
        }
    )

    assert changed["exec.output_char_limit"] == 12_000
    assert wb.get_setting("exec.output_char_limit") == 12_000
    assert wb.settings["files.allow_ai_save"] is True
    assert wb.core.policy.allow_ai_save is True

    with pytest.raises(IfcConsoleError, match="unknown setting"):
        wb.configure({"not.a.setting": True})
    with pytest.raises(IfcConsoleError, match="lifecycle settings"):
        wb.configure({"server.port": 8765})


def test_query_returns_rows(wb: Workbench):
    walls = wb.query("IfcWall")
    assert len(walls) == 3
    assert {"global_id", "class", "name"} <= set(walls[0])


def test_search_resolves_names_global_ids_and_simple_selectors(wb: Workbench):
    named = wb.search("Wall-1")
    assert len(named) == 1
    assert named[0]["name"] == "Wall-1"

    by_id = wb.search(named[0]["global_id"])
    assert [row["global_id"] for row in by_id] == [named[0]["global_id"]]

    selected = wb.search_result("IfcWall")
    assert selected.mode == "selector"
    assert len(selected.results) == 3


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


def test_tools_can_filter_operations_blocked_by_the_current_profile(wb: Workbench):
    all_tools = {tool["name"] for tool in wb.tools()}
    permitted = {tool["name"] for tool in wb.tools(permitted_only=True)}

    assert "save_ifc_file" in all_tools
    assert "save_ifc_file" not in permitted
    assert "query_elements" in permitted


def test_typed_operation_definitions_and_results(wb: Workbench):
    definitions = {item.name: item for item in wb.operation_definitions()}
    assert definitions["query_elements"].data_schema is not None

    query = wb.query_result("IfcWall")
    validation = wb.validation_result()
    assert len(query.rows) == 3
    assert validation.valid is True


def test_sdk_exposes_capability_profiles_and_audit_verification(wb: Workbench):
    from ifc_console import Capability

    tools = {item["name"]: item for item in wb.tools()}
    assert tools["save_ifc_file"]["required_capabilities"] == [
        "file:write",
        "model:commit",
    ]
    assert tools["save_ifc_file"]["permitted"] is False
    assert Capability.MODEL_READ in wb.granted_capabilities()
    assert wb.capability_decision([Capability.MODEL_APPROVE]).allowed is False
    assert wb.capability_decision([Capability.MODEL_APPROVE], authority="caller").allowed is True
    assert wb.verify_audit().valid is True


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


def test_validation_batches_use_typed_sdk(wb: Workbench, tmp_path: Path):
    first = Path(wb.model["path"])
    second = tmp_path / "second.ifc"
    shutil.copy2(first, second)

    submitted = wb.submit_validation_batch([first, second], concurrency=1)
    snapshots = list(wb.watch_batch(submitted.batch_id, poll_interval=0.01))
    completed = snapshots[-1]

    assert completed.state.value == "succeeded"
    assert completed.summary["input_count"] == 2
    assert completed.aggregate_artifact is not None
    assert wb.batch(completed.batch_id) == completed
    assert completed in wb.batches()

    query = wb.submit_query_batch([first, second], query="IfcWall", output_format="jsonl", limit=2)
    query_result = wb.wait_batch(query.batch_id, timeout=90)
    assert query_result.state.value == "succeeded"
    assert query_result.children[0].summary["matched"] == 3
    assert (
        len(wb.read_artifact_text(query_result.children[0].artifacts[0].artifact_id).splitlines())
        == 2
    )


def test_workflows_use_typed_sdk(wb: Workbench, tmp_path: Path):
    manifest = tmp_path / "workflow.yaml"
    manifest.write_text(
        """version: '1'
name: sdk-gate
inputs:
  - id: models
    paths: [work.ifc]
steps:
  - id: walls
    operation:
      kind: query
      version: '1'
      query: IfcWall
      limit: 2
""",
        encoding="utf-8",
    )

    plan = wb.plan_workflow(manifest)
    assert plan.total_children == 1
    submitted = wb.submit_workflow_plan(plan)
    snapshots = list(wb.watch_workflow(submitted.workflow_id, poll_interval=0.01))
    completed = snapshots[-1]

    assert completed.state.value == "succeeded"
    assert completed.steps[0].summary["row_count"] == 2
    assert wb.workflow(completed.workflow_id) == completed
    assert completed in wb.workflows()


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
    commit_job = next(record for record in wb.jobs() if record.kind == "commit")
    assert commit_job.summary["commit_id"] == commit.commit_id
    assert commit_job.phase == "receipt_persisted"
    assert commit_job.transaction_id is not None
    assert wb.transaction_journal(commit_job.transaction_id).receipt_artifact_id == commit.commit_id
    assert wb.transaction_journals()
    assert (
        wb.psets(wall["global_id"])["results"][0]["psets"]["Pset_WallCommon"]["FireRating"] == "F60"
    )

    restored = wb.restore_commit(commit.commit_id, confirm=True)
    assert restored.result.restored_sha256 == commit.result.previous_sha256
    restore_job = next(record for record in wb.jobs() if record.kind == "restore")
    assert restore_job.summary["restore_id"] == restored.restore_id
    assert (
        wb.psets(wall["global_id"])["results"][0]["psets"]["Pset_WallCommon"]["FireRating"] == "F30"
    )


def test_sdk_previews_property_creation_and_classification_assignment(wb: Workbench):
    wall = next(item for item in wb.query("IfcWall") if item["name"] == "Wall-1")
    created_property = wb.preview_property_change(
        wall["global_id"],
        pset_name="Company_QA",
        property_name="ReviewStatus",
        value="Checked",
        create_missing=True,
    )
    classification = wb.preview_classification_assignment(
        wall["global_id"],
        classification_name="Company Classification",
        identification="WALL-EXT",
        reference_name="External wall",
    )

    assert created_property.change_set.changes[0].kind == "property_create"
    assert classification.change_set.operation == "classification.assign"
    assert classification.change_set.changes[0].kind == "classification_assignment"


def test_every_advertised_tool_is_callable(wb: Workbench):
    """tools() and call() must describe the same surface."""
    advertised = {tool["name"] for tool in wb.tools()}
    assert advertised <= set(wb.core.tool_functions)


def test_ask_mode_blocks_mutating_code(wb: Workbench):
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.run_code("ifc_api.root.create_entity(ifc, ifc_class='IfcWall')", "add a wall")
    assert excinfo.value.code == "ASK_MODE_BLOCKED"


def test_edit_mode_runs_the_api_but_ai_save_is_off_by_default(wb: Workbench, tmp_path: Path):
    wb.set_mode("edit")
    result = wb.run_code(
        "ifc_api.root.create_entity(ifc, ifc_class='IfcWall', name='SDK'); len(by_class('IfcWall'))",
        "add one wall",
    )
    assert result["result"] == "4"
    assert wb.model["dirty"] is True
    with pytest.raises(IfcConsoleError) as excinfo:
        wb.save(tmp_path / "out.ifc")
    assert excinfo.value.code == "AI_SAVE_DISABLED"
    assert not (tmp_path / "out.ifc").exists()


def test_sdk_can_explicitly_opt_in_to_ai_save(tmp_path: Path, minimal_ifc4_path: Path):
    output = tmp_path / "out.ifc"
    with Workbench.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
        mode="edit",
        allowed_dirs=(tmp_path,),
        settings={"files.allow_ai_save": True},
    ) as workbench:
        saved = workbench.save(output)
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


def test_invalid_setting_override_is_validated(tmp_path: Path):
    with pytest.raises(IfcConsoleError) as excinfo:
        Workbench.open(None, home=tmp_path / "home", settings={"server.port": 80})

    assert excinfo.value.code == "INVALID_INPUT"
    assert "server.port" in excinfo.value.message


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
    wb.close()
    with pytest.raises(RuntimeError):
        wb.status()


def test_sync_loop_timeout_cancels_background_work():
    loop = _Loop()
    cancelled = threading.Event()
    completed = threading.Event()

    async def delayed() -> None:
        try:
            await asyncio.sleep(0.2)
            completed.set()
        finally:
            cancelled.set()

    try:
        with pytest.raises(TimeoutError):
            loop.run(delayed(), timeout=0.01)
        assert cancelled.is_set()
        time.sleep(0.25)
        assert not completed.is_set()
    finally:
        loop.close()


@pytest.mark.parametrize("method_name", ["wait_job", "wait_batch", "wait_workflow"])
def test_sync_waits_rely_on_the_requested_service_timeout(method_name: str):
    outer_timeouts: list[float | None] = []
    service_timeouts: list[float | None] = []

    class FakeLoop:
        closed = False

        def run(self, coro, timeout):
            outer_timeouts.append(timeout)
            return asyncio.run(coro)

    class FakeAsyncWorkbench:
        async def wait_job(self, _record_id, *, timeout=None):
            service_timeouts.append(timeout)
            return "record"

        async def wait_batch(self, _record_id, *, timeout=None):
            service_timeouts.append(timeout)
            return "record"

        async def wait_workflow(self, _record_id, *, timeout=None):
            service_timeouts.append(timeout)
            return "record"

    workbench = Workbench(FakeAsyncWorkbench(), FakeLoop())

    assert getattr(workbench, method_name)("record-id", timeout=900.0) == "record"
    assert service_timeouts == [900.0]
    assert outer_timeouts == [None]


def test_explicit_close_inside_context_is_safe(tmp_path: Path):
    with Workbench.open(home=tmp_path / "home") as wb:
        wb.close()


@pytest.mark.parametrize(
    "method_name",
    ["ask", "submit_validation_job", "submit_validation_batch", "submit_query_batch"],
)
def test_sync_and_async_sdk_methods_expose_the_same_parameters(method_name: str):
    sync = inspect.signature(getattr(Workbench, method_name))
    async_ = inspect.signature(getattr(AsyncWorkbench, method_name))

    assert tuple(sync.parameters) == tuple(async_.parameters)


def test_sdk_lifecycle_errors_use_the_public_exception(tmp_path: Path):
    with pytest.raises(IfcConsoleError) as missing:
        Workbench.open(tmp_path / "missing.ifc", home=tmp_path / "home")
    assert missing.value.code == "FILE_NOT_FOUND"

    with pytest.raises(IfcConsoleError) as invalid_mode:
        Workbench.open(home=tmp_path / "other-home", mode="unsafe")
    assert invalid_mode.value.code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_async_create_closes_the_core_when_initial_model_loading_fails(
    tmp_path: Path, monkeypatch
):
    import ifc_console.app as app_module
    import ifc_console.application.operations as operations_module

    events: list[str] = []

    class FakeCore:
        def __init__(self, *_args, **_kwargs) -> None:
            events.append("created")

        def start_audit(self) -> None:
            events.append("started")

        def add_allowed_dir(self, _path: Path) -> None:
            pass

        async def ashutdown(self) -> None:
            events.append("closed")

    async def fail_open(_self, _path) -> None:
        raise IfcConsoleError("FILE_NOT_FOUND", "model is missing")

    monkeypatch.setattr(app_module, "AppCore", FakeCore)
    monkeypatch.setattr(operations_module, "build_operations", lambda _core: None)
    monkeypatch.setattr(AsyncWorkbench, "open_model", fail_open)

    with pytest.raises(IfcConsoleError) as failure:
        await AsyncWorkbench.create(tmp_path / "missing.ifc", home=tmp_path / "home")

    assert failure.value.code == "FILE_NOT_FOUND"
    assert events == ["created", "started", "closed"]


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
