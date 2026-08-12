"""Explicit workspace, model, revision, and invocation context."""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ifc_console.core.capabilities import Authority


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

    version: Literal["1"] = "1"
    correlation_id: str
    workspace_id: str
    transport: str
    mode: str
    model_id: str | None = None
    revision_id: str | None = None
    actor: str = "local-user"
    client: str = "ifc-console"
    authority: Authority = "tool"
    operation: str | None = None
    request_id: str | None = None
    job_id: str | None = None


_CURRENT_OPERATION_CONTEXT: ContextVar[OperationContext | None] = ContextVar(
    "ifc_console_operation_context", default=None
)


def new_correlation_id() -> str:
    return f"corr-{secrets.token_hex(16)}"


def current_operation_context() -> OperationContext | None:
    return _CURRENT_OPERATION_CONTEXT.get()


@contextmanager
def bind_operation_context(context: OperationContext) -> Iterator[OperationContext]:
    token = _CURRENT_OPERATION_CONTEXT.set(context)
    try:
        yield context
    finally:
        _CURRENT_OPERATION_CONTEXT.reset(token)
