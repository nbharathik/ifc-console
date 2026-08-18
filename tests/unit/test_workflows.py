"""Versioned, deterministic, durable IFC automation workflows."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from ifc_console.app import AppCore
from ifc_console.core.results import ToolError
from ifc_console.core.workflows import WorkflowState, WorkflowStepState
from ifc_console.policy.modes import Mode
from ifc_console.sandbox.client import worker_executable
from ifc_console.settings import SettingsStore


def _core(tmp_path: Path) -> AppCore:
    core = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        mode=Mode.ASK,
        transport="sdk",
    )
    core.start_audit()
    core.add_allowed_dir(tmp_path)
    return core


def _model(tmp_path: Path, source: Path, name: str = "model.ifc") -> Path:
    target = tmp_path / name
    shutil.copy2(source, target)
    return target


def _manifest(*, query: str = "IfcWall") -> dict[str, object]:
    return {
        "version": "1",
        "name": "submission-gate",
        "inputs": [{"id": "models", "paths": ["*.ifc"]}],
        "step_concurrency": 2,
        "steps": [
            {
                "id": "validate",
                "operation": {"kind": "validation", "version": "1"},
            },
            {
                "id": "walls",
                "needs": ["validate"],
                "operation": {
                    "kind": "query",
                    "version": "1",
                    "query": query,
                    "fields": ["name", "storey"],
                    "limit": 2,
                },
            },
        ],
    }


@pytest.mark.parametrize("suffix", [".json", ".yaml"])
async def test_plan_captures_sources_without_scheduling(
    tmp_path: Path, minimal_ifc4_path: Path, suffix: str
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / f"workflow{suffix}"
    payload = _manifest()
    if suffix == ".json":
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    else:
        manifest.write_text(yaml.safe_dump(payload), encoding="utf-8")
    core = _core(tmp_path)
    try:
        first = await core.workflows.plan_manifest(manifest)
        second = await core.workflows.plan_manifest(manifest)

        assert first.plan_id == second.plan_id
        assert first.total_children == 2
        assert [step.id for step in first.steps] == ["validate", "walls"]
        assert len(first.steps[0].batch_spec.inputs[0].sha256) == 64
        assert core.workflows.list() == []
        assert core.batches.list() == []
        assert core.jobs.list() == []
    finally:
        await core.ashutdown()


async def test_workflow_executes_dependencies_and_persists_aggregate_manifest(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    try:
        submitted = await core.workflows.submit_manifest(manifest)
        completed = await core.workflows.wait(submitted.workflow_id, timeout=90)

        assert completed.state is WorkflowState.SUCCEEDED
        assert completed.progress == 100
        assert completed.summary["passed"] is True
        assert [step.state for step in completed.steps] == [
            WorkflowStepState.SUCCEEDED,
            WorkflowStepState.SUCCEEDED,
        ]
        assert all(step.batch_id is not None for step in completed.steps)
        assert completed.steps[1].summary["row_count"] == 2
        assert completed.aggregate_artifact is not None
        assert set(completed.aggregate_artifact.references) == {
            step.artifact.artifact_id
            for step in completed.steps
            if step.artifact is not None
        }
        aggregate = json.loads(
            core.artifacts.read_text(completed.aggregate_artifact.artifact_id)
        )
        assert aggregate["workflow_id"] == completed.workflow_id
        assert aggregate["plan_id"] == completed.plan.plan_id
        assert [step["id"] for step in aggregate["steps"]] == ["validate", "walls"]
    finally:
        await core.ashutdown()

    reopened = _core(tmp_path)
    try:
        assert reopened.workflows.get(completed.workflow_id) == completed
    finally:
        await reopened.ashutdown()


async def test_failed_dependency_is_skipped_with_structured_failure(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    payload = _manifest(query="IfcWall, ((broken")
    payload["steps"] = list(reversed(payload["steps"]))
    payload["steps"][0]["needs"] = []
    payload["steps"][1]["needs"] = ["walls"]
    manifest = tmp_path / "failed.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    core = _core(tmp_path)
    try:
        submitted = await core.workflows.submit_manifest(manifest)
        completed = await core.workflows.wait(submitted.workflow_id, timeout=30)

        assert completed.state is WorkflowState.FAILED
        assert completed.steps[0].state is WorkflowStepState.FAILED
        assert completed.steps[0].failure is not None
        assert completed.steps[1].state is WorkflowStepState.SKIPPED
        assert completed.steps[1].failure is not None
        assert completed.steps[1].failure.code == "WORKFLOW_DEPENDENCY_FAILED"
    finally:
        await core.ashutdown()


async def test_submit_refuses_source_changed_after_plan(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    model = _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    try:
        plan = await core.workflows.plan_manifest(manifest)
        model.write_bytes(model.read_bytes() + b"\n")

        with pytest.raises(ToolError) as excinfo:
            await core.workflows.submit_plan(plan)
        assert excinfo.value.code == "WORKFLOW_SOURCE_CHANGED"
        assert core.workflows.list() == []
    finally:
        await core.ashutdown()


async def test_serialized_plan_rejects_a_tampered_identity(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    try:
        plan = await core.workflows.plan_manifest(manifest)
        payload = plan.model_dump(mode="json")
        payload["plan_id"] = "sha256:" + "0" * 64

        with pytest.raises(ValidationError, match="immutable content"):
            type(plan).model_validate(payload)
    finally:
        await core.ashutdown()


async def test_cancel_then_resume_reuses_completed_workflow_steps(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    normal_command = core.jobs._worker_command
    calls = 0

    def controlled_command(input_path: Path) -> tuple[str, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return normal_command(input_path)
        return (worker_executable(), "-c", "import time; time.sleep(60)")

    monkeypatch.setattr(core.jobs, "_worker_command", controlled_command)
    try:
        submitted = await core.workflows.submit_manifest(manifest)
        for _ in range(1000):
            current = core.workflows.get(submitted.workflow_id)
            if (
                current.steps[0].state is WorkflowStepState.SUCCEEDED
                and current.steps[1].state is WorkflowStepState.RUNNING
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("workflow did not reach the controlled cancellation point")

        cancelled = await core.workflows.cancel(submitted.workflow_id)
        assert cancelled.state is WorkflowState.CANCELLED
        assert cancelled.steps[0].state is WorkflowStepState.SUCCEEDED
        first_batch_id = cancelled.steps[0].batch_id

        monkeypatch.setattr(core.jobs, "_worker_command", normal_command)
        resumed = await core.workflows.resume(cancelled.workflow_id)
        completed = await core.workflows.wait(resumed.workflow_id, timeout=90)

        assert completed.state is WorkflowState.SUCCEEDED
        assert completed.run_count == 2
        assert completed.summary["reused"] == 1
        assert completed.steps[0].attempts == 1
        assert completed.steps[0].batch_id == first_batch_id
        assert completed.steps[1].attempts == 2
    finally:
        await core.ashutdown()


async def test_concurrent_resume_schedules_one_workflow_supervisor(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    monkeypatch.setattr(core.workflows, "_schedule", lambda _workflow_id: None)
    release = threading.Event()
    started = threading.Event()

    def delayed_match(_source) -> bool:
        started.set()
        if not release.wait(5):
            raise RuntimeError("timed out waiting to release source verification")
        return True

    try:
        submitted = await core.workflows.submit_manifest(manifest)
        core.workflows._replace(
            submitted.workflow_id,
            state=WorkflowState.CANCELLED,
            message="workflow cancelled for resume race test",
        )
        scheduled: list[str] = []

        def schedule(workflow_id: str) -> None:
            scheduled.append(workflow_id)
            core.workflows._replace(
                workflow_id,
                state=WorkflowState.FAILED,
                message="workflow supervisor failed immediately",
            )

        monkeypatch.setattr(core.workflows, "_schedule", schedule)
        monkeypatch.setattr(
            "ifc_console.application.workflows.source_matches", delayed_match
        )

        first = asyncio.create_task(core.workflows.resume(submitted.workflow_id))
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            pytest.fail("first resume did not start source verification")
        second = asyncio.create_task(core.workflows.resume(submitted.workflow_id))
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

        errors = [outcome for outcome in outcomes if isinstance(outcome, ToolError)]
        assert len(errors) == 1
        assert errors[0].code == "WORKFLOW_NOT_RESUMABLE"
        assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
        assert scheduled == [submitted.workflow_id]
        assert core.workflows.get(submitted.workflow_id).run_count == 2
        assert not core.workflows._resume_locks
        assert not core.workflows._resume_lock_users
    finally:
        release.set()
        await core.ashutdown()


async def test_dead_owner_recovers_as_resumable_interruption(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    submitted = await core.workflows.submit_manifest(manifest)
    completed = await core.workflows.wait(submitted.workflow_id, timeout=90)
    await core.ashutdown()

    record_path = (
        tmp_path
        / "home"
        / "workflows"
        / "records"
        / f"{completed.workflow_id}.json"
    )
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["record"]["state"] = "running"
    payload["record"]["progress"] = 50
    payload["record"]["failure"] = None
    payload["record"]["aggregate_artifact"] = None
    payload["record"]["summary"] = {}
    payload["record"]["steps"][1]["state"] = "running"
    payload["record"]["steps"][1]["failure"] = None
    payload["owner_pid"] = 2_147_483_647
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = _core(tmp_path)
    try:
        recovered = reopened.workflows.get(completed.workflow_id)
        assert recovered.state is WorkflowState.INTERRUPTED
        assert recovered.steps[0].state is WorkflowStepState.SUCCEEDED
        assert recovered.steps[1].state is WorkflowStepState.INTERRUPTED
        assert recovered.steps[1].failure is not None
        assert recovered.steps[1].failure.code == "WORKFLOW_INTERRUPTED"

        resumed = await reopened.workflows.resume(completed.workflow_id)
        final = await reopened.workflows.wait(resumed.workflow_id, timeout=90)
        assert final.state is WorkflowState.SUCCEEDED
        assert final.steps[0].attempts == 1
        assert final.steps[1].attempts == 2
    finally:
        await reopened.ashutdown()


async def test_submission_is_not_scheduled_when_record_cannot_persist(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    plan = await core.workflows.plan_manifest(manifest)

    def disk_full(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core.workflows, "_write_json", disk_full)
    try:
        with pytest.raises(ToolError) as excinfo:
            await core.workflows.submit_plan(plan)
        assert excinfo.value.code == "WORKFLOW_STORE_FAILED"
        assert not core.workflows._tasks
        assert not core.workflows._records
    finally:
        await core.ashutdown()


async def test_corrupt_record_is_reported_instead_of_serving_stale_state(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    try:
        submitted = await core.workflows.submit_manifest(manifest)
        completed = await core.workflows.wait(submitted.workflow_id, timeout=90)
        record_path = (
            tmp_path
            / "home"
            / "workflows"
            / "records"
            / f"{completed.workflow_id}.json"
        )
        record_path.write_text("{broken", encoding="utf-8")

        with pytest.raises(ToolError) as excinfo:
            core.workflows.get(completed.workflow_id)
        assert excinfo.value.code == "WORKFLOW_STORE_CORRUPT"
    finally:
        await core.ashutdown()


async def test_resolved_input_limit_is_enforced(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _model(tmp_path, minimal_ifc4_path, "one.ifc")
    _model(tmp_path, minimal_ifc4_path, "two.ifc")
    manifest = tmp_path / "workflow.json"
    manifest.write_text(json.dumps(_manifest()), encoding="utf-8")
    core = _core(tmp_path)
    monkeypatch.setattr(core.workflows, "_MAX_RESOLVED_FILES", 1)
    try:
        with pytest.raises(ToolError) as excinfo:
            await core.workflows.plan_manifest(manifest)
        assert excinfo.value.code == "WORKFLOW_INPUT_LIMIT"
    finally:
        await core.ashutdown()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["inputs"][0].update(paths=["../outside.ifc"]),
        lambda payload: payload["steps"][0].update(needs=["walls"]),
    ],
)
async def test_invalid_paths_and_cycles_are_rejected_before_scheduling(
    tmp_path: Path, minimal_ifc4_path: Path, mutate
) -> None:
    _model(tmp_path, minimal_ifc4_path)
    payload = _manifest()
    mutate(payload)
    manifest = tmp_path / "invalid.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    core = _core(tmp_path)
    try:
        with pytest.raises(ToolError) as excinfo:
            await core.workflows.plan_manifest(manifest)
        assert excinfo.value.code in {
            "WORKFLOW_MANIFEST_INVALID",
            "WORKFLOW_PATH_INVALID",
        }
        assert core.batches.list() == []
        assert core.jobs.list() == []
    finally:
        await core.ashutdown()
