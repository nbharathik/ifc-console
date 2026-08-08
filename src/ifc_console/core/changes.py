"""Typed contracts for previewed model changes and verified commits."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.jobs import SourceFileRef
from ifc_console.core.revisions import RevisionRef

IfcScalar = str | int | float | bool | None


class PropertyValueChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["property_value"] = "property_value"
    global_id: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    entity_name: str | None = None
    pset_name: str = Field(min_length=1)
    property_name: str = Field(min_length=1)
    pset_id: int = Field(gt=0)
    property_id: int = Field(gt=0)
    nominal_type: str = Field(min_length=1)
    before: IfcScalar
    after: IfcScalar


class ChangeSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    operation: Literal["property.set"] = "property.set"
    created_at: datetime
    revision: RevisionRef
    source: SourceFileRef
    changes: tuple[PropertyValueChange, ...] = Field(min_length=1)
    warnings: tuple[str, ...] = ()


class ChangeSetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_set_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    change_set: ChangeSet
    artifact: ArtifactRef


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    change_set_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    revision: RevisionRef
    approved_by: str = Field(min_length=1, max_length=200)
    approved_at: datetime
    reason: str = Field(default="", max_length=1000)


class ApprovalRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval: Approval
    artifact: ArtifactRef


class CommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    change_set_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_path: str
    committed_at: datetime
    revision_before: RevisionRef
    revision_after: RevisionRef
    previous_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_artifact: ArtifactRef
    changed_global_ids: tuple[str, ...]
    schema_valid: bool
    schema_issue_count: int = Field(ge=0)
    worker: dict[str, object] = Field(default_factory=dict)


class CommitRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result: CommitResult
    artifact: ArtifactRef


class RestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    commit_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    target_path: str
    restored_at: datetime
    revision_before: RevisionRef
    revision_after: RevisionRef
    replaced_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    restored_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    safety_artifact: ArtifactRef
    schema_valid: bool
    schema_issue_count: int = Field(ge=0)
    worker: dict[str, object] = Field(default_factory=dict)


class RestoreRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    restore_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    result: RestoreResult
    artifact: ArtifactRef
