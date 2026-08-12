"""Job contracts shared by embedded and remote automation clients."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ifc_console._compat import StrEnum
from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.context import OperationContext
from ifc_console.core.revisions import RevisionRef


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATES = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED})


class SourceFileRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ValidationJobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["validation"] = "validation"
    revision: RevisionRef
    model: SourceFileRef
    ids_files: tuple[SourceFileRef, ...] = ()
    express_rules: bool = False
    max_issues: int = Field(default=200, ge=1, le=2000)
    context: OperationContext | None = None


QueryField = Literal[
    "name",
    "predefined_type",
    "type_name",
    "storey",
    "description",
    "tag",
]


class QueryJobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["query"] = "query"
    revision: RevisionRef
    model: SourceFileRef
    query: str = Field(min_length=1, max_length=10_000)
    fields: tuple[QueryField, ...] = ("name", "storey", "type_name")
    order_by: Literal["class", "name", "storey"] = "class"
    output_format: Literal["jsonl", "csv"] = "jsonl"
    limit: int = Field(default=100_000, ge=1, le=1_000_000)
    context: OperationContext | None = None

    @model_validator(mode="after")
    def unique_fields(self) -> QueryJobSpec:
        if len(self.fields) != len(set(self.fields)):
            raise ValueError("query fields must be unique")
        return self


class CommitJobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["commit"] = "commit"
    revision: RevisionRef
    change_set_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    context: OperationContext | None = None


class RestoreJobSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["restore"] = "restore"
    revision: RevisionRef
    commit_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    confirmed: Literal[True] = True
    context: OperationContext | None = None


JobSpec = Annotated[
    ValidationJobSpec | QueryJobSpec | CommitJobSpec | RestoreJobSpec,
    Field(discriminator="kind"),
]


class JobEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ts: datetime
    type: str
    progress: int = Field(ge=0, le=100)
    message: str


class JobFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    hint: str = ""


class JobRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str = Field(pattern=r"^job-[0-9a-f]{16}$")
    kind: Literal["validation", "query", "commit", "restore"]
    state: JobState
    created_at: datetime
    updated_at: datetime
    progress: int = Field(ge=0, le=100)
    message: str
    spec: JobSpec
    phase: str = "queued"
    cancellable: bool = True
    transaction_id: str | None = None
    events: tuple[JobEvent, ...] = ()
    worker: dict[str, object] = Field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    summary: dict[str, object] = Field(default_factory=dict)
    failure: JobFailure | None = None
    cancel_requested: bool = False

    @model_validator(mode="after")
    def validate_kind(self) -> JobRecord:
        if self.kind != self.spec.kind:
            raise ValueError("job kind does not match its discriminated spec")
        return self
