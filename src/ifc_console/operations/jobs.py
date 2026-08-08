"""Job and artifact operations."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.operation_data import (
    ArtifactData,
    ArtifactListData,
    JobData,
    JobListData,
)
from ifc_console.core.operations import OperationAnnotations, OperationRegistry
from ifc_console.core.results import Envelope, ok

if TYPE_CHECKING:
    from ifc_console.app import AppCore

READ_ANN = OperationAnnotations(readOnlyHint=True, destructiveHint=False)
START_ANN = OperationAnnotations(readOnlyHint=False, destructiveHint=False)
CANCEL_ANN = OperationAnnotations(readOnlyHint=False, destructiveHint=True)


def register(registry: OperationRegistry, core: AppCore) -> None:
    char_limit = core.settings.exec.output_char_limit

    @registry.tool(
        annotations=START_ANN,
        data_model=JobData,
        description=(
            "[AUTOMATION] Submit schema and optional IDS validation to an isolated "
            "worker. Returns immediately with a durable job_id. The model must be "
            "clean because the job is bound to verified source bytes and a revision."
        ),
    )
    @enveloped(core, "submit_validation_job")
    async def submit_validation_job(
        ids_paths: list[str] | None = None,
        express_rules: bool = False,
        max_issues: Annotated[int, Field(ge=1, le=2000)] = 200,
        model: str | None = None,
        expected_revision: str | None = None,
    ) -> Envelope:
        record = await core.jobs.submit_validation(
            model=model,
            ids_paths=tuple(ids_paths or ()),
            express_rules=express_rules,
            max_issues=max_issues,
            expected_revision=expected_revision,
        )
        return ok(
            {"job": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )

    @registry.tool(
        annotations=READ_ANN,
        data_model=JobData,
        description=(
            "[AUTOMATION] Get durable job state, progress, events, failure details, "
            "and result artifacts. Optionally wait for a terminal state."
        ),
    )
    @enveloped(core, "get_job")
    async def get_job(
        job_id: str,
        wait_seconds: Annotated[float, Field(ge=0, le=3600)] = 0,
    ) -> Envelope:
        if wait_seconds:
            record = await core.jobs.wait(job_id, timeout=wait_seconds)
        else:
            record = core.jobs.get(job_id)
        return ok(
            {"job": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )

    @registry.tool(
        annotations=READ_ANN,
        data_model=JobListData,
        description="[AUTOMATION] List recent durable jobs, newest first.",
    )
    @enveloped(core, "list_jobs")
    async def list_jobs(
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> Envelope:
        records = core.jobs.list(limit=limit)
        return ok(
            {"jobs": [record.model_dump(mode="json") for record in records]},
            core.session_meta(),
            char_limit=char_limit,
            returned=len(records),
        )

    @registry.tool(
        annotations=CANCEL_ANN,
        data_model=JobData,
        description=(
            "[AUTOMATION] Request cancellation of a queued or running job. "
            "Completed jobs are returned unchanged."
        ),
    )
    @enveloped(core, "cancel_job")
    async def cancel_job(job_id: str) -> Envelope:
        record = await core.jobs.cancel(job_id)
        return ok(
            {"job": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )

    @registry.tool(
        annotations=READ_ANN,
        data_model=ArtifactListData,
        description="[AUTOMATION] List recent content-addressed artifacts.",
    )
    @enveloped(core, "list_artifacts")
    async def list_artifacts(
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
    ) -> Envelope:
        refs = core.artifacts.list(limit=limit)
        return ok(
            {"artifacts": [ref.model_dump(mode="json") for ref in refs]},
            core.session_meta(),
            char_limit=char_limit,
            returned=len(refs),
        )

    @registry.tool(
        annotations=READ_ANN,
        data_model=ArtifactData,
        description=(
            "[AUTOMATION] Get metadata for a content-addressed artifact. "
            "The SDK or CLI can export its verified content."
        ),
    )
    @enveloped(core, "get_artifact")
    async def get_artifact(artifact_id: str) -> Envelope:
        ref = core.artifacts.get(artifact_id)
        return ok(
            {"artifact": ref.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )
