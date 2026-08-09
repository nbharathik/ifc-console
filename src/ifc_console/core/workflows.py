"""Versioned contracts for deterministic read-only automation workflows."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.batches import BatchSpec
from ifc_console.core.capabilities import Capability
from ifc_console.core.context import OperationContext
from ifc_console.core.jobs import JobEvent, JobFailure, QueryField

WorkflowId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
PathPattern = Annotated[str, Field(min_length=1, max_length=4096)]


class WorkflowState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_WORKFLOW_STATES = frozenset(
    {
        WorkflowState.SUCCEEDED,
        WorkflowState.PARTIAL,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
        WorkflowState.INTERRUPTED,
    }
)


class WorkflowStepState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


TERMINAL_WORKFLOW_STEP_STATES = frozenset(
    {
        WorkflowStepState.SUCCEEDED,
        WorkflowStepState.PARTIAL,
        WorkflowStepState.FAILED,
        WorkflowStepState.CANCELLED,
        WorkflowStepState.SKIPPED,
        WorkflowStepState.INTERRUPTED,
    }
)


class WorkflowInputSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: WorkflowId
    paths: tuple[PathPattern, ...] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_paths(self) -> WorkflowInputSpec:
        if len(self.paths) != len(set(self.paths)):
            raise ValueError("workflow input paths must be unique")
        return self


class WorkflowValidationOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["validation"] = "validation"
    version: Literal["1"] = "1"
    ids_paths: tuple[PathPattern, ...] = ()
    express_rules: bool = False
    max_issues: int = Field(default=200, ge=1, le=2000)

    @model_validator(mode="after")
    def unique_ids_paths(self) -> WorkflowValidationOperation:
        if len(self.ids_paths) != len(set(self.ids_paths)):
            raise ValueError("workflow IDS paths must be unique")
        return self


class WorkflowQueryOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["query"] = "query"
    version: Literal["1"] = "1"
    query: str = Field(min_length=1, max_length=10_000)
    fields: tuple[QueryField, ...] = ("name", "storey", "type_name")
    order_by: Literal["class", "name", "storey"] = "class"
    output_format: Literal["jsonl", "csv"] = "jsonl"
    limit: int = Field(default=100_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def unique_fields(self) -> WorkflowQueryOperation:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("workflow query fields must be unique")
        return self


WorkflowOperation = Annotated[
    WorkflowValidationOperation | WorkflowQueryOperation,
    Field(discriminator="kind"),
]


class WorkflowStepSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: WorkflowId
    output: WorkflowId | None = None
    input_ids: tuple[WorkflowId, ...] = ("models",)
    needs: tuple[WorkflowId, ...] = ()
    operation: WorkflowOperation
    concurrency: int = Field(default=2, ge=1, le=32)
    failure_policy: Literal["continue", "fail_fast"] = "continue"

    @model_validator(mode="after")
    def unique_references(self) -> WorkflowStepSpec:
        if len(self.input_ids) != len(set(self.input_ids)):
            raise ValueError("workflow step input_ids must be unique")
        if len(self.needs) != len(set(self.needs)):
            raise ValueError("workflow step needs must be unique")
        if self.id in self.needs:
            raise ValueError(f"workflow step {self.id!r} cannot depend on itself")
        return self


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    name: WorkflowId
    inputs: tuple[WorkflowInputSpec, ...] = Field(min_length=1)
    steps: tuple[WorkflowStepSpec, ...] = Field(min_length=1)
    step_concurrency: int = Field(default=1, ge=1, le=8)
    failure_policy: Literal["continue", "fail_fast"] = "continue"

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowSpec:
        input_ids = [item.id for item in self.inputs]
        step_ids = [step.id for step in self.steps]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("workflow input IDs must be unique")
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow step IDs must be unique")
        known_inputs = set(input_ids)
        known_steps = set(step_ids)
        output_ids = [step.output or step.id for step in self.steps]
        if len(output_ids) != len(set(output_ids)):
            raise ValueError("workflow step output names must be unique")
        for step in self.steps:
            missing_inputs = set(step.input_ids).difference(known_inputs)
            if missing_inputs:
                raise ValueError(
                    f"workflow step {step.id!r} names unknown inputs: {sorted(missing_inputs)}"
                )
            missing_needs = set(step.needs).difference(known_steps)
            if missing_needs:
                raise ValueError(
                    f"workflow step {step.id!r} names unknown dependencies: "
                    f"{sorted(missing_needs)}"
                )
        dependencies = {step.id: set(step.needs) for step in self.steps}
        ready = [step_id for step_id, needs in dependencies.items() if not needs]
        visited: set[str] = set()
        while ready:
            current = ready.pop()
            if current in visited:
                continue
            visited.add(current)
            for step_id, needs in dependencies.items():
                if current in needs and needs.issubset(visited):
                    ready.append(step_id)
        if visited != known_steps:
            raise ValueError("workflow dependency graph contains a cycle")
        return self


class WorkflowStepPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: WorkflowId
    output: WorkflowId
    needs: tuple[WorkflowId, ...]
    batch_spec: BatchSpec


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    generated_at: datetime
    manifest_path: str | None = None
    spec: WorkflowSpec
    steps: tuple[WorkflowStepPlan, ...]
    total_children: int = Field(ge=1)
    required_capabilities: tuple[Capability, ...] = (
        Capability.ARTIFACT_WRITE,
        Capability.FILE_READ,
        Capability.JOB_READ,
        Capability.JOB_SUBMIT,
    )

    @staticmethod
    def compute_plan_id(
        spec: WorkflowSpec, steps: tuple[WorkflowStepPlan, ...]
    ) -> str:
        identity = {
            "spec": spec.model_dump(mode="json"),
            "steps": [step.model_dump(mode="json") for step in steps],
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"sha256:{digest}"

    @model_validator(mode="after")
    def validate_plan(self) -> WorkflowPlan:
        expected_capabilities = (
            Capability.ARTIFACT_WRITE,
            Capability.FILE_READ,
            Capability.JOB_READ,
            Capability.JOB_SUBMIT,
        )
        if self.required_capabilities != expected_capabilities:
            raise ValueError("workflow plan required capabilities are not canonical")
        if tuple(step.id for step in self.steps) != tuple(
            step.id for step in self.spec.steps
        ):
            raise ValueError("workflow plan steps must match its specification")
        if self.total_children != sum(len(step.batch_spec.inputs) for step in self.steps):
            raise ValueError("workflow plan total_children does not match its captured inputs")
        for planned, declared in zip(self.steps, self.spec.steps, strict=True):
            if planned.needs != declared.needs:
                raise ValueError(f"workflow plan dependencies differ for step {planned.id!r}")
            if planned.output != (declared.output or declared.id):
                raise ValueError(f"workflow plan output differs for step {planned.id!r}")
            batch = planned.batch_spec
            operation = declared.operation
            if batch.concurrency != declared.concurrency:
                raise ValueError(f"workflow plan concurrency differs for step {planned.id!r}")
            if batch.failure_policy != declared.failure_policy:
                raise ValueError(f"workflow plan failure policy differs for step {planned.id!r}")
            if batch.context is not None:
                raise ValueError("workflow plans may not capture an invocation context")
            if isinstance(operation, WorkflowValidationOperation):
                matches = (
                    batch.operation.kind == "validation"
                    and batch.operation.express_rules == operation.express_rules
                    and batch.operation.max_issues == operation.max_issues
                    and bool(batch.operation.ids_files) == bool(operation.ids_paths)
                )
            else:
                matches = (
                    batch.operation.kind == "query"
                    and batch.operation.query == operation.query
                    and batch.operation.fields == operation.fields
                    and batch.operation.order_by == operation.order_by
                    and batch.operation.output_format == operation.output_format
                    and batch.operation.limit == operation.limit
                )
            if not matches:
                raise ValueError(f"workflow plan operation differs for step {planned.id!r}")
        if self.plan_id != self.compute_plan_id(self.spec, self.steps):
            raise ValueError("workflow plan ID does not match its immutable content")
        return self


class WorkflowStepRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: WorkflowId
    output: WorkflowId
    needs: tuple[WorkflowId, ...]
    batch_spec: BatchSpec
    state: WorkflowStepState = WorkflowStepState.PENDING
    attempts: int = Field(default=0, ge=0)
    last_run: int | None = Field(default=None, ge=1)
    batch_id: str | None = Field(default=None, pattern=r"^batch-[0-9a-f]{16}$")
    created_at: datetime
    updated_at: datetime
    artifact: ArtifactRef | None = None
    summary: dict[str, object] = Field(default_factory=dict)
    failure: JobFailure | None = None


class WorkflowRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workflow_id: str = Field(pattern=r"^workflow-[0-9a-f]{16}$")
    state: WorkflowState
    created_at: datetime
    updated_at: datetime
    progress: int = Field(ge=0, le=100)
    message: str
    plan: WorkflowPlan
    steps: tuple[WorkflowStepRecord, ...]
    context: OperationContext | None = None
    run_count: int = Field(default=1, ge=1)
    events: tuple[JobEvent, ...] = ()
    aggregate_artifact: ArtifactRef | None = None
    summary: dict[str, object] = Field(default_factory=dict)
    failure: JobFailure | None = None
    cancel_requested: bool = False

    @model_validator(mode="after")
    def validate_steps(self) -> WorkflowRecord:
        if tuple(step.id for step in self.steps) != tuple(
            step.id for step in self.plan.steps
        ):
            raise ValueError("workflow record steps must match its immutable plan")
        for recorded, planned in zip(self.steps, self.plan.steps, strict=True):
            if (
                recorded.output != planned.output
                or recorded.needs != planned.needs
                or recorded.batch_spec != planned.batch_spec
            ):
                raise ValueError(
                    f"workflow record step {recorded.id!r} differs from its immutable plan"
                )
        return self
