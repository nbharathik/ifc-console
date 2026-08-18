"""Durable bounded validation batches, cancellation, and resume."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest

from ifc_console.app import AppCore
from ifc_console.core.batches import BatchState
from ifc_console.core.jobs import JobState
from ifc_console.core.results import ToolError
from ifc_console.policy.modes import Mode
from ifc_console.sandbox.client import worker_executable
from ifc_console.settings import SettingsStore


async def _core(tmp_path: Path, source: Path, *, count: int = 3) -> tuple[AppCore, tuple[Path, ...]]:
    models: list[Path] = []
    for index in range(count):
        model = tmp_path / f"model-{index}.ifc"
        shutil.copy2(source, model)
        models.append(model)
    core = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        mode=Mode.ASK,
        transport="sdk",
    )
    core.start_audit()
    core.add_allowed_dir(tmp_path)
    return core, tuple(models)


async def test_validation_batch_persists_children_and_manifest(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path)
    try:
        submitted = await core.batches.submit_validation(models, concurrency=2)
        completed = await core.batches.wait(submitted.batch_id, timeout=90)

        assert completed.state is BatchState.SUCCEEDED
        assert completed.progress == 100
        assert completed.summary == {
            "input_count": 3,
            "succeeded": 3,
            "failed": 0,
            "cancelled": 0,
            "passed": True,
            "reused": 0,
        }
        assert all(child.state is JobState.SUCCEEDED for child in completed.children)
        assert all(child.attempts == 1 for child in completed.children)
        assert all(child.job_id is not None for child in completed.children)
        assert completed.aggregate_artifact is not None
        manifest = json.loads(
            core.artifacts.read_text(completed.aggregate_artifact.artifact_id)
        )
        assert manifest["batch_id"] == completed.batch_id
        assert manifest["state"] == "succeeded"
        assert len(manifest["children"]) == 3
        assert set(completed.aggregate_artifact.references) == {
            artifact.artifact_id
            for child in completed.children
            for artifact in child.artifacts
        }

        reopened = AppCore(
            SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
            transport="cli",
        )
        try:
            assert reopened.batches.get(completed.batch_id) == completed
        finally:
            reopened.shutdown()
    finally:
        core.shutdown()


@pytest.mark.parametrize(
    ("output_format", "media_type"),
    [("jsonl", "application/x-ndjson"), ("csv", "text/csv")],
)
async def test_query_batch_streams_typed_result_artifacts(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    output_format: str,
    media_type: str,
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=2)
    try:
        submitted = await core.batches.submit_query(
            models,
            query="IfcWall",
            fields=("name", "storey"),
            output_format=output_format,
            limit=2,
            concurrency=2,
        )
        completed = await core.batches.wait(submitted.batch_id, timeout=90)

        assert completed.state is BatchState.SUCCEEDED
        assert completed.spec.operation.kind == "query"
        assert completed.summary["passed"] is True
        for child in completed.children:
            assert child.summary["matched"] == 3
            assert child.summary["row_count"] == 2
            assert child.summary["truncated"] is True
            assert len(child.artifacts) == 1
            assert child.artifacts[0].kind == "query-result"
            assert child.artifacts[0].media_type == media_type
            result = core.artifacts.read_text(child.artifacts[0].artifact_id)
            assert "Wall-1" in result
            assert len(result.splitlines()) == (3 if output_format == "csv" else 2)
    finally:
        core.shutdown()


async def test_invalid_query_is_a_structured_child_failure(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=1)
    try:
        submitted = await core.batches.submit_query(models, query="IfcWall, ((broken")
        completed = await core.batches.wait(submitted.batch_id, timeout=30)

        assert completed.state is BatchState.FAILED
        assert completed.children[0].failure is not None
        assert completed.children[0].failure.code == "INVALID_QUERY"
        assert not completed.children[0].artifacts
    finally:
        core.shutdown()


async def test_cancel_then_resume_reuses_verified_successes(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path)
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
        submitted = await core.batches.submit_validation(models, concurrency=1)
        for _ in range(1000):
            current = core.batches.get(submitted.batch_id)
            if (
                current.children[0].state is JobState.SUCCEEDED
                and current.children[1].state is JobState.RUNNING
            ):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("batch did not reach the controlled cancellation point")

        cancelled = await core.batches.cancel(submitted.batch_id)
        assert cancelled.state is BatchState.CANCELLED
        assert cancelled.children[0].state is JobState.SUCCEEDED
        assert cancelled.children[0].attempts == 1

        monkeypatch.setattr(core.jobs, "_worker_command", normal_command)
        resumed = await core.batches.resume(submitted.batch_id)
        completed = await core.batches.wait(resumed.batch_id, timeout=90)

        assert completed.state is BatchState.SUCCEEDED
        assert completed.run_count == 2
        assert completed.children[0].attempts == 1
        assert completed.children[1].attempts == 2
        assert completed.children[2].attempts == 1
        assert completed.summary["reused"] == 1
    finally:
        await core.ashutdown()


async def test_concurrent_resume_schedules_one_batch_supervisor(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=1)
    monkeypatch.setattr(core.batches, "_schedule", lambda _batch_id: None)
    release = threading.Event()
    started = threading.Event()

    def delayed_match(_source) -> bool:
        started.set()
        if not release.wait(5):
            raise RuntimeError("timed out waiting to release source verification")
        return True

    try:
        submitted = await core.batches.submit_validation(models)
        core.batches._replace(
            submitted.batch_id,
            state=BatchState.CANCELLED,
            message="batch cancelled for resume race test",
        )
        scheduled: list[str] = []

        def schedule(batch_id: str) -> None:
            scheduled.append(batch_id)
            core.batches._replace(
                batch_id,
                state=BatchState.FAILED,
                message="batch supervisor failed immediately",
            )

        monkeypatch.setattr(core.batches, "_schedule", schedule)
        monkeypatch.setattr(
            "ifc_console.application.batches.source_matches", delayed_match
        )

        first = asyncio.create_task(core.batches.resume(submitted.batch_id))
        for _ in range(500):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            release.set()
            await asyncio.gather(first, return_exceptions=True)
            pytest.fail("first resume did not start source verification")
        second = asyncio.create_task(core.batches.resume(submitted.batch_id))
        await asyncio.sleep(0)
        release.set()
        outcomes = await asyncio.gather(first, second, return_exceptions=True)

        errors = [outcome for outcome in outcomes if isinstance(outcome, ToolError)]
        assert len(errors) == 1
        assert errors[0].code == "BATCH_NOT_RESUMABLE"
        assert sum(not isinstance(outcome, BaseException) for outcome in outcomes) == 1
        assert scheduled == [submitted.batch_id]
        assert core.batches.get(submitted.batch_id).run_count == 2
        assert not core.batches._resume_locks
        assert not core.batches._resume_lock_users
    finally:
        release.set()
        await core.ashutdown()


async def test_resume_refuses_changed_captured_source(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=1)
    monkeypatch.setattr(
        core.jobs,
        "_worker_command",
        lambda _path: (worker_executable(), "-c", "import time; time.sleep(60)"),
    )
    try:
        submitted = await core.batches.submit_validation(models, concurrency=1)
        for _ in range(500):
            if core.batches.get(submitted.batch_id).children[0].state is JobState.RUNNING:
                break
            await asyncio.sleep(0.01)
        cancelled = await core.batches.cancel(submitted.batch_id)
        assert cancelled.state is BatchState.CANCELLED
        models[0].write_bytes(models[0].read_bytes() + b"\n")

        with pytest.raises(ToolError) as excinfo:
            await core.batches.resume(submitted.batch_id)
        assert excinfo.value.code == "BATCH_SOURCE_CHANGED"
    finally:
        await core.ashutdown()


async def test_fail_fast_stops_unscheduled_children(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path)
    monkeypatch.setattr(
        core.jobs,
        "_worker_command",
        lambda _path: (worker_executable(), "-c", "raise RuntimeError('batch boom')"),
    )
    try:
        submitted = await core.batches.submit_validation(
            models, concurrency=1, failure_policy="fail_fast"
        )
        completed = await core.batches.wait(submitted.batch_id, timeout=30)

        assert completed.state is BatchState.FAILED
        assert completed.children[0].state is JobState.FAILED
        assert completed.children[0].attempts == 1
        assert all(
            child.state is JobState.CANCELLED and child.attempts == 0
            for child in completed.children[1:]
        )
        assert completed.summary["failed"] == 1
        assert completed.summary["cancelled"] == 2
    finally:
        core.shutdown()


async def test_scheduler_never_exceeds_configured_concurrency_for_100_inputs(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=100)
    active = 0
    peak = 0

    async def fake_child(batch_id: str, index: int) -> JobState:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        child = core.batches.get(batch_id).children[index]
        core.batches._replace_child(
            batch_id,
            index,
            state=JobState.SUCCEEDED,
            attempts=child.attempts + 1,
            last_run=1,
            summary={"passed": True},
        )
        active -= 1
        return JobState.SUCCEEDED

    monkeypatch.setattr(core.batches, "_run_child", fake_child)
    try:
        submitted = await core.batches.submit_validation(models, concurrency=4)
        completed = await core.batches.wait(submitted.batch_id, timeout=30)

        assert completed.state is BatchState.SUCCEEDED
        assert completed.summary["input_count"] == 100
        assert peak == 4
    finally:
        core.shutdown()


async def test_submission_is_not_scheduled_when_batch_record_cannot_persist(
    tmp_path: Path,
    minimal_ifc4_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=1)

    def disk_full(_path: Path, _payload: dict[str, object]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(core.batches, "_write_json", disk_full)
    try:
        with pytest.raises(ToolError) as excinfo:
            await core.batches.submit_validation(models)
        assert excinfo.value.code == "BATCH_STORE_FAILED"
        assert not core.batches._tasks
        assert not core.batches._records
    finally:
        core.shutdown()


async def test_dead_batch_owner_recovers_as_resumable_interruption(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core, models = await _core(tmp_path, minimal_ifc4_path, count=2)
    submitted = await core.batches.submit_validation(models)
    completed = await core.batches.wait(submitted.batch_id, timeout=90)
    core.shutdown()

    record_path = (
        tmp_path / "home" / "batches" / "records" / f"{completed.batch_id}.json"
    )
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["record"]["state"] = "running"
    payload["record"]["progress"] = 50
    payload["record"]["failure"] = None
    payload["record"]["children"][1]["state"] = "running"
    payload["owner_pid"] = 2_147_483_647
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        transport="cli",
    )
    reopened.start_audit()
    try:
        recovered = reopened.batches.get(completed.batch_id)
        assert recovered.state is BatchState.INTERRUPTED
        assert recovered.children[0].state is JobState.SUCCEEDED
        assert recovered.children[1].state is JobState.FAILED
        assert recovered.children[1].failure is not None
        assert recovered.children[1].failure.code == "BATCH_INTERRUPTED"

        resumed = await reopened.batches.resume(completed.batch_id)
        final = await reopened.batches.wait(resumed.batch_id, timeout=90)
        assert final.state is BatchState.SUCCEEDED
        assert final.children[0].attempts == 1
        assert final.children[1].attempts == 2
    finally:
        await reopened.ashutdown()
