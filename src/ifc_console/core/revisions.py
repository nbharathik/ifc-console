"""Revision identity contracts for optimistic automation."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

RevisionId = Annotated[str, Field(min_length=3)]


class RevisionRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    workspace_id: str
    model_id: str
    revision_id: RevisionId
    content_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
