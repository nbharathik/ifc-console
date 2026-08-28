"""Durable local jobs backed by supervised worker processes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import process_is_running
from ifc_console.automation.files import describe_source
from ifc_console.checks import render
from ifc_console.core.capabilities import Capability
from ifc_console.core.context import bind_operation_context, current_operation_context
from ifc_console.core.jobs import (
    TERMINAL_JOB_STATES,
    CommitJobSpec,
    JobEvent,
    JobFailure,
    JobRecord,
    JobState,
    QueryJobSpec,
    RestoreJobSpec,
    ValidationJobSpec,
)
from ifc_console.core.results import ToolError
from ifc_console.core.revisions import RevisionRef
from ifc_console.core.transaction_journal import TransactionKind, TransactionPhase
from ifc_console.sandbox.client import _child_env, worker_command
from ifc_console.sandbox.limits import ProcessJail, isolated_process_kwargs
from ifc_console.sandbox.policy import SandboxPolicy

if TYPE_CHECKING:
    from asyncio.subprocess import Process

    from ifc_console.app import AppCore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _JobCancelled(Exception):
    pass


class JobService:
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
        self.work_dir = root / "work"
        self.cancel_dir = root / "cancel"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        self.retention = max(10, retention)
        self.instance_id = secrets.token_hex(8)
        self._records: dict[str, JobRecord] = {}
        self._owners: dict[str, tuple[int, str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, Process] = {}
        self._jails: dict[str, ProcessJail] = {}
        self._lock = RLock()
        self._closing = False
        self._load_records()
        self._prune_records()

    async def submit_validation(
        self,
        *,
        model: str | None = None,
        ids_paths: tuple[str | Path, ...] = (),
        express_rules: bool = False,
        max_issues: int = 200,
        expected_revision: str | None = None,
    ) -> JobRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "submit_validation_job", authority="caller", client=self.core.transport
            ):
                return await self.submit_validation(
                    model=model,
                    ids_paths=ids_paths,
                    express_rules=express_rules,
                    max_issues=max_issues,
                    expected_revision=expected_revision,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT],
            authority=context.authority,
            action="submit validation job",
        )
        session = self.core.resolve_session(model)
        if session.dirty:
            raise ToolError(
                "UNSAVED_CHANGES",
                "validation jobs require a clean model revision.",
                "Save the model first. Dirty-model snapshots arrive with ChangeSets.",
            )
        if session.path is None or session.model_id is None or session.fingerprint is None:
            raise ToolError(
                "NO_MODEL_LOADED",
                "the selected model has no durable source revision.",
                "Open a saved IFC model before submitting a validation job.",
            )
        captured_revision = f"{session.fingerprint}:{session.revision}"
        if expected_revision is not None and expected_revision != captured_revision:
            raise ToolError(
                "REVISION_CONFLICT",
                f"expected revision {expected_revision}, current revision is {captured_revision}.",
                "Refresh workspace context and submit against the current revision.",
            )
        resolved_ids = tuple(
            self.core.resolve_attachment(str(path), kind="ids") for path in ids_paths
        )
        sources = await asyncio.gather(
            asyncio.to_thread(describe_source, session.path),
            *(asyncio.to_thread(describe_source, path) for path in resolved_ids),
        )
        current_revision = f"{session.fingerprint}:{session.revision}"
        if session.dirty or current_revision != captured_revision:
            raise ToolError(
                "REVISION_CONFLICT",
                "the model revision changed while the job inputs were being prepared.",
                "Refresh workspace context and submit the validation job again.",
            )
        revision = RevisionRef(
            workspace_id=self.core.workspace_id,
            model_id=session.model_id,
            revision_id=captured_revision,
            content_sha256=sources[0].sha256,
        )
        spec = ValidationJobSpec(
            revision=revision,
            model=sources[0],
            ids_files=tuple(sources[1:]),
            express_rules=express_rules,
            max_issues=max_issues,
            context=context,
        )
        return self._enqueue(
            spec,
            message="validation job queued",
            runner=self._run_validation,
        )

    def submit_captured_validation(self, spec: ValidationJobSpec) -> JobRecord:
        """Enqueue an already captured validation spec for application orchestration.

        BatchService is responsible for the caller capability check and source
        capture. The normal worker still independently verifies every source.
        """
        if self._closing:
            raise ToolError(
                "JOB_SERVICE_CLOSED",
                "the local job service is shutting down.",
                "Create a new workbench before submitting more work.",
            )
        return self._enqueue(
            spec,
            message="validation job queued by batch",
            runner=self._run_validation,
        )

    def submit_captured_query(self, spec: QueryJobSpec) -> JobRecord:
        """Enqueue an immutable query spec captured by BatchService."""
        if self._closing:
            raise ToolError(
                "JOB_SERVICE_CLOSED",
                "the local job service is shutting down.",
                "Create a new workbench before submitting more work.",
            )
        return self._enqueue(
            spec,
            message="query job queued by batch",
            runner=self._run_query,
        )

    async def submit_commit(self, change_set_id: str, *, approval_id: str) -> JobRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "submit_commit_job", authority="caller", client=self.core.transport
            ):
                return await self.submit_commit(change_set_id, approval_id=approval_id)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT, Capability.MODEL_COMMIT],
            authority=context.authority,
            action="submit commit job",
        )
        change = self.core.transactions.get_change_set(change_set_id)
        approval = self.core.transactions.get_approval(approval_id)
        if approval.approval.change_set_id != change.change_set_id:
            raise ToolError(
                "APPROVAL_MISMATCH",
                "the approval belongs to a different ChangeSet.",
                "Approve this ChangeSet explicitly and pass the returned approval_id.",
            )
        if approval.approval.revision != change.change_set.revision:
            raise ToolError(
                "APPROVAL_MISMATCH",
                "the approval is bound to a different model revision.",
                "Create a new approval for this ChangeSet.",
            )
        spec = CommitJobSpec(
            revision=change.change_set.revision,
            change_set_id=change.change_set_id,
            approval_id=approval.approval_id,
            context=context,
        )
        return self._enqueue(spec, message="commit job queued", runner=self._run_transaction)

    async def submit_restore(self, commit_id: str, *, confirm: bool = False) -> JobRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "submit_restore_job", authority="caller", client=self.core.transport
            ):
                return await self.submit_restore(commit_id, confirm=confirm)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT, Capability.MODEL_RESTORE],
            authority=context.authority,
            action="submit restore job",
        )
        if not confirm:
            raise ToolError(
                "APPROVAL_REQUIRED",
                "restore requires explicit caller confirmation.",
                "Pass confirm=true only after reviewing the commit receipt.",
            )
        commit = self.core.transactions.get_commit(commit_id)
        spec = RestoreJobSpec(
            revision=commit.result.revision_after,
            commit_id=commit.commit_id,
            confirmed=True,
            context=context,
        )
        return self._enqueue(spec, message="restore job queued", runner=self._run_transaction)

    def _enqueue(self, spec: Any, *, message: str, runner: Any) -> JobRecord:
        job_id = f"job-{secrets.token_hex(8)}"
        created = _now()
        event = JobEvent(
            ts=created,
            type="queued",
            progress=0,
            message=message,
        )
        record = JobRecord(
            job_id=job_id,
            kind=spec.kind,
            state=JobState.QUEUED,
            created_at=created,
            updated_at=created,
            progress=0,
            message=event.message,
            spec=spec,
            events=(event,),
        )
        self._records[job_id] = record
        self._owners[job_id] = (os.getpid(), self.instance_id)
        self._persist(record)
        self._prune_records()
        self._announce(record, "job_submitted")
        task = asyncio.create_task(runner(job_id), name=job_id)
        self._tasks[job_id] = task
        return record

    def get(self, job_id: str) -> JobRecord:
        if re.fullmatch(r"job-[0-9a-f]{16}", job_id) is None:
            raise ToolError(
                "JOB_NOT_FOUND",
                f"invalid job ID {job_id!r}.",
                "Job IDs have the form job-<16 lowercase hex characters>.",
            )
        loaded = self._read_record(job_id)
        if loaded is not None:
            record, owner = loaded
            self._records[job_id] = record
            self._owners[job_id] = owner
        record = self._records.get(job_id)
        if record is None:
            raise ToolError(
                "JOB_NOT_FOUND",
                f"no job named {job_id!r}.",
                "List jobs and use a returned job_id.",
            )
        return record

    def list(self, *, limit: int = 100) -> list[JobRecord]:
        for path in self.records_dir.glob("job-*.json"):
            loaded = self._read_record(path.stem)
            if loaded is not None:
                record, owner = loaded
                self._records[record.job_id] = record
                self._owners[record.job_id] = owner
        records = sorted(self._records.values(), key=lambda record: record.created_at, reverse=True)
        return records[: max(0, limit)]

    async def wait(self, job_id: str, *, timeout: float | None = None) -> JobRecord:
        started = time.monotonic()
        while True:
            record = self.get(job_id)
            if record.state in TERMINAL_JOB_STATES:
                return record
            task = self._tasks.get(job_id)
            if task is not None:
                remaining = None
                if timeout is not None:
                    remaining = max(0.0, timeout - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise ToolError(
                        "JOB_TIMEOUT",
                        f"timed out waiting for {job_id}.",
                        "The job is still running; inspect it or cancel it explicitly.",
                    ) from exc
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
                continue
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ToolError(
                    "JOB_TIMEOUT",
                    f"timed out waiting for {job_id}.",
                    "The job may belong to another local process.",
                )
            await asyncio.sleep(0.1)

    async def watch(self, job_id: str, *, poll_interval: float = 0.1) -> AsyncIterator[JobRecord]:
        last_update: datetime | None = None
        while True:
            record = self.get(job_id)
            if record.updated_at != last_update:
                last_update = record.updated_at
                yield record
            if record.state in TERMINAL_JOB_STATES:
                return
            await asyncio.sleep(max(0.01, poll_interval))

    async def cancel(self, job_id: str) -> JobRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "cancel_job", authority="caller", client=self.core.transport
            ):
                return await self.cancel(job_id)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_CANCEL], authority=context.authority, action="cancel job"
        )
        record = self.get(job_id)
        if record.state in TERMINAL_JOB_STATES:
            return record
        if not record.cancellable:
            raise ToolError(
                "JOB_NOT_CANCELLABLE",
                f"{job_id} passed its cancellation boundary at {record.phase}.",
                "Wait for receipt persistence or rollback to finish, then inspect the job.",
            )
        self._replace(job_id, cancel_requested=True, message="cancellation requested")
        task = self._tasks.get(job_id)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        else:
            process = self._processes.get(job_id)
            if process is not None:
                await self._stop_process(process)
            owner_pid, _owner_id = self._owners.get(job_id, (0, ""))
            if self._pid_exists(owner_pid):
                self._cancel_path(job_id).touch(exist_ok=True)
                return self.get(job_id)
        record = self.get(job_id)
        if record.state not in TERMINAL_JOB_STATES:
            record = self._transition(
                job_id,
                JobState.CANCELLED,
                progress=record.progress,
                message=f"{record.kind} job cancelled",
            )
        return record

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for process in self._processes.values():
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
        for job_id, task in list(self._tasks.items()):
            if task.done():
                continue
            task.cancel()
            record = self._records.get(job_id)
            if (
                record is not None
                and record.state not in TERMINAL_JOB_STATES
                and record.kind in {"validation", "query"}
            ):
                self._transition(
                    job_id,
                    JobState.CANCELLED,
                    progress=record.progress,
                    message="job cancelled during shutdown",
                )

    async def aclose(self) -> None:
        """Cancel owned work and await rollback or worker termination."""
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        self.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for jail in self._jails.values():
            jail.close()

    async def _run_validation(self, job_id: str) -> None:
        record = self._records[job_id]
        context = record.spec.context or self.core.operation_service.context(
            operation="validation_job", authority="worker", job_id=job_id
        )
        context = context.model_copy(
            update={"authority": "worker", "operation": "validation_job", "job_id": job_id}
        )
        with bind_operation_context(context):
            await self._run_validation_bound(job_id)

    async def _run_query(self, job_id: str) -> None:
        record = self._records[job_id]
        context = record.spec.context or self.core.operation_service.context(
            operation="query_job", authority="worker", job_id=job_id
        )
        context = context.model_copy(
            update={"authority": "worker", "operation": "query_job", "job_id": job_id}
        )
        with bind_operation_context(context):
            await self._run_query_bound(job_id)

    async def _run_transaction(self, job_id: str) -> None:
        record = self._records[job_id]
        operation = f"{record.kind}_job"
        context = record.spec.context or self.core.operation_service.context(
            operation=operation, authority="worker", job_id=job_id
        )
        context = context.model_copy(
            update={"authority": "worker", "operation": operation, "job_id": job_id}
        )
        with bind_operation_context(context):
            await self._run_transaction_bound(job_id)

    async def _run_transaction_bound(self, job_id: str) -> None:
        record = self._transition(
            job_id,
            JobState.RUNNING,
            progress=1,
            message=f"preparing {self._records[job_id].kind} transaction",
        )
        self._replace(job_id, phase="preparing", cancellable=True, event_type="phase")

        phase_progress = {
            TransactionPhase.PREPARED: 30,
            TransactionPhase.CANDIDATE_VERIFIED: 45,
            TransactionPhase.BACKUP_VERIFIED: 60,
            TransactionPhase.COMMIT_POINT: 70,
            TransactionPhase.TARGET_VERIFIED: 82,
            TransactionPhase.RECEIPT_PREPARED: 92,
            TransactionPhase.RECEIPT_PERSISTED: 99,
            TransactionPhase.ROLLBACK_STARTED: 90,
            TransactionPhase.ROLLED_BACK: 99,
            TransactionPhase.ABORTED: 99,
            TransactionPhase.RECOVERY_FAILED: 99,
        }

        def progress(journal: Any) -> None:
            current = self._records[job_id]
            if current.state in TERMINAL_JOB_STATES:
                return
            self._replace(
                job_id,
                progress=max(current.progress, phase_progress[journal.phase]),
                message=f"transaction phase: {journal.phase.value}",
                phase=journal.phase.value,
                cancellable=journal.cancellable,
                transaction_id=journal.transaction_id,
                event_type="phase",
            )

        try:
            spec = record.spec
            if isinstance(spec, CommitJobSpec):
                result = await self._await_transaction(
                    job_id,
                    self.core.transactions.commit(
                        spec.change_set_id,
                        approval_id=spec.approval_id,
                        _job_id=job_id,
                        _progress=progress,
                        _cancel_check=lambda: self._cancel_path(job_id).exists(),
                    ),
                )
                summary: dict[str, object] = {
                    "commit_id": result.commit_id,
                    "change_set_id": result.result.change_set_id,
                    "committed_sha256": result.result.committed_sha256,
                }
                artifacts = (result.artifact,)
            elif isinstance(spec, RestoreJobSpec):
                result = await self._await_transaction(
                    job_id,
                    self.core.transactions.restore(
                        spec.commit_id,
                        confirm=spec.confirmed,
                        _job_id=job_id,
                        _progress=progress,
                        _cancel_check=lambda: self._cancel_path(job_id).exists(),
                    ),
                )
                summary = {
                    "restore_id": result.restore_id,
                    "commit_id": result.result.commit_id,
                    "restored_sha256": result.result.restored_sha256,
                }
                artifacts = (result.artifact,)
            else:
                raise ToolError(
                    "JOB_SPEC_INVALID",
                    f"unsupported transaction job kind {record.kind!r}.",
                    "Submit a commit or restore job.",
                )
            current = self._records[job_id]
            self._replace(
                job_id,
                phase=TransactionPhase.RECEIPT_PERSISTED.value,
                cancellable=False,
                transaction_id=current.transaction_id,
                event_type="finalized",
            )
            self._transition(
                job_id,
                JobState.SUCCEEDED,
                progress=100,
                message=f"{record.kind} job completed",
                artifacts=artifacts,
                summary=summary,
            )
        except asyncio.CancelledError:
            current = self._records[job_id]
            if current.state not in TERMINAL_JOB_STATES:
                self._transition(
                    job_id,
                    JobState.CANCELLED,
                    progress=current.progress,
                    message=f"{record.kind} job cancelled before commit completion",
                    cancel_requested=True,
                )
            raise
        except ToolError as exc:
            current = self._records[job_id]
            self._transition(
                job_id,
                JobState.FAILED,
                progress=current.progress,
                message=f"{record.kind} job failed",
                failure=JobFailure(code=exc.code, message=exc.message, hint=exc.hint),
            )
        except Exception as exc:
            current = self._records[job_id]
            self._transition(
                job_id,
                JobState.FAILED,
                progress=current.progress,
                message=f"{record.kind} job failed",
                failure=JobFailure(
                    code="JOB_WORKER_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="Inspect the transaction journal and retry only when the target is safe.",
                ),
            )
        finally:
            self._tasks.pop(job_id, None)
            with contextlib.suppress(OSError):
                self._cancel_path(job_id).unlink()

    async def _await_transaction(self, job_id: str, operation: Any) -> Any:
        task = asyncio.create_task(operation)
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=0.05)
                if done:
                    return task.result()
                if self._cancel_path(job_id).exists():
                    current = self._records[job_id]
                    if current.cancellable:
                        task.cancel()
                        return await task
                    with contextlib.suppress(OSError):
                        self._cancel_path(job_id).unlink()
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _run_validation_bound(self, job_id: str) -> None:
        record = self._transition(
            job_id,
            JobState.RUNNING,
            progress=1,
            message="starting validation worker",
        )
        work = self.work_dir / job_id
        input_path = work / "input.json"
        output_path = work / "report.json"
        worker_failure: JobFailure | None = None
        process: Process | None = None
        try:
            work.mkdir(parents=True, exist_ok=True)
            payload = {
                "spec": record.spec.model_dump(mode="json"),
                "output_path": str(output_path),
                "policy": SandboxPolicy.build(
                    read_dirs=[
                        Path(source.path).parent
                        for source in (record.spec.model, *record.spec.ids_files)
                    ],
                    scratch_dir=work,
                    deny_dirs=[self.core.store.home],
                    memory_mb=self.core.settings.sandbox.memory_mb,
                ).to_dict(),
            }
            self._write_json(input_path, payload)
            process = await asyncio.create_subprocess_exec(
                *self._worker_command(input_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work,
                env=_child_env(work),
                **isolated_process_kwargs(),
            )
            self._processes[job_id] = process
            jail = ProcessJail(self.core.settings.sandbox.memory_mb)
            jail.attach(process.pid)
            self._jails[job_id] = jail
            self._replace(
                job_id,
                worker={
                    "pid": process.pid,
                    "controls": list(jail.controls),
                    "environment": "minimal",
                    "network": "blocked",
                    "correlation_id": (
                        record.spec.context.correlation_id if record.spec.context else None
                    ),
                },
                event_type="worker_started",
            )
            try:
                return_code, stderr, worker_failure = await asyncio.wait_for(
                    self._consume_worker(job_id, process),
                    timeout=float(self.core.settings.automation.validation_timeout_s),
                )
            except asyncio.TimeoutError as exc:
                await self._stop_process(process)
                raise ToolError(
                    "JOB_TIMEOUT",
                    "the validation worker exceeded its configured timeout.",
                    "Raise automation.validation_timeout_s or inspect the model and rules.",
                ) from exc
            if return_code != 0:
                if worker_failure is None:
                    worker_failure = JobFailure(
                        code="JOB_WORKER_FAILED",
                        message=stderr[-1000:] or f"worker exited with code {return_code}",
                        hint="Inspect the job input and retry.",
                    )
                raise ToolError(
                    worker_failure.code,
                    worker_failure.message,
                    worker_failure.hint,
                )
            report_text = output_path.read_text(encoding="utf-8")
            report = json.loads(report_text)
            base = Path(record.spec.model.path).stem
            json_ref = self.artifacts.put_text(
                report_text,
                name=f"{base}-validation.json",
                kind="validation-report",
                media_type="application/json",
                producer=job_id,
                revision=record.spec.revision,
                metadata={"format": "json", "passed": bool(report["passed"])},
            )
            sarif_ref = self.artifacts.put_text(
                render(report, "sarif") + "\n",
                name=f"{base}-validation.sarif",
                kind="validation-report",
                media_type="application/sarif+json",
                producer=job_id,
                revision=record.spec.revision,
                metadata={"format": "sarif", "passed": bool(report["passed"])},
            )
            self._transition(
                job_id,
                JobState.SUCCEEDED,
                progress=100,
                message="validation job completed",
                artifacts=(json_ref, sarif_ref),
                summary={
                    "passed": bool(report["passed"]),
                    "schema": report.get("schema"),
                    "issue_count": report["checks"]["schema"]["issue_count"],
                },
            )
        except asyncio.CancelledError:
            if process is not None:
                await self._stop_process(process)
            current = self._records[job_id]
            if current.state not in TERMINAL_JOB_STATES:
                self._transition(
                    job_id,
                    JobState.CANCELLED,
                    progress=current.progress,
                    message="validation job cancelled",
                )
            raise
        except _JobCancelled:
            if process is not None:
                await self._stop_process(process)
            current = self._records[job_id]
            if current.state not in TERMINAL_JOB_STATES:
                self._transition(
                    job_id,
                    JobState.CANCELLED,
                    progress=current.progress,
                    message="validation job cancelled",
                    cancel_requested=True,
                )
        except ToolError as exc:
            self._transition(
                job_id,
                JobState.FAILED,
                progress=self._records[job_id].progress,
                message="validation job failed",
                failure=JobFailure(code=exc.code, message=exc.message, hint=exc.hint),
            )
        except Exception as exc:
            self._transition(
                job_id,
                JobState.FAILED,
                progress=self._records[job_id].progress,
                message="validation job failed",
                failure=JobFailure(
                    code="JOB_WORKER_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="Inspect the job record and retry.",
                ),
            )
        finally:
            self._processes.pop(job_id, None)
            jail = self._jails.pop(job_id, None)
            if jail is not None:
                jail.close()
            self._tasks.pop(job_id, None)
            shutil.rmtree(work, ignore_errors=True)
            with contextlib.suppress(OSError):
                self._cancel_path(job_id).unlink()

    async def _run_query_bound(self, job_id: str) -> None:
        record = self._transition(
            job_id,
            JobState.RUNNING,
            progress=1,
            message="starting query worker",
        )
        work = self.work_dir / job_id
        input_path = work / "input.json"
        spec = record.spec
        assert isinstance(spec, QueryJobSpec)
        extension = spec.output_format
        output_path = work / f"result.{extension}"
        metadata_path = work / "metadata.json"
        worker_failure: JobFailure | None = None
        process: Process | None = None
        try:
            work.mkdir(parents=True, exist_ok=True)
            payload = {
                "spec": spec.model_dump(mode="json"),
                "output_path": str(output_path),
                "metadata_path": str(metadata_path),
                "policy": SandboxPolicy.build(
                    read_dirs=[Path(spec.model.path).parent],
                    scratch_dir=work,
                    deny_dirs=[self.core.store.home],
                    memory_mb=self.core.settings.sandbox.memory_mb,
                ).to_dict(),
            }
            self._write_json(input_path, payload)
            process = await asyncio.create_subprocess_exec(
                *self._query_worker_command(input_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=work,
                env=_child_env(work),
                **isolated_process_kwargs(),
            )
            self._processes[job_id] = process
            jail = ProcessJail(self.core.settings.sandbox.memory_mb)
            jail.attach(process.pid)
            self._jails[job_id] = jail
            self._replace(
                job_id,
                worker={
                    "pid": process.pid,
                    "controls": list(jail.controls),
                    "environment": "minimal",
                    "network": "blocked",
                    "correlation_id": spec.context.correlation_id if spec.context else None,
                },
                event_type="worker_started",
            )
            try:
                return_code, stderr, worker_failure = await asyncio.wait_for(
                    self._consume_worker(job_id, process),
                    timeout=float(self.core.settings.automation.validation_timeout_s),
                )
            except asyncio.TimeoutError as exc:
                await self._stop_process(process)
                raise ToolError(
                    "JOB_TIMEOUT",
                    "the query worker exceeded its configured timeout.",
                    "Narrow the selector or raise automation.validation_timeout_s.",
                ) from exc
            if return_code != 0:
                if worker_failure is None:
                    worker_failure = JobFailure(
                        code="JOB_WORKER_FAILED",
                        message=stderr[-1000:] or f"worker exited with code {return_code}",
                        hint="Inspect the query input and retry.",
                    )
                raise ToolError(
                    worker_failure.code, worker_failure.message, worker_failure.hint
                )
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            base = Path(spec.model.path).stem
            media_type = (
                "application/x-ndjson"
                if spec.output_format == "jsonl"
                else "text/csv"
            )
            artifact = self.artifacts.put_file(
                output_path,
                name=f"{base}-query.{extension}",
                kind="query-result",
                media_type=media_type,
                producer=job_id,
                revision=spec.revision,
                metadata={
                    **metadata,
                    "query": spec.query,
                    "order_by": spec.order_by,
                },
            )
            self._transition(
                job_id,
                JobState.SUCCEEDED,
                progress=100,
                message="query job completed",
                artifacts=(artifact,),
                summary=metadata,
            )
        except asyncio.CancelledError:
            if process is not None:
                await self._stop_process(process)
            current = self._records[job_id]
            if current.state not in TERMINAL_JOB_STATES:
                self._transition(
                    job_id,
                    JobState.CANCELLED,
                    progress=current.progress,
                    message="query job cancelled",
                )
            raise
        except _JobCancelled:
            if process is not None:
                await self._stop_process(process)
            current = self._records[job_id]
            if current.state not in TERMINAL_JOB_STATES:
                self._transition(
                    job_id,
                    JobState.CANCELLED,
                    progress=current.progress,
                    message="query job cancelled",
                    cancel_requested=True,
                )
        except ToolError as exc:
            self._transition(
                job_id,
                JobState.FAILED,
                progress=self._records[job_id].progress,
                message="query job failed",
                failure=JobFailure(code=exc.code, message=exc.message, hint=exc.hint),
            )
        except Exception as exc:
            self._transition(
                job_id,
                JobState.FAILED,
                progress=self._records[job_id].progress,
                message="query job failed",
                failure=JobFailure(
                    code="JOB_WORKER_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="Inspect the query job record and retry.",
                ),
            )
        finally:
            self._processes.pop(job_id, None)
            jail = self._jails.pop(job_id, None)
            if jail is not None:
                jail.close()
            self._tasks.pop(job_id, None)
            shutil.rmtree(work, ignore_errors=True)
            with contextlib.suppress(OSError):
                self._cancel_path(job_id).unlink()

    async def _consume_worker(
        self, job_id: str, process: Process
    ) -> tuple[int, str, JobFailure | None]:
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        failure: JobFailure | None = None
        line_task: asyncio.Task[bytes] | None = None
        try:
            line_task = asyncio.create_task(process.stdout.readline())
            while True:
                if self._cancel_path(job_id).exists():
                    raise _JobCancelled
                done, _pending = await asyncio.wait({line_task}, timeout=0.1)
                if not done:
                    continue
                line = line_task.result()
                if not line:
                    break
                line_task = asyncio.create_task(process.stdout.readline())
                try:
                    event = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if event.get("type") == "progress":
                    self._progress(
                        job_id,
                        int(event.get("progress", 0)),
                        str(event.get("message", "validation running")),
                    )
                elif event.get("type") == "error":
                    failure = JobFailure(
                        code=str(event.get("code") or "JOB_WORKER_FAILED"),
                        message=str(event.get("message") or "validation worker failed"),
                        hint=str(event.get("hint") or ""),
                    )
                elif event.get("type") == "worker_ready":
                    current = self._records[job_id]
                    controls = list(current.worker.get("controls") or ())
                    controls.extend(str(item) for item in event.get("controls") or ())
                    self._replace(
                        job_id,
                        worker={
                            **current.worker,
                            "controls": list(dict.fromkeys(controls)),
                        },
                        event_type="worker_ready",
                    )
            return_code = await process.wait()
            stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
            return return_code, stderr, failure
        finally:
            if line_task is not None and not line_task.done():
                line_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()

    def _progress(self, job_id: str, progress: int, message: str) -> JobRecord:
        current = self._records[job_id]
        return self._replace(
            job_id,
            progress=max(current.progress, min(progress, 99)),
            message=message,
            event_type="progress",
        )

    def _transition(
        self,
        job_id: str,
        state: JobState,
        *,
        progress: int,
        message: str,
        artifacts: tuple[Any, ...] | None = None,
        summary: dict[str, object] | None = None,
        failure: JobFailure | None = None,
        cancel_requested: bool | None = None,
    ) -> JobRecord:
        current = self._records[job_id]
        if current.state in TERMINAL_JOB_STATES:
            return current
        updates: dict[str, Any] = {
            "state": state,
            "progress": progress,
            "message": message,
            "failure": failure,
            "event_type": state.value,
        }
        if state in TERMINAL_JOB_STATES:
            updates["cancellable"] = False
        if artifacts is not None:
            updates["artifacts"] = artifacts
        if summary is not None:
            updates["summary"] = summary
        if cancel_requested is not None:
            updates["cancel_requested"] = cancel_requested
        record = self._replace(job_id, **updates)
        self._announce(record, "job_state")
        return record

    def _replace(self, job_id: str, **updates: Any) -> JobRecord:
        event_type = str(updates.pop("event_type", "updated"))
        with self._lock:
            current = self._records[job_id]
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
            self._records[job_id] = record
            self._persist(record)
        self.core.events.emit(
            "job_updated",
            job_id=record.job_id,
            state=record.state.value,
            progress=record.progress,
            message=record.message,
        )
        return record

    def _announce(self, record: JobRecord, event: str) -> None:
        self.core.audit.record(
            event,
            job_id=record.job_id,
            kind=record.kind,
            state=record.state.value,
            progress=record.progress,
            workspace_id=record.spec.revision.workspace_id,
            model_id=record.spec.revision.model_id,
            revision_id=record.spec.revision.revision_id,
            artifacts=[artifact.artifact_id for artifact in record.artifacts],
            failure=record.failure.code if record.failure else None,
        )

    def _persist(self, record: JobRecord) -> None:
        owner_pid, owner_id = self._owners.get(record.job_id, (os.getpid(), self.instance_id))
        with self._lock:
            self._write_json(
                self.records_dir / f"{record.job_id}.json",
                {
                    "record": record.model_dump(mode="json"),
                    "owner_pid": owner_pid,
                    "owner_id": owner_id,
                },
            )

    def _load_records(self) -> None:
        for path in self.records_dir.glob("job-*.json"):
            loaded = self._read_record(path.stem)
            if loaded is None:
                continue
            record, owner = loaded
            self._records[record.job_id] = record
            self._owners[record.job_id] = owner
        for job_id, record in list(self._records.items()):
            owner_pid, _owner_id = self._owners[job_id]
            if record.state not in TERMINAL_JOB_STATES and not self._pid_exists(owner_pid):
                if record.kind in {
                    TransactionKind.COMMIT.value,
                    TransactionKind.RESTORE.value,
                } and self._recover_transaction_job(job_id):
                    continue
                self._transition(
                    job_id,
                    JobState.FAILED,
                    progress=record.progress,
                    message="job owner exited before completion",
                    failure=JobFailure(
                        code="JOB_WORKER_FAILED",
                        message="the process supervising this job is no longer running",
                        hint="Submit the job again; incomplete jobs are never reported as successful.",
                    ),
                )

    def _recover_transaction_job(self, job_id: str) -> bool:
        journal = self.core.transactions.journals.find_by_job(job_id)
        if journal is None:
            return False
        common = {
            "phase": journal.phase.value,
            "cancellable": journal.cancellable,
            "transaction_id": journal.transaction_id,
        }
        if journal.phase is TransactionPhase.RECEIPT_PERSISTED:
            receipt_id = journal.receipt_artifact_id or journal.expected_receipt_id
            if not receipt_id:
                return False
            try:
                artifact = self.artifacts.verify(receipt_id)
            except ToolError:
                return False
            key = "commit_id" if journal.kind is TransactionKind.COMMIT else "restore_id"
            self._replace(job_id, **common, event_type="recovered")
            self._transition(
                job_id,
                JobState.SUCCEEDED,
                progress=100,
                message=f"{journal.kind.value} job recovered from its durable receipt",
                artifacts=(artifact,),
                summary={key: receipt_id, "recovered": True},
            )
            return True
        if journal.phase in {
            TransactionPhase.ABORTED,
            TransactionPhase.ROLLED_BACK,
        }:
            self._replace(job_id, **common, event_type="recovered")
            self._transition(
                job_id,
                JobState.FAILED,
                progress=self._records[job_id].progress,
                message=f"{journal.kind.value} job interrupted and target left unchanged",
                failure=JobFailure(
                    code="TRANSACTION_INTERRUPTED",
                    message=journal.error or "the transaction owner exited before completion",
                    hint="Review the journal, then submit a fresh transaction.",
                ),
            )
            return True
        if journal.phase is TransactionPhase.RECOVERY_FAILED:
            self._replace(job_id, **common, event_type="recovery_failed")
            self._transition(
                job_id,
                JobState.FAILED,
                progress=self._records[job_id].progress,
                message=f"{journal.kind.value} job requires manual recovery",
                failure=JobFailure(
                    code="TRANSACTION_RECOVERY_REQUIRED",
                    message=journal.error or "automatic transaction recovery failed",
                    hint="Do not modify the target until its hashes and backup are inspected.",
                ),
            )
            return True
        return False

    def _prune_records(self) -> None:
        records = sorted(self._records.values(), key=lambda record: record.created_at, reverse=True)
        kept = 0
        for record in records:
            if record.state not in TERMINAL_JOB_STATES:
                continue
            kept += 1
            if kept <= self.retention:
                continue
            self._records.pop(record.job_id, None)
            self._owners.pop(record.job_id, None)
            with contextlib.suppress(OSError):
                (self.records_dir / f"{record.job_id}.json").unlink()
            shutil.rmtree(self.work_dir / record.job_id, ignore_errors=True)

    def _read_record(self, job_id: str) -> tuple[JobRecord, tuple[int, str]] | None:
        try:
            with self._lock:
                payload = json.loads(
                    (self.records_dir / f"{job_id}.json").read_text(encoding="utf-8")
                )
            record = JobRecord.model_validate(payload["record"])
            owner = (int(payload.get("owner_pid", 0)), str(payload.get("owner_id", "")))
            return record, owner
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _worker_command(self, input_path: Path) -> tuple[str, ...]:
        return worker_command("ifc_console.automation.validation_worker", input_path)

    def _query_worker_command(self, input_path: Path) -> tuple[str, ...]:
        return worker_command("ifc_console.automation.query_worker", input_path)

    def _cancel_path(self, job_id: str) -> Path:
        return self.cancel_dir / job_id

    @staticmethod
    async def _stop_process(process: Process) -> None:
        if process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        return process_is_running(pid)

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
