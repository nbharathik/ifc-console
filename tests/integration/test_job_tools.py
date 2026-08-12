"""Job operations use the same durable application services as the SDK."""

from __future__ import annotations

from pathlib import Path


async def test_validation_job_operations(ask_harness, work_model: Path) -> None:
    submitted = await ask_harness.call("submit_validation_job")
    assert submitted["ok"] is True
    job_id = submitted["data"]["job"]["job_id"]

    completed = await ask_harness.call("get_job", job_id=job_id, wait_seconds=60)
    assert completed["ok"] is True
    job = completed["data"]["job"]
    assert job["state"] == "succeeded"
    assert job["summary"]["passed"] is True
    assert len(job["artifacts"]) == 2

    artifacts = await ask_harness.call("list_artifacts")
    assert artifacts["meta"]["returned"] >= 2


async def test_unknown_job_is_structured_error(ask_harness) -> None:
    result = await ask_harness.call("get_job", job_id="job-0000000000000000")
    assert result["ok"] is False
    assert result["error"]["code"] == "JOB_NOT_FOUND"

