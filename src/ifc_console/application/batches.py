"""Durable bounded orchestration for read-only IFC batch work."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import process_is_running
from ifc_console.automation.files import describe_source, source_matches
from ifc_console.core.batches import (
    TERMINAL_BATCH_STATES,
    BatchChildRecord,
    BatchRecord,
    BatchSpec,
    BatchState,
    QueryBatchOperation,
    ValidationBatchOperation,
)
from ifc_console.core.capabilities import Capability
from ifc_console.core.context import current_operation_context
from ifc_console.core.jobs import (
    TERMINAL_JOB_STATES,
    JobEvent,
    JobFailure,
    JobState,
    QueryJobSpec,
    ValidationJobSpec,
)
from ifc_console.core.results import ToolError
from ifc_console.core.revisions import RevisionRef

if TYPE_CHECKING:
    from ifc_console.app import AppCore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class BatchService:
    """Persist and supervise validation across many captured IFC inputs."""

    def __init__(
        self,
        core: AppCore,
        root: Path,
        artifacts: ArtifactService,
        *,
        retention: int = 200,
    ) -> None:
        self.core = core
        self.root = root
        self.records_dir = root / "records"
        self.cancel_dir = root / "cancel"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        self.retention = max(10, retention)
        self.instance_id = secrets.token_hex(8)
        self._records: dict[str, BatchRecord] = {}
        self._owners: dict[str, tuple[int, str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = RLock()
        self._closing = False
        self._load_records()
        self._prune_records()

    async def submit_validation(
        self,
        inputs: tuple[str | Path, ...],
        *,
        ids_paths: tuple[str | Path, ...] = (),
        express_rules: bool = False,
        max_issues: int = 200,
        concurrency: int = 2,
        failure_policy: str = "continue",
    ) -> BatchRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "submit_validation_batch", authority="caller", client=self.core.transport
            ):
                return await self.submit_validation(
                    inputs,
                    ids_paths=ids_paths,
                    express_rules=express_rules,
                    max_issues=max_issues,
                    concurrency=concurrency,
                    failure_policy=failure_policy,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT],
            authority=context.authority,
            action="submit validation batch",
        )
        if self._closing:
            raise ToolError(
                "BATCH_SERVICE_CLOSED",
                "the local batch service is shutting down.",
                "Create a new workbench before submitting more work.",
            )
        if not inputs:
            raise ToolError(
                "INVALID_INPUT",
                "a validation batch requires at least one IFC input.",
                "Pass one or more IFC file paths.",
            )
        resolved_inputs = tuple(
            self.core.resolve_attachment(str(path), kind="ifc") for path in inputs
        )
        resolved_ids = tuple(
            self.core.resolve_attachment(str(path), kind="ids") for path in ids_paths
        )
        try:
            sources = await asyncio.gather(
                *(asyncio.to_thread(describe_source, path) for path in resolved_inputs),
                *(asyncio.to_thread(describe_source, path) for path in resolved_ids),
            )
            input_sources = tuple(sources[: len(resolved_inputs)])
            ids_sources = tuple(sources[len(resolved_inputs) :])
            spec = BatchSpec(
                operation=ValidationBatchOperation(
                    ids_files=ids_sources,
                    express_rules=express_rules,
                    max_issues=max_issues,
                ),
                inputs=input_sources,
                concurrency=concurrency,
                failure_policy=failure_policy,
                context=context,
            )
        except (OSError, ValueError) as exc:
            raise ToolError(
                "INVALID_INPUT",
                f"could not capture the validation batch inputs: {exc}",
                "Use unique, readable IFC inputs and valid batch options.",
            ) from exc
        return self._create(spec)

    async def submit_query(
        self,
        inputs: tuple[str | Path, ...],
        *,
        query: str,
        fields: tuple[str, ...] = ("name", "storey", "type_name"),
        order_by: str = "class",
        output_format: str = "jsonl",
        limit: int = 100_000,
        concurrency: int = 2,
        failure_policy: str = "continue",
    ) -> BatchRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "submit_query_batch", authority="caller", client=self.core.transport
            ):
                return await self.submit_query(
                    inputs,
                    query=query,
                    fields=fields,
                    order_by=order_by,
                    output_format=output_format,
                    limit=limit,
                    concurrency=concurrency,
                    failure_policy=failure_policy,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT],
            authority=context.authority,
            action="submit query batch",
        )
        if self._closing:
            raise ToolError(
                "BATCH_SERVICE_CLOSED",
                "the local batch service is shutting down.",
                "Create a new workbench before submitting more work.",
            )
        if not inputs:
            raise ToolError(
                "INVALID_INPUT",
                "a query batch requires at least one IFC input.",
                "Pass one or more IFC file paths.",
            )
        resolved = tuple(
            self.core.resolve_attachment(str(path), kind="ifc") for path in inputs
        )
        try:
            sources = tuple(
                await asyncio.gather(
                    *(asyncio.to_thread(describe_source, path) for path in resolved)
                )
            )
            spec = BatchSpec(
                operation=QueryBatchOperation(
                    query=query,
                    fields=fields,
                    order_by=order_by,
                    output_format=output_format,
                    limit=limit,
                ),
                inputs=sources,
                concurrency=concurrency,
                failure_policy=failure_policy,
                context=context,
            )
        except (OSError, ValueError) as exc:
            raise ToolError(
                "INVALID_INPUT",
                f"could not capture the query batch inputs: {exc}",
                "Use unique, readable IFC inputs and valid query options.",
            ) from exc
        return self._create(spec)

    def submit_captured(self, spec: BatchSpec) -> BatchRecord:
        """Schedule a validated immutable batch captured by WorkflowService."""
        if self._closing:
            raise ToolError(
                "BATCH_SERVICE_CLOSED",
                "the local batch service is shutting down.",
                "Create a new workbench before submitting more work.",
            )
        return self._create(spec)

    def get(self, batch_id: str) -> BatchRecord:
        if re.fullmatch(r"batch-[0-9a-f]{16}", batch_id) is None:
            raise ToolError(
                "BATCH_NOT_FOUND",
                f"invalid batch ID {batch_id!r}.",
                "Batch IDs have the form batch-<16 lowercase hex characters>.",
            )
        loaded = self._read_record(batch_id)
        if loaded is not None:
            record, owner = loaded
            self._records[batch_id] = record
            self._owners[batch_id] = owner
        record = self._records.get(batch_id)
        if record is None:
            raise ToolError(
                "BATCH_NOT_FOUND",
                f"no batch named {batch_id!r}.",
                "List batches and use a returned batch_id.",
            )
        return record

    def list(self, *, limit: int = 100) -> list[BatchRecord]:
        for path in self.records_dir.glob("batch-*.json"):
            loaded = self._read_record(path.stem)
            if loaded is not None:
                record, owner = loaded
                self._records[record.batch_id] = record
                self._owners[record.batch_id] = owner
        records = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
        return records[: max(0, limit)]

    async def wait(self, batch_id: str, *, timeout: float | None = None) -> BatchRecord:
        started = time.monotonic()
        while True:
            record = self.get(batch_id)
            if record.state in TERMINAL_BATCH_STATES:
                return record
            task = self._tasks.get(batch_id)
            if task is not None:
                remaining = None
                if timeout is not None:
                    remaining = max(0.0, timeout - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise ToolError(
                        "BATCH_TIMEOUT",
                        f"timed out waiting for {batch_id}.",
                        "The batch is still running; inspect it or cancel it explicitly.",
                    ) from exc
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
                continue
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ToolError(
                    "BATCH_TIMEOUT",
                    f"timed out waiting for {batch_id}.",
                    "The batch may belong to another local process.",
                )
            await asyncio.sleep(0.1)

    async def watch(
        self, batch_id: str, *, poll_interval: float = 0.1
    ) -> AsyncIterator[BatchRecord]:
        last_update: datetime | None = None
        while True:
            record = self.get(batch_id)
            if record.updated_at != last_update:
                last_update = record.updated_at
                yield record
            if record.state in TERMINAL_BATCH_STATES:
                return
            await asyncio.sleep(max(0.01, poll_interval))

    async def cancel(self, batch_id: str) -> BatchRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "cancel_batch", authority="caller", client=self.core.transport
            ):
                return await self.cancel(batch_id)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_CANCEL], authority=context.authority, action="cancel batch"
        )
        record = self.get(batch_id)
        if record.state in TERMINAL_BATCH_STATES:
            return record
        self._replace(
            batch_id,
            cancel_requested=True,
            message="batch cancellation requested",
            event_type="cancel_requested",
        )
        self._cancel_path(batch_id).touch(exist_ok=True)
        task = self._tasks.get(batch_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.get(batch_id)

    async def resume(self, batch_id: str) -> BatchRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "resume_batch", authority="caller", client=self.core.transport
            ):
                return await self.resume(batch_id)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT], authority=context.authority, action="resume batch"
        )
        record = self.get(batch_id)
        if record.state not in {
            BatchState.PARTIAL,
            BatchState.FAILED,
            BatchState.CANCELLED,
            BatchState.INTERRUPTED,
        }:
            raise ToolError(
                "BATCH_NOT_RESUMABLE",
                f"{batch_id} is {record.state.value}, so it cannot be resumed.",
                "Resume a partial, failed, cancelled, or interrupted batch.",
            )
        operation = record.spec.operation
        extra_sources = (
            operation.ids_files
            if isinstance(operation, ValidationBatchOperation)
            else ()
        )
        captured = (*record.spec.inputs, *extra_sources)
        matches = await asyncio.gather(
            *(asyncio.to_thread(source_matches, source) for source in captured)
        )
        if not all(matches):
            changed = [
                Path(source.path).name
                for source, match in zip(captured, matches, strict=True)
                if not match
            ]
            raise ToolError(
                "BATCH_SOURCE_CHANGED",
                f"captured batch source identity changed: {', '.join(changed)}.",
                "Submit a new batch so its revision manifest matches the current files.",
            )
        children: list[BatchChildRecord] = []
        for child in record.children:
            reusable = child.state is JobState.SUCCEEDED and bool(child.artifacts)
            if reusable:
                try:
                    await asyncio.gather(
                        *(asyncio.to_thread(self.artifacts.verify, ref.artifact_id) for ref in child.artifacts)
                    )
                except ToolError:
                    reusable = False
            if reusable:
                children.append(child)
            else:
                children.append(
                    child.model_copy(
                        update={
                            "state": JobState.QUEUED,
                            "updated_at": _now(),
                            "job_id": None,
                            "artifacts": (),
                            "summary": {},
                            "failure": None,
                        }
                    )
                )
        self._owners[batch_id] = (os.getpid(), self.instance_id)
        with contextlib.suppress(OSError):
            self._cancel_path(batch_id).unlink()
        resumed = self._replace(
            batch_id,
            state=BatchState.QUEUED,
            progress=self._progress_for(tuple(children)),
            message="batch queued for resume",
            children=tuple(children),
            run_count=record.run_count + 1,
            aggregate_artifact=None,
            summary={},
            failure=None,
            cancel_requested=False,
            event_type="resumed",
        )
        self._schedule(batch_id)
        self._announce(resumed, "batch_resumed")
        return resumed

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for batch_id, task in tuple(self._tasks.items()):
            if task.done():
                continue
            with contextlib.suppress(OSError):
                self._cancel_path(batch_id).touch(exist_ok=True)
            task.cancel()

    async def aclose(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        self.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _create(self, spec: BatchSpec) -> BatchRecord:
        batch_id = f"batch-{secrets.token_hex(8)}"
        created = _now()
        message = f"{spec.operation.kind} batch queued with {len(spec.inputs)} input(s)"
        record = BatchRecord(
            batch_id=batch_id,
            state=BatchState.QUEUED,
            created_at=created,
            updated_at=created,
            progress=0,
            message=message,
            spec=spec,
            children=tuple(
                BatchChildRecord(
                    index=index,
                    source=source,
                    created_at=created,
                    updated_at=created,
                )
                for index, source in enumerate(spec.inputs)
            ),
            events=(JobEvent(ts=created, type="queued", progress=0, message=message),),
        )
        self._owners[batch_id] = (os.getpid(), self.instance_id)
        try:
            self._persist(record)
        except OSError as exc:
            self._owners.pop(batch_id, None)
            raise ToolError(
                "BATCH_STORE_FAILED",
                f"could not persist the batch before scheduling: {exc}",
                "Free local storage or repair the IFC-Console home, then retry.",
            ) from exc
        self._records[batch_id] = record
        self._prune_records()
        self._schedule(batch_id)
        self._announce(record, "batch_submitted")
        return record

    def _schedule(self, batch_id: str) -> None:
        task = asyncio.create_task(self._run(batch_id), name=batch_id)
        self._tasks[batch_id] = task

    async def _run(self, batch_id: str) -> None:
        active: dict[asyncio.Task[JobState], int] = {}
        try:
            self._replace(
                batch_id,
                state=BatchState.RUNNING,
                message=f"{self._records[batch_id].spec.operation.kind} batch running",
                event_type="running",
            )
            record = self._records[batch_id]
            pending = [
                child.index for child in record.children if child.state is not JobState.SUCCEEDED
            ]
            fail_fast = False
            while pending or active:
                if self._cancel_requested(batch_id):
                    await self._cancel_active(batch_id, active)
                    self._cancel_queued(batch_id, pending, "batch cancelled")
                    pending.clear()
                    break
                while pending and len(active) < record.spec.concurrency and not fail_fast:
                    index = pending.pop(0)
                    task = asyncio.create_task(self._run_child(batch_id, index))
                    active[task] = index
                if not active:
                    break
                done, _waiting = await asyncio.wait(
                    active, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    active.pop(task)
                    state = task.result()
                    if state is JobState.FAILED and record.spec.failure_policy == "fail_fast":
                        fail_fast = True
                if fail_fast:
                    await self._cancel_active(batch_id, active)
                    self._cancel_queued(batch_id, pending, "skipped after fail-fast failure")
                    pending.clear()
            await self._finalize(batch_id)
        except asyncio.CancelledError:
            await self._cancel_active(batch_id, active)
            current = self._records.get(batch_id)
            if current is not None and current.state not in TERMINAL_BATCH_STATES:
                pending = [
                    child.index
                    for child in current.children
                    if child.state not in TERMINAL_JOB_STATES
                ]
                self._cancel_queued(batch_id, pending, "batch cancelled during shutdown")
                await self._finalize(batch_id, force_cancelled=True)
            raise
        except Exception as exc:
            await self._cancel_active(batch_id, active)
            current = self._records[batch_id]
            self._replace(
                batch_id,
                state=BatchState.FAILED,
                progress=current.progress,
                message="validation batch supervisor failed",
                failure=JobFailure(
                    code="BATCH_SUPERVISOR_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="Inspect the durable child jobs, then resume the batch.",
                ),
                event_type="failed",
            )
        finally:
            self._tasks.pop(batch_id, None)
            with contextlib.suppress(OSError):
                self._cancel_path(batch_id).unlink()

    async def _run_child(self, batch_id: str, index: int) -> JobState:
        record = self._records[batch_id]
        child = record.children[index]
        if not await asyncio.to_thread(source_matches, child.source):
            failure = JobFailure(
                code="SOURCE_CHANGED",
                message=f"captured IFC source changed before execution: {child.source.path}",
                hint="Submit a new batch against the current source revision.",
            )
            self._replace_child(
                batch_id,
                index,
                state=JobState.FAILED,
                attempts=child.attempts + 1,
                last_run=record.run_count,
                failure=failure,
            )
            return JobState.FAILED
        operation = record.spec.operation
        revision = RevisionRef(
            workspace_id=self.core.workspace_id,
            model_id=f"batch-model-{child.source.sha256[:16]}",
            revision_id=f"{child.source.sha256}:0",
            content_sha256=child.source.sha256,
        )
        if isinstance(operation, ValidationBatchOperation):
            spec = ValidationJobSpec(
                revision=revision,
                model=child.source,
                ids_files=operation.ids_files,
                express_rules=operation.express_rules,
                max_issues=operation.max_issues,
                context=record.spec.context,
            )
            job = self.core.jobs.submit_captured_validation(spec)
        else:
            spec = QueryJobSpec(
                revision=revision,
                model=child.source,
                query=operation.query,
                fields=operation.fields,
                order_by=operation.order_by,
                output_format=operation.output_format,
                limit=operation.limit,
                context=record.spec.context,
            )
            job = self.core.jobs.submit_captured_query(spec)
        self._replace_child(
            batch_id,
            index,
            state=JobState.RUNNING,
            attempts=child.attempts + 1,
            last_run=record.run_count,
            job_id=job.job_id,
            failure=None,
        )
        job = await self.core.jobs.wait(job.job_id)
        if job.state is JobState.SUCCEEDED:
            try:
                verified = tuple(
                    await asyncio.gather(
                        *(asyncio.to_thread(self.artifacts.verify, ref.artifact_id) for ref in job.artifacts)
                    )
                )
            except ToolError as exc:
                job = job.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "failure": JobFailure(code=exc.code, message=exc.message, hint=exc.hint),
                    },
                )
            else:
                self._replace_child(
                    batch_id,
                    index,
                    state=JobState.SUCCEEDED,
                    artifacts=verified,
                    summary=job.summary,
                    failure=None,
                )
                return JobState.SUCCEEDED
        self._replace_child(
            batch_id,
            index,
            state=job.state,
            artifacts=job.artifacts,
            summary=job.summary,
            failure=job.failure,
        )
        return job.state

    async def _cancel_active(
        self, batch_id: str, active: dict[asyncio.Task[JobState], int]
    ) -> None:
        current = self._records[batch_id]
        running_ids = [
            child.job_id
            for child in current.children
            if child.state is JobState.RUNNING and child.job_id is not None
        ]
        await asyncio.gather(
            *(self.core.jobs.cancel(job_id) for job_id in running_ids), return_exceptions=True
        )
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        active.clear()

    def _cancel_queued(self, batch_id: str, indexes: list[int], message: str) -> None:
        for index in indexes:
            child = self._records[batch_id].children[index]
            if child.state not in TERMINAL_JOB_STATES:
                self._replace_child(
                    batch_id,
                    index,
                    state=JobState.CANCELLED,
                    failure=JobFailure(
                        code="BATCH_CANCELLED",
                        message=message,
                        hint="Resume the batch to process this captured input.",
                    ),
                )

    async def _finalize(self, batch_id: str, *, force_cancelled: bool = False) -> None:
        record = self._records[batch_id]
        children = record.children
        counts = {state.value: 0 for state in JobState}
        for child in children:
            counts[child.state.value] += 1
        succeeded = counts[JobState.SUCCEEDED.value]
        failed = counts[JobState.FAILED.value]
        cancelled = counts[JobState.CANCELLED.value]
        if force_cancelled or record.cancel_requested or self._cancel_path(batch_id).exists():
            state = BatchState.CANCELLED
        elif succeeded == len(children):
            state = BatchState.SUCCEEDED
        elif succeeded:
            state = BatchState.PARTIAL
        elif failed:
            state = BatchState.FAILED
        else:
            state = BatchState.CANCELLED
        summary: dict[str, object] = {
            "input_count": len(children),
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
            "passed": succeeded == len(children)
            and (
                record.spec.operation.kind == "query"
                or all(bool(child.summary.get("passed")) for child in children)
            ),
            "reused": sum(
                1
                for child in children
                if child.state is JobState.SUCCEEDED
                and child.last_run is not None
                and child.last_run < record.run_count
            ),
        }
        if record.spec.operation.kind == "query":
            summary.update(
                {
                    "matched": sum(int(child.summary.get("matched", 0)) for child in children),
                    "row_count": sum(
                        int(child.summary.get("row_count", 0)) for child in children
                    ),
                    "truncated_inputs": sum(
                        bool(child.summary.get("truncated")) for child in children
                    ),
                }
            )
        artifact = self._write_manifest(record, state, summary)
        message = {
            BatchState.SUCCEEDED: f"{record.spec.operation.kind} batch completed",
            BatchState.PARTIAL: (
                f"{record.spec.operation.kind} batch completed with operational failures"
            ),
            BatchState.FAILED: f"{record.spec.operation.kind} batch failed",
            BatchState.CANCELLED: f"{record.spec.operation.kind} batch cancelled",
        }[state]
        final = self._replace(
            batch_id,
            state=state,
            progress=100,
            message=message,
            aggregate_artifact=artifact,
            summary=summary,
            failure=(
                JobFailure(
                    code="BATCH_CHILD_FAILED",
                    message=f"{failed} child operation(s) failed.",
                    hint="Inspect child failures and resume when the cause is resolved.",
                )
                if failed
                else None
            ),
            event_type=state.value,
        )
        self._announce(final, "batch_state")

    def _write_manifest(
        self, record: BatchRecord, state: BatchState, summary: dict[str, object]
    ) -> Any:
        children = [
            {
                "index": child.index,
                "source": child.source.model_dump(mode="json"),
                "state": child.state.value,
                "attempts": child.attempts,
                "last_run": child.last_run,
                "job_id": child.job_id,
                "artifacts": [ref.model_dump(mode="json") for ref in child.artifacts],
                "summary": child.summary,
                "failure": child.failure.model_dump(mode="json") if child.failure else None,
            }
            for child in record.children
        ]
        references = tuple(
            ref.artifact_id for child in record.children for ref in child.artifacts
        )
        payload = {
            "version": "1",
            "batch_id": record.batch_id,
            "state": state.value,
            "created_at": record.created_at.isoformat(),
            "completed_at": _now().isoformat(),
            "run_count": record.run_count,
            "spec": record.spec.model_dump(mode="json"),
            "summary": summary,
            "children": children,
        }
        return self.artifacts.put_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            name=f"{record.batch_id}-manifest.json",
            kind="batch-manifest",
            media_type="application/json",
            producer=record.batch_id,
            metadata={"state": state.value, **summary},
            references=references,
        )

    def _replace_child(self, batch_id: str, index: int, **updates: Any) -> BatchRecord:
        with self._lock:
            current = self._records[batch_id]
            children = list(current.children)
            updates["updated_at"] = _now()
            children[index] = children[index].model_copy(update=updates)
        return self._replace(
            batch_id,
            children=tuple(children),
            progress=self._progress_for(tuple(children)),
            message=f"processed {sum(child.state in TERMINAL_JOB_STATES for child in children)} of {len(children)} inputs",
            event_type="child_updated",
        )

    def _replace(self, batch_id: str, **updates: Any) -> BatchRecord:
        event_type = str(updates.pop("event_type", "updated"))
        with self._lock:
            current = self._records[batch_id]
            updated_at = _now()
            event = JobEvent(
                ts=updated_at,
                type=event_type,
                progress=int(updates.get("progress", current.progress)),
                message=str(updates.get("message", current.message)),
            )
            updates["updated_at"] = updated_at
            updates["events"] = (*current.events[-99:], event)
            record = current.model_copy(update=updates)
            self._persist(record)
            self._records[batch_id] = record
        self.core.events.emit(
            "batch_updated",
            batch_id=batch_id,
            state=record.state.value,
            progress=record.progress,
            message=record.message,
        )
        return record

    def _announce(self, record: BatchRecord, event: str) -> None:
        self.core.audit.record(
            event,
            batch_id=record.batch_id,
            kind=record.spec.operation.kind,
            state=record.state.value,
            progress=record.progress,
            input_count=len(record.children),
            concurrency=record.spec.concurrency,
            failure_policy=record.spec.failure_policy,
            aggregate_artifact=(
                record.aggregate_artifact.artifact_id if record.aggregate_artifact else None
            ),
            failure=record.failure.code if record.failure else None,
        )

    def _persist(self, record: BatchRecord) -> None:
        owner_pid, owner_id = self._owners.get(
            record.batch_id, (os.getpid(), self.instance_id)
        )
        self._write_json(
            self.records_dir / f"{record.batch_id}.json",
            {
                "record": record.model_dump(mode="json"),
                "owner_pid": owner_pid,
                "owner_id": owner_id,
            },
        )

    def _load_records(self) -> None:
        for path in self.records_dir.glob("batch-*.json"):
            loaded = self._read_record(path.stem)
            if loaded is None:
                continue
            record, owner = loaded
            self._records[record.batch_id] = record
            self._owners[record.batch_id] = owner
        for batch_id, record in tuple(self._records.items()):
            owner_pid, _owner_id = self._owners[batch_id]
            if record.state in TERMINAL_BATCH_STATES or process_is_running(owner_pid):
                continue
            children = tuple(
                child.model_copy(
                    update={
                        "state": JobState.FAILED,
                        "updated_at": _now(),
                        "failure": JobFailure(
                            code="BATCH_INTERRUPTED",
                            message="the batch supervisor exited before this child completed",
                            hint="Resume the batch to retry this captured input.",
                        ),
                    }
                )
                if child.state not in TERMINAL_JOB_STATES
                else child
                for child in record.children
            )
            recovered = self._replace(
                batch_id,
                state=BatchState.INTERRUPTED,
                children=children,
                progress=self._progress_for(children),
                message="batch owner exited before completion",
                failure=JobFailure(
                    code="BATCH_INTERRUPTED",
                    message="the process supervising this batch is no longer running",
                    hint="Verify the captured sources, then resume the batch.",
                ),
                event_type="interrupted",
            )
            self._announce(recovered, "batch_recovered")

    def _read_record(self, batch_id: str) -> tuple[BatchRecord, tuple[int, str]] | None:
        try:
            with self._lock:
                payload = json.loads(
                    (self.records_dir / f"{batch_id}.json").read_text(encoding="utf-8")
                )
            return BatchRecord.model_validate(payload["record"]), (
                int(payload.get("owner_pid", 0)),
                str(payload.get("owner_id", "")),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _prune_records(self) -> None:
        records = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
        kept = 0
        for record in records:
            if record.state not in TERMINAL_BATCH_STATES:
                continue
            kept += 1
            if kept <= self.retention:
                continue
            self._records.pop(record.batch_id, None)
            self._owners.pop(record.batch_id, None)
            with contextlib.suppress(OSError):
                (self.records_dir / f"{record.batch_id}.json").unlink()

    def _cancel_requested(self, batch_id: str) -> bool:
        return self._records[batch_id].cancel_requested or self._cancel_path(batch_id).exists()

    def _cancel_path(self, batch_id: str) -> Path:
        return self.cancel_dir / batch_id

    @staticmethod
    def _progress_for(children: tuple[BatchChildRecord, ...]) -> int:
        if not children:
            return 0
        completed = sum(child.state in TERMINAL_JOB_STATES for child in children)
        return min(99, int(completed * 100 / len(children)))

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + 2
            while True:
                try:
                    os.replace(temp, path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        finally:
            if temp.exists():
                temp.unlink()
