"""Durable execution of versioned read-only workflow manifests."""

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

import yaml

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import process_is_running
from ifc_console.automation.files import describe_source, source_matches
from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.batches import (
    BatchSpec,
    BatchState,
    QueryBatchOperation,
    ValidationBatchOperation,
)
from ifc_console.core.capabilities import Capability
from ifc_console.core.context import OperationContext, current_operation_context
from ifc_console.core.jobs import JobEvent, JobFailure, SourceFileRef
from ifc_console.core.results import ToolError
from ifc_console.core.workflows import (
    TERMINAL_WORKFLOW_STATES,
    TERMINAL_WORKFLOW_STEP_STATES,
    WorkflowPlan,
    WorkflowRecord,
    WorkflowSpec,
    WorkflowState,
    WorkflowStepPlan,
    WorkflowStepRecord,
    WorkflowStepState,
    WorkflowValidationOperation,
)

if TYPE_CHECKING:
    from ifc_console.app import AppCore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowService:
    """Plan and supervise deterministic validation/query workflow DAGs."""

    _MAX_MANIFEST_BYTES = 2 * 1024 * 1024
    _MAX_RESOLVED_FILES = 10_000
    _HASH_CONCURRENCY = 16

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
        self._records: dict[str, WorkflowRecord] = {}
        self._owners: dict[str, tuple[int, str]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._resume_locks: dict[str, asyncio.Lock] = {}
        self._resume_lock_users: dict[str, int] = {}
        self._lock = RLock()
        self._closing = False
        self._load_records()
        self._prune_records()

    async def plan_manifest(self, manifest_path: str | Path) -> WorkflowPlan:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "plan_workflow", authority="caller", client=self.core.transport
            ):
                return await self.plan_manifest(manifest_path)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.FILE_READ, Capability.JOB_READ],
            authority=context.authority,
            action="plan workflow",
        )
        path = self.core.require_path_allowed(Path(manifest_path))
        if not path.is_file():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"workflow manifest {path} does not exist.",
                "Choose an existing .json, .yaml, or .yml manifest.",
            )
        try:
            if path.stat().st_size > self._MAX_MANIFEST_BYTES:
                raise ToolError(
                    "WORKFLOW_MANIFEST_TOO_LARGE",
                    f"workflow manifest exceeds {self._MAX_MANIFEST_BYTES} bytes.",
                    "Split the workflow or reduce generated manifest content.",
                )
            text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            if path.suffix.casefold() == ".json":
                payload = json.loads(text)
            elif path.suffix.casefold() in {".yaml", ".yml"}:
                payload = yaml.safe_load(text)
            else:
                raise ToolError(
                    "INVALID_INPUT",
                    f"unsupported workflow manifest extension {path.suffix!r}.",
                    "Use a .json, .yaml, or .yml manifest.",
                )
            spec = WorkflowSpec.model_validate(payload)
        except ToolError:
            raise
        except (OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ToolError(
                "WORKFLOW_MANIFEST_INVALID",
                f"workflow manifest is invalid: {exc}",
                "Fix the manifest against WorkflowSpec version 1 and retry.",
            ) from exc
        return await self.plan_spec(spec, base_dir=path.parent, manifest_path=path)

    async def plan_spec(
        self,
        spec: WorkflowSpec,
        *,
        base_dir: Path,
        manifest_path: Path | None = None,
    ) -> WorkflowPlan:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "plan_workflow", authority="caller", client=self.core.transport
            ):
                return await self.plan_spec(
                    spec, base_dir=base_dir, manifest_path=manifest_path
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.FILE_READ, Capability.JOB_READ],
            authority=context.authority,
            action="plan workflow",
        )
        base = self.core.require_path_allowed(base_dir)
        groups: dict[str, tuple[Path, ...]] = {}
        for item in spec.inputs:
            groups[item.id] = self._resolve_patterns(base, item.paths, kind="ifc")
        step_paths: dict[str, tuple[Path, ...]] = {}
        step_ids: dict[str, tuple[Path, ...]] = {}
        all_paths: dict[str, Path] = {}
        for step in spec.steps:
            inputs = tuple(
                dict.fromkeys(path for input_id in step.input_ids for path in groups[input_id])
            )
            if not inputs:
                raise ToolError(
                    "WORKFLOW_INPUT_EMPTY",
                    f"workflow step {step.id!r} resolved no IFC inputs.",
                    "Fix its input groups or file patterns.",
                )
            step_paths[step.id] = inputs
            for path in inputs:
                all_paths[str(path)] = path
            operation = step.operation
            ids = (
                self._resolve_patterns(base, operation.ids_paths, kind="ids")
                if isinstance(operation, WorkflowValidationOperation)
                and operation.ids_paths
                else ()
            )
            step_ids[step.id] = ids
            for path in ids:
                all_paths[str(path)] = path
        if len(all_paths) > self._MAX_RESOLVED_FILES:
            raise ToolError(
                "WORKFLOW_INPUT_LIMIT",
                f"workflow resolved {len(all_paths)} files; limit is {self._MAX_RESOLVED_FILES}.",
                "Split the input set across smaller workflow runs.",
            )
        semaphore = asyncio.Semaphore(self._HASH_CONCURRENCY)

        async def capture(path: Path) -> SourceFileRef:
            async with semaphore:
                return await asyncio.to_thread(describe_source, path)

        try:
            described = await asyncio.gather(*(capture(path) for path in all_paths.values()))
        except (OSError, ValueError) as exc:
            raise ToolError(
                "INVALID_INPUT",
                f"could not capture workflow inputs: {exc}",
                "Use readable IFC and IDS files below an allowed directory.",
            ) from exc
        sources = dict(zip(all_paths, described, strict=True))
        plans: list[WorkflowStepPlan] = []
        for step in spec.steps:
            operation = step.operation
            if isinstance(operation, WorkflowValidationOperation):
                batch_operation = ValidationBatchOperation(
                    ids_files=tuple(sources[str(path)] for path in step_ids[step.id]),
                    express_rules=operation.express_rules,
                    max_issues=operation.max_issues,
                )
            else:
                batch_operation = QueryBatchOperation(
                    query=operation.query,
                    fields=operation.fields,
                    order_by=operation.order_by,
                    output_format=operation.output_format,
                    limit=operation.limit,
                )
            plans.append(
                WorkflowStepPlan(
                    id=step.id,
                    output=step.output or step.id,
                    needs=step.needs,
                    batch_spec=BatchSpec(
                        operation=batch_operation,
                        inputs=tuple(sources[str(path)] for path in step_paths[step.id]),
                        concurrency=step.concurrency,
                        failure_policy=step.failure_policy,
                    ),
                )
            )
        planned_steps = tuple(plans)
        return WorkflowPlan(
            plan_id=WorkflowPlan.compute_plan_id(spec, planned_steps),
            generated_at=_now(),
            manifest_path=str(manifest_path) if manifest_path is not None else None,
            spec=spec,
            steps=planned_steps,
            total_children=sum(len(item.batch_spec.inputs) for item in plans),
        )

    async def submit_manifest(self, manifest_path: str | Path) -> WorkflowRecord:
        plan = await self.plan_manifest(manifest_path)
        return await self.submit_plan(plan)

    async def submit_plan(self, plan: WorkflowPlan) -> WorkflowRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "submit_workflow", authority="caller", client=self.core.transport
            ):
                return await self.submit_plan(plan)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT, Capability.ARTIFACT_WRITE],
            authority=context.authority,
            action="submit workflow",
        )
        if self._closing:
            raise ToolError(
                "WORKFLOW_SERVICE_CLOSED",
                "the local workflow service is shutting down.",
                "Create a new workbench before submitting more work.",
            )
        sources = self._plan_sources(plan)
        for source in sources:
            self.core.require_path_allowed(Path(source.path))
        matches = await asyncio.gather(
            *(asyncio.to_thread(source_matches, source) for source in sources)
        )
        if not all(matches):
            raise ToolError(
                "WORKFLOW_SOURCE_CHANGED",
                "a workflow source changed after planning.",
                "Generate and review a new workflow plan before submitting.",
            )
        return self._create(plan, context=context)

    def get(self, workflow_id: str) -> WorkflowRecord:
        if re.fullmatch(r"workflow-[0-9a-f]{16}", workflow_id) is None:
            raise ToolError(
                "WORKFLOW_NOT_FOUND",
                f"invalid workflow ID {workflow_id!r}.",
                "Workflow IDs have the form workflow-<16 lowercase hex characters>.",
            )
        record_path = self.records_dir / f"{workflow_id}.json"
        loaded = self._read_record(workflow_id)
        if loaded is not None:
            record, owner = loaded
            self._records[workflow_id] = record
            self._owners[workflow_id] = owner
        elif record_path.exists():
            raise ToolError(
                "WORKFLOW_STORE_CORRUPT",
                f"workflow record {workflow_id!r} is unreadable or invalid.",
                "Restore the record from backup or remove it and submit a new workflow.",
            )
        record = self._records.get(workflow_id)
        if record is None:
            raise ToolError(
                "WORKFLOW_NOT_FOUND",
                f"no workflow named {workflow_id!r}.",
                "List workflow runs and use a returned workflow_id.",
            )
        return record

    def list(self, *, limit: int = 100) -> list[WorkflowRecord]:
        for path in self.records_dir.glob("workflow-*.json"):
            loaded = self._read_record(path.stem)
            if loaded is not None:
                record, owner = loaded
                self._records[record.workflow_id] = record
                self._owners[record.workflow_id] = owner
        records = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
        return records[: max(0, limit)]

    async def wait(
        self, workflow_id: str, *, timeout: float | None = None
    ) -> WorkflowRecord:
        started = time.monotonic()
        while True:
            record = self.get(workflow_id)
            if record.state in TERMINAL_WORKFLOW_STATES:
                return record
            task = self._tasks.get(workflow_id)
            if task is not None:
                remaining = None
                if timeout is not None:
                    remaining = max(0.0, timeout - (time.monotonic() - started))
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise ToolError(
                        "WORKFLOW_TIMEOUT",
                        f"timed out waiting for {workflow_id}.",
                        "The workflow is still running; inspect or cancel it explicitly.",
                    ) from exc
                except asyncio.CancelledError:
                    if not task.cancelled():
                        raise
                continue
            if timeout is not None and time.monotonic() - started >= timeout:
                raise ToolError(
                    "WORKFLOW_TIMEOUT",
                    f"timed out waiting for {workflow_id}.",
                    "The workflow may belong to another local process.",
                )
            await asyncio.sleep(0.1)

    async def watch(
        self, workflow_id: str, *, poll_interval: float = 0.1
    ) -> AsyncIterator[WorkflowRecord]:
        last_update: datetime | None = None
        while True:
            record = self.get(workflow_id)
            if record.updated_at != last_update:
                last_update = record.updated_at
                yield record
            if record.state in TERMINAL_WORKFLOW_STATES:
                return
            await asyncio.sleep(max(0.01, poll_interval))

    async def cancel(self, workflow_id: str) -> WorkflowRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "cancel_workflow", authority="caller", client=self.core.transport
            ):
                return await self.cancel(workflow_id)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_CANCEL], authority=context.authority, action="cancel workflow"
        )
        record = self.get(workflow_id)
        if record.state in TERMINAL_WORKFLOW_STATES:
            return record
        self._replace(
            workflow_id,
            cancel_requested=True,
            message="workflow cancellation requested",
            event_type="cancel_requested",
        )
        self._cancel_path(workflow_id).touch(exist_ok=True)
        task = self._tasks.get(workflow_id)
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.get(workflow_id)

    async def resume(self, workflow_id: str) -> WorkflowRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "resume_workflow", authority="caller", client=self.core.transport
            ):
                return await self.resume(workflow_id)
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.JOB_SUBMIT], authority=context.authority, action="resume workflow"
        )
        observed = self.get(workflow_id)
        lock = self._resume_locks.setdefault(workflow_id, asyncio.Lock())
        self._resume_lock_users[workflow_id] = (
            self._resume_lock_users.get(workflow_id, 0) + 1
        )
        try:
            async with lock:
                return await self._resume_locked(
                    workflow_id,
                    context=context,
                    expected_run_count=observed.run_count,
                )
        finally:
            users = self._resume_lock_users[workflow_id] - 1
            if users:
                self._resume_lock_users[workflow_id] = users
            else:
                self._resume_lock_users.pop(workflow_id, None)
                if self._resume_locks.get(workflow_id) is lock:
                    self._resume_locks.pop(workflow_id, None)

    async def _resume_locked(
        self,
        workflow_id: str,
        *,
        context: OperationContext,
        expected_run_count: int,
    ) -> WorkflowRecord:
        record = self.get(workflow_id)
        if record.run_count != expected_run_count:
            raise ToolError(
                "WORKFLOW_NOT_RESUMABLE",
                f"{workflow_id} changed while the resume request was waiting.",
                "Inspect the latest workflow state before retrying.",
            )
        if record.state not in {
            WorkflowState.PARTIAL,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
            WorkflowState.INTERRUPTED,
        }:
            raise ToolError(
                "WORKFLOW_NOT_RESUMABLE",
                f"{workflow_id} is {record.state.value}, so it cannot be resumed.",
                "Resume a partial, failed, cancelled, or interrupted workflow.",
            )
        sources = self._plan_sources(record.plan)
        for source in sources:
            self.core.require_path_allowed(Path(source.path))
        matches = await asyncio.gather(
            *(asyncio.to_thread(source_matches, source) for source in sources)
        )
        if not all(matches):
            raise ToolError(
                "WORKFLOW_SOURCE_CHANGED",
                "a captured workflow source changed.",
                "Submit a new workflow run against current source identities.",
            )
        steps: list[WorkflowStepRecord] = []
        for step in record.steps:
            reusable = step.state is WorkflowStepState.SUCCEEDED and step.artifact is not None
            if reusable:
                try:
                    await asyncio.to_thread(self.artifacts.verify, step.artifact.artifact_id)
                    batch = self.core.batches.get(step.batch_id or "")
                    reusable = batch.state is BatchState.SUCCEEDED
                except ToolError:
                    reusable = False
            if reusable:
                steps.append(step)
            else:
                steps.append(
                    step.model_copy(
                        update={
                            "state": WorkflowStepState.PENDING,
                            "updated_at": _now(),
                            "batch_id": None,
                            "artifact": None,
                            "summary": {},
                            "failure": None,
                        }
                    )
                )
        self._owners[workflow_id] = (os.getpid(), self.instance_id)
        with contextlib.suppress(OSError):
            self._cancel_path(workflow_id).unlink()
        resumed = self._replace(
            workflow_id,
            state=WorkflowState.QUEUED,
            progress=self._progress_for(tuple(steps)),
            message="workflow queued for resume",
            steps=tuple(steps),
            context=context,
            run_count=record.run_count + 1,
            aggregate_artifact=None,
            summary={},
            failure=None,
            cancel_requested=False,
            event_type="resumed",
        )
        self._schedule(workflow_id)
        self._announce(resumed, "workflow_resumed")
        return resumed

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for workflow_id, task in tuple(self._tasks.items()):
            if task.done():
                continue
            with contextlib.suppress(OSError):
                self._cancel_path(workflow_id).touch(exist_ok=True)
            task.cancel()

    async def aclose(self) -> None:
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        self.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _create(
        self, plan: WorkflowPlan, *, context: OperationContext
    ) -> WorkflowRecord:
        workflow_id = f"workflow-{secrets.token_hex(8)}"
        created = _now()
        message = f"workflow {plan.spec.name!r} queued with {len(plan.steps)} step(s)"
        record = WorkflowRecord(
            workflow_id=workflow_id,
            state=WorkflowState.QUEUED,
            created_at=created,
            updated_at=created,
            progress=0,
            message=message,
            plan=plan,
            context=context,
            steps=tuple(
                WorkflowStepRecord(
                    id=step.id,
                    output=step.output,
                    needs=step.needs,
                    batch_spec=step.batch_spec,
                    created_at=created,
                    updated_at=created,
                )
                for step in plan.steps
            ),
            events=(JobEvent(ts=created, type="queued", progress=0, message=message),),
        )
        self._owners[workflow_id] = (os.getpid(), self.instance_id)
        try:
            self._persist(record)
        except OSError as exc:
            self._owners.pop(workflow_id, None)
            raise ToolError(
                "WORKFLOW_STORE_FAILED",
                f"could not persist the workflow before scheduling: {exc}",
                "Free local storage or repair the IFC-Console home, then retry.",
            ) from exc
        self._records[workflow_id] = record
        self._prune_records()
        self._schedule(workflow_id)
        self._announce(record, "workflow_submitted")
        return record

    def _schedule(self, workflow_id: str) -> None:
        task = asyncio.create_task(self._run(workflow_id), name=workflow_id)
        self._tasks[workflow_id] = task

    async def _run(self, workflow_id: str) -> None:
        active: dict[asyncio.Task[WorkflowStepState], str] = {}
        try:
            self._replace(
                workflow_id,
                state=WorkflowState.RUNNING,
                message="workflow running",
                event_type="running",
            )
            fail_fast = False
            while True:
                record = self._records[workflow_id]
                if self._cancel_requested(workflow_id):
                    await self._cancel_active(workflow_id, active)
                    self._stop_pending(workflow_id, "workflow cancelled")
                    break
                states = {step.id: step.state for step in record.steps}
                for step in record.steps:
                    if step.state is not WorkflowStepState.PENDING:
                        continue
                    needed = [states[item] for item in step.needs]
                    if any(
                        state in TERMINAL_WORKFLOW_STEP_STATES
                        and state is not WorkflowStepState.SUCCEEDED
                        for state in needed
                    ):
                        self._replace_step(
                            workflow_id,
                            step.id,
                            state=WorkflowStepState.SKIPPED,
                            failure=JobFailure(
                                code="WORKFLOW_DEPENDENCY_FAILED",
                                message="a required workflow step did not succeed",
                                hint="Inspect the dependency failure, then resume the workflow.",
                            ),
                        )
                record = self._records[workflow_id]
                states = {step.id: step.state for step in record.steps}
                ready = [
                    step.id
                    for step in record.steps
                    if step.state is WorkflowStepState.PENDING
                    and all(states[item] is WorkflowStepState.SUCCEEDED for item in step.needs)
                ]
                while (
                    ready
                    and len(active) < record.plan.spec.step_concurrency
                    and not fail_fast
                ):
                    step_id = ready.pop(0)
                    task = asyncio.create_task(self._run_step(workflow_id, step_id))
                    active[task] = step_id
                pending = any(
                    step.state is WorkflowStepState.PENDING
                    for step in self._records[workflow_id].steps
                )
                if not active:
                    if not pending or fail_fast:
                        if fail_fast:
                            self._stop_pending(
                                workflow_id, "skipped after fail-fast workflow failure"
                            )
                        break
                    await asyncio.sleep(0.05)
                    continue
                done, _waiting = await asyncio.wait(
                    active, timeout=0.1, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    active.pop(task)
                    state = task.result()
                    if (
                        state is not WorkflowStepState.SUCCEEDED
                        and record.plan.spec.failure_policy == "fail_fast"
                    ):
                        fail_fast = True
                if fail_fast:
                    await self._cancel_active(workflow_id, active)
            await self._finalize(workflow_id)
        except asyncio.CancelledError:
            await self._cancel_active(workflow_id, active)
            current = self._records.get(workflow_id)
            if current is not None and current.state not in TERMINAL_WORKFLOW_STATES:
                self._stop_pending(workflow_id, "workflow cancelled during shutdown")
                await self._finalize(workflow_id, force_cancelled=True)
            raise
        except Exception as exc:
            await self._cancel_active(workflow_id, active)
            current = self._records[workflow_id]
            self._replace(
                workflow_id,
                state=WorkflowState.FAILED,
                progress=current.progress,
                message="workflow supervisor failed",
                failure=JobFailure(
                    code="WORKFLOW_SUPERVISOR_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="Inspect durable workflow and batch records, then resume.",
                ),
                event_type="failed",
            )
        finally:
            self._tasks.pop(workflow_id, None)
            with contextlib.suppress(OSError):
                self._cancel_path(workflow_id).unlink()

    async def _run_step(self, workflow_id: str, step_id: str) -> WorkflowStepState:
        record = self._records[workflow_id]
        step = self._step(record, step_id)
        try:
            batch_spec = step.batch_spec.model_copy(update={"context": record.context})
            batch = self.core.batches.submit_captured(batch_spec)
            self._replace_step(
                workflow_id,
                step_id,
                state=WorkflowStepState.RUNNING,
                attempts=step.attempts + 1,
                last_run=record.run_count,
                batch_id=batch.batch_id,
                failure=None,
            )
            batch = await self.core.batches.wait(batch.batch_id)
            mapping = {
                BatchState.SUCCEEDED: WorkflowStepState.SUCCEEDED,
                BatchState.PARTIAL: WorkflowStepState.PARTIAL,
                BatchState.FAILED: WorkflowStepState.FAILED,
                BatchState.CANCELLED: WorkflowStepState.CANCELLED,
                BatchState.INTERRUPTED: WorkflowStepState.INTERRUPTED,
            }
            state = mapping.get(batch.state, WorkflowStepState.FAILED)
            self._replace_step(
                workflow_id,
                step_id,
                state=state,
                artifact=batch.aggregate_artifact,
                summary=batch.summary,
                failure=batch.failure,
            )
            return state
        except asyncio.CancelledError:
            raise
        except ToolError as exc:
            self._replace_step(
                workflow_id,
                step_id,
                state=WorkflowStepState.FAILED,
                failure=JobFailure(code=exc.code, message=exc.message, hint=exc.hint),
            )
            return WorkflowStepState.FAILED
        except Exception as exc:
            self._replace_step(
                workflow_id,
                step_id,
                state=WorkflowStepState.FAILED,
                failure=JobFailure(
                    code="WORKFLOW_STEP_FAILED",
                    message=f"{type(exc).__name__}: {exc}",
                    hint="Inspect the step and durable batch records, then resume.",
                ),
            )
            return WorkflowStepState.FAILED

    async def _cancel_active(
        self,
        workflow_id: str,
        active: dict[asyncio.Task[WorkflowStepState], str],
    ) -> None:
        record = self._records[workflow_id]
        batch_ids = [
            step.batch_id
            for step in record.steps
            if step.state is WorkflowStepState.RUNNING and step.batch_id is not None
        ]
        await asyncio.gather(
            *(self.core.batches.cancel(batch_id) for batch_id in batch_ids),
            return_exceptions=True,
        )
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        active.clear()

    def _stop_pending(self, workflow_id: str, message: str) -> None:
        for step in self._records[workflow_id].steps:
            if step.state is WorkflowStepState.PENDING:
                self._replace_step(
                    workflow_id,
                    step.id,
                    state=WorkflowStepState.CANCELLED,
                    failure=JobFailure(
                        code="WORKFLOW_CANCELLED",
                        message=message,
                        hint="Resume the workflow to retry this step.",
                    ),
                )

    async def _finalize(
        self, workflow_id: str, *, force_cancelled: bool = False
    ) -> None:
        record = self._records[workflow_id]
        counts = {state.value: 0 for state in WorkflowStepState}
        for step in record.steps:
            counts[step.state.value] += 1
        succeeded = counts[WorkflowStepState.SUCCEEDED.value]
        failed = sum(
            counts[state.value]
            for state in (
                WorkflowStepState.FAILED,
                WorkflowStepState.PARTIAL,
                WorkflowStepState.INTERRUPTED,
                WorkflowStepState.SKIPPED,
            )
        )
        cancelled = counts[WorkflowStepState.CANCELLED.value]
        if force_cancelled or record.cancel_requested or self._cancel_path(workflow_id).exists():
            state = WorkflowState.CANCELLED
        elif succeeded == len(record.steps):
            state = WorkflowState.SUCCEEDED
        elif succeeded:
            state = WorkflowState.PARTIAL
        elif failed:
            state = WorkflowState.FAILED
        else:
            state = WorkflowState.CANCELLED
        summary: dict[str, object] = {
            "step_count": len(record.steps),
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
            "passed": succeeded == len(record.steps)
            and all(bool(step.summary.get("passed")) for step in record.steps),
            "reused": sum(
                1
                for step in record.steps
                if step.state is WorkflowStepState.SUCCEEDED
                and step.last_run is not None
                and step.last_run < record.run_count
            ),
        }
        artifact = self._write_manifest(record, state, summary)
        message = {
            WorkflowState.SUCCEEDED: "workflow completed",
            WorkflowState.PARTIAL: "workflow completed with failed steps",
            WorkflowState.FAILED: "workflow failed",
            WorkflowState.CANCELLED: "workflow cancelled",
        }[state]
        final = self._replace(
            workflow_id,
            state=state,
            progress=100,
            message=message,
            aggregate_artifact=artifact,
            summary=summary,
            failure=(
                JobFailure(
                    code="WORKFLOW_STEP_FAILED",
                    message=f"{failed} workflow step(s) did not succeed.",
                    hint="Inspect step and child batch failures before resuming.",
                )
                if failed
                else None
            ),
            event_type=state.value,
        )
        self._announce(final, "workflow_state")

    def _write_manifest(
        self,
        record: WorkflowRecord,
        state: WorkflowState,
        summary: dict[str, object],
    ) -> ArtifactRef:
        references = tuple(
            step.artifact.artifact_id for step in record.steps if step.artifact is not None
        )
        payload = {
            "version": "1",
            "workflow_id": record.workflow_id,
            "plan_id": record.plan.plan_id,
            "name": record.plan.spec.name,
            "state": state.value,
            "created_at": record.created_at.isoformat(),
            "completed_at": _now().isoformat(),
            "run_count": record.run_count,
            "summary": summary,
            "steps": [step.model_dump(mode="json") for step in record.steps],
        }
        return self.artifacts.put_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            name=f"{record.workflow_id}-manifest.json",
            kind="workflow-manifest",
            media_type="application/json",
            producer=record.workflow_id,
            metadata={"state": state.value, "plan_id": record.plan.plan_id, **summary},
            references=references,
        )

    def _replace_step(
        self, workflow_id: str, step_id: str, **updates: Any
    ) -> WorkflowRecord:
        with self._lock:
            current = self._records[workflow_id]
            steps = list(current.steps)
            index = next(index for index, step in enumerate(steps) if step.id == step_id)
            updates["updated_at"] = _now()
            steps[index] = steps[index].model_copy(update=updates)
        return self._replace(
            workflow_id,
            steps=tuple(steps),
            progress=self._progress_for(tuple(steps)),
            message=(
                f"completed {sum(step.state in TERMINAL_WORKFLOW_STEP_STATES for step in steps)} "
                f"of {len(steps)} workflow steps"
            ),
            event_type="step_updated",
        )

    def _replace(self, workflow_id: str, **updates: Any) -> WorkflowRecord:
        event_type = str(updates.pop("event_type", "updated"))
        with self._lock:
            current = self._records[workflow_id]
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
            self._records[workflow_id] = record
        self.core.events.emit(
            "workflow_updated",
            workflow_id=workflow_id,
            state=record.state.value,
            progress=record.progress,
            message=record.message,
        )
        return record

    def _announce(self, record: WorkflowRecord, event: str) -> None:
        self.core.audit.record(
            event,
            workflow_id=record.workflow_id,
            plan_id=record.plan.plan_id,
            name=record.plan.spec.name,
            state=record.state.value,
            progress=record.progress,
            step_count=len(record.steps),
            aggregate_artifact=(
                record.aggregate_artifact.artifact_id if record.aggregate_artifact else None
            ),
            failure=record.failure.code if record.failure else None,
        )

    def _persist(self, record: WorkflowRecord) -> None:
        owner_pid, owner_id = self._owners.get(
            record.workflow_id, (os.getpid(), self.instance_id)
        )
        self._write_json(
            self.records_dir / f"{record.workflow_id}.json",
            {
                "record": record.model_dump(mode="json"),
                "owner_pid": owner_pid,
                "owner_id": owner_id,
            },
        )

    def _load_records(self) -> None:
        for path in self.records_dir.glob("workflow-*.json"):
            loaded = self._read_record(path.stem)
            if loaded is None:
                continue
            record, owner = loaded
            self._records[record.workflow_id] = record
            self._owners[record.workflow_id] = owner
        for workflow_id, record in tuple(self._records.items()):
            owner_pid, _owner_id = self._owners[workflow_id]
            if record.state in TERMINAL_WORKFLOW_STATES or process_is_running(owner_pid):
                continue
            steps = tuple(
                step.model_copy(
                    update={
                        "state": WorkflowStepState.INTERRUPTED,
                        "updated_at": _now(),
                        "failure": JobFailure(
                            code="WORKFLOW_INTERRUPTED",
                            message="the workflow supervisor exited before this step completed",
                            hint="Resume the workflow to retry this captured step.",
                        ),
                    }
                )
                if step.state not in TERMINAL_WORKFLOW_STEP_STATES
                else step
                for step in record.steps
            )
            recovered = self._replace(
                workflow_id,
                state=WorkflowState.INTERRUPTED,
                steps=steps,
                progress=self._progress_for(steps),
                message="workflow owner exited before completion",
                failure=JobFailure(
                    code="WORKFLOW_INTERRUPTED",
                    message="the process supervising this workflow is no longer running",
                    hint="Verify captured sources, then resume the workflow.",
                ),
                event_type="interrupted",
            )
            self._announce(recovered, "workflow_recovered")

    def _read_record(
        self, workflow_id: str
    ) -> tuple[WorkflowRecord, tuple[int, str]] | None:
        try:
            with self._lock:
                payload = json.loads(
                    (self.records_dir / f"{workflow_id}.json").read_text(encoding="utf-8")
                )
            return WorkflowRecord.model_validate(payload["record"]), (
                int(payload.get("owner_pid", 0)),
                str(payload.get("owner_id", "")),
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _prune_records(self) -> None:
        records = sorted(self._records.values(), key=lambda item: item.created_at, reverse=True)
        kept = 0
        for record in records:
            if record.state not in TERMINAL_WORKFLOW_STATES:
                continue
            kept += 1
            if kept <= self.retention:
                continue
            self._records.pop(record.workflow_id, None)
            self._owners.pop(record.workflow_id, None)
            with contextlib.suppress(OSError):
                (self.records_dir / f"{record.workflow_id}.json").unlink()

    def _resolve_patterns(
        self, base: Path, patterns: tuple[str, ...], *, kind: str
    ) -> tuple[Path, ...]:
        resolved: list[Path] = []
        for raw in patterns:
            pattern = raw.replace("\\", "/")
            candidate = Path(pattern)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ToolError(
                    "WORKFLOW_PATH_INVALID",
                    f"workflow path pattern {raw!r} is not relative and contained.",
                    "Use paths below the manifest directory without '..'.",
                )
            try:
                matches = sorted(path for path in base.glob(pattern) if path.is_file())
            except (OSError, ValueError) as exc:
                raise ToolError(
                    "WORKFLOW_PATH_INVALID",
                    f"workflow path pattern {raw!r} is invalid: {exc}",
                    "Use a relative file path or glob below the manifest directory.",
                ) from exc
            if not matches:
                raise ToolError(
                    "WORKFLOW_INPUT_EMPTY",
                    f"workflow pattern {raw!r} matched no files.",
                    "Correct the path relative to the manifest directory.",
                )
            for path in matches:
                resolved.append(self.core.resolve_attachment(str(path), kind=kind))
                if len(resolved) > self._MAX_RESOLVED_FILES:
                    raise ToolError(
                        "WORKFLOW_INPUT_LIMIT",
                        f"workflow input group exceeds {self._MAX_RESOLVED_FILES} files.",
                        "Use narrower glob patterns or split the workflow.",
                    )
        return tuple(dict.fromkeys(resolved))

    @staticmethod
    def _plan_sources(plan: WorkflowPlan) -> tuple[SourceFileRef, ...]:
        sources: dict[tuple[str, str], SourceFileRef] = {}
        for step in plan.steps:
            for source in step.batch_spec.inputs:
                sources[(source.path, source.sha256)] = source
            operation = step.batch_spec.operation
            if isinstance(operation, ValidationBatchOperation):
                for source in operation.ids_files:
                    sources[(source.path, source.sha256)] = source
        return tuple(sources.values())

    @staticmethod
    def _step(record: WorkflowRecord, step_id: str) -> WorkflowStepRecord:
        return next(step for step in record.steps if step.id == step_id)

    def _cancel_requested(self, workflow_id: str) -> bool:
        return (
            self._records[workflow_id].cancel_requested
            or self._cancel_path(workflow_id).exists()
        )

    def _cancel_path(self, workflow_id: str) -> Path:
        return self.cancel_dir / workflow_id

    @staticmethod
    def _progress_for(steps: tuple[WorkflowStepRecord, ...]) -> int:
        if not steps:
            return 0
        completed = sum(step.state in TERMINAL_WORKFLOW_STEP_STATES for step in steps)
        return min(99, int(completed * 100 / len(steps)))

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
