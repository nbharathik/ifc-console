"""Typed references to durable outputs produced by operations and jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ifc_console.core.revisions import RevisionRef


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    name: str
    kind: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
    producer: str
    revision: RevisionRef | None = None
    correlation_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)
    references: tuple[str, ...] = Field(default=())

    @field_validator("references")
    @classmethod
    def validate_references(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for artifact_id in value:
            prefix, separator, digest = artifact_id.partition(":")
            valid = prefix == "sha256" and separator == ":" and len(digest) == 64
            if valid:
                try:
                    valid = digest == digest.lower() and int(digest, 16) >= 0
                except ValueError:
                    valid = False
            if not valid:
                raise ValueError(f"invalid referenced artifact ID {artifact_id!r}")
        return value


class ArtifactGCPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    cutoff: datetime
    older_than_days: int = Field(ge=1)
    scanned_count: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    root_count: int = Field(ge=0)
    candidate_count: int = Field(ge=0)
    candidate_bytes: int = Field(ge=0)
    candidate_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ArtifactGCResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    completed_at: datetime
    deleted_count: int = Field(ge=0)
    deleted_bytes: int = Field(ge=0)
    deleted_ids: tuple[str, ...] = ()
    plan: ArtifactGCPlan
