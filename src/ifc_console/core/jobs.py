"""Job contracts shared by embedded and remote automation clients."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.core.artifacts import ArtifactRef
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

    kind: str = Field(default="validation", pattern="^validation$")
    revision: RevisionRef
    model: SourceFileRef
    ids_files: tuple[SourceFileRef, ...] = ()
    express_rules: bool = False
    max_issues: int = Field(default=200, ge=1, le=2000)


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
    kind: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    progress: int = Field(ge=0, le=100)
    message: str
    spec: ValidationJobSpec
    events: tuple[JobEvent, ...] = ()
    worker: dict[str, object] = Field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    summary: dict[str, object] = Field(default_factory=dict)
    failure: JobFailure | None = None
    cancel_requested: bool = False
