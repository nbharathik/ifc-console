"""Durable contracts for bounded, resumable IFC batch automation."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ifc_console._compat import StrEnum
from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.context import OperationContext
from ifc_console.core.jobs import (
    JobEvent,
    JobFailure,
    JobState,
    QueryField,
    SourceFileRef,
)


class BatchState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_BATCH_STATES = frozenset(
    {
        BatchState.SUCCEEDED,
        BatchState.PARTIAL,
        BatchState.FAILED,
        BatchState.CANCELLED,
        BatchState.INTERRUPTED,
    }
)


class ValidationBatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["validation"] = "validation"
    ids_files: tuple[SourceFileRef, ...] = ()
    express_rules: bool = False
    max_issues: int = Field(default=200, ge=1, le=2000)


class QueryBatchOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["query"] = "query"
    query: str = Field(min_length=1, max_length=10_000)
    fields: tuple[QueryField, ...] = ("name", "storey", "type_name")
    order_by: Literal["class", "name", "storey"] = "class"
    output_format: Literal["jsonl", "csv"] = "jsonl"
    limit: int = Field(default=100_000, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def unique_fields(self) -> QueryBatchOperation:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("query fields must be unique")
        return self


BatchOperation = Annotated[
    ValidationBatchOperation | QueryBatchOperation,
    Field(discriminator="kind"),
]


class BatchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    operation: BatchOperation
    inputs: tuple[SourceFileRef, ...] = Field(min_length=1)
    concurrency: int = Field(default=2, ge=1, le=32)
    failure_policy: Literal["continue", "fail_fast"] = "continue"
    context: OperationContext | None = None

    @model_validator(mode="after")
    def unique_inputs(self) -> BatchSpec:
        paths = [source.path.casefold() for source in self.inputs]
        if len(paths) != len(set(paths)):
            raise ValueError("batch inputs must be unique")
        return self


class BatchChildRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0)
    source: SourceFileRef
    state: JobState = JobState.QUEUED
    attempts: int = Field(default=0, ge=0)
    last_run: int | None = Field(default=None, ge=1)
    job_id: str | None = Field(default=None, pattern=r"^job-[0-9a-f]{16}$")
    created_at: datetime
    updated_at: datetime
    artifacts: tuple[ArtifactRef, ...] = ()
    summary: dict[str, object] = Field(default_factory=dict)
    failure: JobFailure | None = None


class BatchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str = Field(pattern=r"^batch-[0-9a-f]{16}$")
    state: BatchState
    created_at: datetime
    updated_at: datetime
    progress: int = Field(ge=0, le=100)
    message: str
    spec: BatchSpec
    children: tuple[BatchChildRecord, ...]
    run_count: int = Field(default=1, ge=1)
    events: tuple[JobEvent, ...] = ()
    aggregate_artifact: ArtifactRef | None = None
    summary: dict[str, object] = Field(default_factory=dict)
    failure: JobFailure | None = None
    cancel_requested: bool = False

    @model_validator(mode="after")
    def validate_children(self) -> BatchRecord:
        if len(self.children) != len(self.spec.inputs):
            raise ValueError("batch child count must match the captured inputs")
        for index, child in enumerate(self.children):
            if child.index != index or child.source != self.spec.inputs[index]:
                raise ValueError("batch children must preserve input order and identity")
        return self
