"""Explicit workspace, model, revision, and operation context."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    model_id: str | None = None
    revision_id: str | None = None
    name: str | None = None
    schema_name: str | None = None
    dirty: bool = False
    content_sha256: str | None = None


class WorkspaceContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str
    active_model: ModelContext
    attached_model_ids: tuple[str, ...] = ()


class OperationContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace_id: str
    transport: str
    mode: str
    model_id: str | None = None
    revision_id: str | None = None
    actor: str = "local-user"
