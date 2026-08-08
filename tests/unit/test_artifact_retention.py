"""Reference-aware artifact retention and guarded collection."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import exclusive_file_lock
from ifc_console.application.retention import ArtifactRetentionService
from ifc_console.core.results import ToolError


def _old(service: ArtifactService, artifact_id: str) -> None:
    ref = service.get(artifact_id)
    path = service._metadata_path(ref.sha256)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")


def _put(service: ArtifactService, name: str, *, references: tuple[str, ...] = ()):
    return service.put_text(
        name,
        name=f"{name}.txt",
        kind="report",
        media_type="text/plain",
        producer="test",
        references=references,
    )


def test_gc_plan_retains_recent_pinned_job_and_referenced_artifacts(tmp_path: Path) -> None:
    artifacts = ArtifactService(tmp_path / "artifacts")
    retention = ArtifactRetentionService(artifacts, tmp_path / "jobs")
    candidate = _put(artifacts, "candidate")
    child = _put(artifacts, "child")
    parent = _put(artifacts, "parent", references=(child.artifact_id,))
    job_output = _put(artifacts, "job-output")
    recent = _put(artifacts, "recent")
    for ref in (candidate, child, parent, job_output):
        _old(artifacts, ref.artifact_id)
    retention.pin(parent.artifact_id)
    records = tmp_path / "jobs" / "records"
    records.mkdir(parents=True)
    (records / "job-example.json").write_text(
        json.dumps({"artifacts": [{"artifact_id": job_output.artifact_id}]}),
        encoding="utf-8",
    )

    plan = retention.plan(older_than_days=30)

    assert plan.candidate_ids == (candidate.artifact_id,)
    assert plan.retained_count == 4
    assert recent.artifact_id not in plan.candidate_ids
    assert child.artifact_id not in plan.candidate_ids
    assert job_output.artifact_id not in plan.candidate_ids


def test_gc_requires_confirmation_and_exact_unchanged_plan(tmp_path: Path) -> None:
    artifacts = ArtifactService(tmp_path / "artifacts")
    retention = ArtifactRetentionService(artifacts, tmp_path / "jobs")
    candidate = _put(artifacts, "candidate")
    _old(artifacts, candidate.artifact_id)
    plan = retention.plan(older_than_days=30)

    with pytest.raises(ToolError) as unconfirmed:
        retention.collect(plan)
    assert unconfirmed.value.code == "APPROVAL_REQUIRED"

    retention.pin(candidate.artifact_id)
    with pytest.raises(ToolError) as changed:
        retention.collect(plan, confirm=True)
    assert changed.value.code == "ARTIFACT_GC_CONFLICT"
    assert artifacts.get(candidate.artifact_id).artifact_id == candidate.artifact_id


def test_gc_deletes_only_reviewed_unreachable_artifacts(tmp_path: Path) -> None:
    artifacts = ArtifactService(tmp_path / "artifacts")
    retention = ArtifactRetentionService(artifacts, tmp_path / "jobs")
    candidate = _put(artifacts, "candidate")
    _old(artifacts, candidate.artifact_id)
    plan = retention.plan(older_than_days=30)

    result = retention.collect(plan, confirm=True)

    assert result.deleted_ids == (candidate.artifact_id,)
    assert result.deleted_bytes == candidate.size_bytes
    with pytest.raises(ToolError) as missing:
        artifacts.get(candidate.artifact_id)
    assert missing.value.code == "ARTIFACT_NOT_FOUND"


def test_transaction_artifact_kinds_are_protected_roots(tmp_path: Path) -> None:
    artifacts = ArtifactService(tmp_path / "artifacts")
    retention = ArtifactRetentionService(artifacts, tmp_path / "jobs")
    backup = artifacts.put_text(
        "backup",
        name="backup.ifc",
        kind="ifc-verified-backup",
        media_type="application/x-step",
        producer="test",
    )
    _old(artifacts, backup.artifact_id)

    assert retention.plan(older_than_days=30).candidate_count == 0


def test_artifact_store_lock_excludes_another_local_client(tmp_path: Path) -> None:
    lock_path = tmp_path / "artifacts" / ".store.lock"
    errors: list[str] = []

    def contend() -> None:
        try:
            with exclusive_file_lock(
                lock_path, timeout_s=0.05, error_code="ARTIFACT_STORE_BUSY"
            ):
                pass
        except ToolError as exc:
            errors.append(exc.code)

    with exclusive_file_lock(lock_path):
        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert errors == ["ARTIFACT_STORE_BUSY"]
