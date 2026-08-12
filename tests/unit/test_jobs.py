"""Durable validation jobs and supervised cancellation."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

from ifc_console.app import AppCore
from ifc_console.core.jobs import JobState
from ifc_console.core.results import ToolError
from ifc_console.policy.modes import Mode
from ifc_console.sandbox.client import worker_executable
from ifc_console.settings import SettingsStore


def test_pid_probe_is_read_only() -> None:
    from ifc_console.application.jobs import JobService

    assert JobService._pid_exists(os.getpid()) is True


async def _core(tmp_path: Path, source: Path) -> tuple[AppCore, Path]:
    model = tmp_path / "model.ifc"
    shutil.copy2(source, model)
    store = SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={})
    core = AppCore(store, mode=Mode.ASK, transport="sdk")
    core.start_audit()
    core.add_allowed_dir(tmp_path)
    await core.open_model(model)
    return core, model


def _property_target(core: AppCore) -> str:
    wall = core.session.ifc.by_type("IfcWall")[0]
    return str(wall.GlobalId)


async def test_validation_job_persists_progress_and_artifacts(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    try:
        submitted = await core.jobs.submit_validation()
        snapshots = [
            snapshot
            async for snapshot in core.jobs.watch(
                submitted.job_id, poll_interval=0.01
            )
        ]
        record = snapshots[-1]

        assert record.state is JobState.SUCCEEDED
        assert record.summary == {"passed": True, "schema": "IFC4", "issue_count": 0}
        assert record.progress == 100
        assert any(snapshot.progress < 100 for snapshot in snapshots)
        assert {event.type for event in record.events} >= {"queued", "running", "progress"}
        assert record.worker["environment"] == "minimal"
        assert "network-blocked" in record.worker["controls"]
        assert {artifact.media_type for artifact in record.artifacts} == {
            "application/json",
            "application/sarif+json",
        }
        report_ref = next(
            artifact
            for artifact in record.artifacts
            if artifact.media_type == "application/json"
        )
        report = json.loads(core.artifacts.read_text(report_ref.artifact_id))
        assert report["passed"] is True
        assert report_ref.revision == record.spec.revision
        assert record.spec.context is not None
        assert record.worker["correlation_id"] == record.spec.context.correlation_id
        assert report_ref.correlation_ids == (record.spec.context.correlation_id,)
        job_events = [
            event
            for event in core.audit.tail(100)
            if event.get("job_id") == record.job_id
        ]
        assert job_events
        assert {event["correlation_id"] for event in job_events} == {
            record.spec.context.correlation_id
        }
    finally:
        core.shutdown()


async def test_completed_job_is_loaded_by_a_new_process_context(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    submitted = await core.jobs.submit_validation()
    completed = await core.jobs.wait(submitted.job_id, timeout=60)
    core.shutdown()

    reopened = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        transport="cli",
    )
    try:
        restored = reopened.jobs.get(completed.job_id)
        assert restored.state is JobState.SUCCEEDED
        assert restored.artifacts == completed.artifacts
    finally:
        reopened.shutdown()


async def test_orphaned_running_job_recovers_as_failed(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    submitted = await core.jobs.submit_validation()
    completed = await core.jobs.wait(submitted.job_id, timeout=60)
    core.shutdown()

    record_path = tmp_path / "home" / "jobs" / "records" / f"{completed.job_id}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["record"]["state"] = "running"
    payload["record"]["progress"] = 40
    payload["record"]["failure"] = None
    payload["owner_pid"] = 2_147_483_647
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        transport="cli",
    )
    try:
        restored = reopened.jobs.get(completed.job_id)
        assert restored.state is JobState.FAILED
        assert restored.failure is not None
        assert restored.failure.code == "JOB_WORKER_FAILED"
    finally:
        reopened.shutdown()


async def test_running_worker_can_be_cancelled(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    monkeypatch.setattr(
        core.jobs,
        "_worker_command",
        lambda _path: (worker_executable(), "-c", "import time; time.sleep(60)"),
    )
    try:
        submitted = await core.jobs.submit_validation()
        for _ in range(100):
            if submitted.job_id in core.jobs._processes:
                break
            await asyncio.sleep(0.01)
        cancelled = await core.jobs.cancel(submitted.job_id)
        assert cancelled.state is JobState.CANCELLED
        assert cancelled.cancel_requested is True
    finally:
        core.shutdown()


async def test_worker_crash_becomes_structured_failure(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    monkeypatch.setattr(
        core.jobs,
        "_worker_command",
        lambda _path: (worker_executable(), "-c", "raise RuntimeError('boom')"),
    )
    try:
        submitted = await core.jobs.submit_validation()
        failed = await core.jobs.wait(submitted.job_id, timeout=10)
        assert failed.state is JobState.FAILED
        assert failed.failure is not None
        assert failed.failure.code == "JOB_WORKER_FAILED"
        assert "boom" in failed.failure.message
    finally:
        core.shutdown()


async def test_source_change_fails_revision_bound_job(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, model = await _core(tmp_path, minimal_ifc4_path)
    try:
        submitted = await core.jobs.submit_validation()
        model.write_bytes(model.read_bytes() + b"\n")
        record = await core.jobs.wait(submitted.job_id, timeout=60)
        assert record.state is JobState.FAILED
        assert record.failure is not None
        assert record.failure.code == "SOURCE_CHANGED"
        assert not record.artifacts
    finally:
        core.shutdown()


async def test_second_local_context_can_request_cancellation(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    monkeypatch.setattr(
        core.jobs,
        "_worker_command",
        lambda _path: (worker_executable(), "-c", "import time; time.sleep(60)"),
    )
    observer: AppCore | None = None
    try:
        submitted = await core.jobs.submit_validation()
        for _ in range(100):
            if submitted.job_id in core.jobs._processes:
                break
            await asyncio.sleep(0.01)
        observer = AppCore(
            SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
            transport="cli",
        )
        requested = await observer.jobs.cancel(submitted.job_id)
        assert requested.cancel_requested is True

        cancelled = await core.jobs.wait(submitted.job_id, timeout=10)
        assert cancelled.state is JobState.CANCELLED
        assert cancelled.cancel_requested is True
    finally:
        if observer is not None:
            observer.shutdown()
        core.shutdown()


async def test_dirty_model_job_is_refused(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    core.session.mark_dirty()
    try:
        with pytest.raises(ToolError) as excinfo:
            await core.jobs.submit_validation()
        assert excinfo.value.code == "UNSAVED_CHANGES"
    finally:
        core.shutdown()


async def test_expected_revision_conflict_is_refused_before_worker_start(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    try:
        with pytest.raises(ToolError) as excinfo:
            await core.jobs.submit_validation(expected_revision="stale:1")
        assert excinfo.value.code == "REVISION_CONFLICT"
        assert not core.jobs.list()
    finally:
        core.shutdown()


async def test_commit_and_restore_are_durable_phase_jobs(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    try:
        preview = await core.transactions.preview_property_value(
            global_ids=[_property_target(core)],
            pset_name="Pset_WallCommon",
            property_name="FireRating",
            value="F60",
        )
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        core.set_mode(Mode.EDIT, by="test")

        submitted = await core.jobs.submit_commit(
            preview.change_set_id, approval_id=approval.approval_id
        )
        committed = await core.jobs.wait(submitted.job_id, timeout=60)

        assert committed.state is JobState.SUCCEEDED
        assert committed.kind == "commit"
        assert committed.phase == "receipt_persisted"
        assert committed.cancellable is False
        assert committed.transaction_id is not None
        assert committed.summary["commit_id"] == committed.artifacts[0].artifact_id
        assert {event.type for event in committed.events} >= {"queued", "running", "phase"}

        restored_submit = await core.jobs.submit_restore(
            str(committed.summary["commit_id"]), confirm=True
        )
        restored = await core.jobs.wait(restored_submit.job_id, timeout=60)

        assert restored.state is JobState.SUCCEEDED
        assert restored.kind == "restore"
        assert restored.phase == "receipt_persisted"
        assert restored.cancellable is False
        assert core.transactions.get_restore(str(restored.summary["restore_id"]))
    finally:
        core.shutdown()


async def test_cancel_rejects_a_job_after_its_commit_point(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    monkeypatch.setattr(
        core.jobs,
        "_worker_command",
        lambda _path: (worker_executable(), "-c", "import time; time.sleep(60)"),
    )
    try:
        submitted = await core.jobs.submit_validation()
        core.jobs._replace(
            submitted.job_id,
            phase="commit_point",
            cancellable=False,
        )
        with pytest.raises(ToolError) as excinfo:
            await core.jobs.cancel(submitted.job_id)
        assert excinfo.value.code == "JOB_NOT_CANCELLABLE"
    finally:
        core.shutdown()


async def test_orphaned_commit_job_recovers_from_durable_receipt(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, _model = await _core(tmp_path, minimal_ifc4_path)
    preview = await core.transactions.preview_property_value(
        global_ids=[_property_target(core)],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value="F60",
    )
    approval = core.transactions.approve(preview.change_set_id, approved_by="test")
    core.set_mode(Mode.EDIT, by="test")
    submitted = await core.jobs.submit_commit(
        preview.change_set_id, approval_id=approval.approval_id
    )
    completed = await core.jobs.wait(submitted.job_id, timeout=60)
    core.shutdown()

    record_path = tmp_path / "home" / "jobs" / "records" / f"{completed.job_id}.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["record"]["state"] = "running"
    payload["record"]["progress"] = 82
    payload["record"]["message"] = "supervisor exited after target verification"
    payload["record"]["failure"] = None
    payload["owner_pid"] = 2_147_483_647
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        mode=Mode.EDIT,
        transport="cli",
    )
    try:
        recovered = reopened.jobs.get(completed.job_id)
        assert recovered.state is JobState.SUCCEEDED
        assert recovered.phase == "receipt_persisted"
        assert recovered.summary["commit_id"] == completed.summary["commit_id"]
        assert recovered.summary["recovered"] is True
    finally:
        reopened.shutdown()
