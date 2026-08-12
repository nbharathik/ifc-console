"""Transport-neutral operation result envelope and errors."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

ERROR_CODES = (
    "AI_SAVE_DISABLED",
    "APPROVAL_MISMATCH",
    "APPROVAL_NOT_FOUND",
    "APPROVAL_REQUIRED",
    "ARTIFACT_CORRUPT",
    "ARTIFACT_EXPORT_FAILED",
    "ARTIFACT_GC_CONFLICT",
    "ARTIFACT_GC_FAILED",
    "ARTIFACT_NOT_FOUND",
    "ARTIFACT_STORE_BUSY",
    "ARTIFACT_STORE_CORRUPT",
    "ASK_MODE_BLOCKED",
    "BATCH_NOT_FOUND",
    "BATCH_NOT_RESUMABLE",
    "BATCH_SERVICE_CLOSED",
    "BATCH_SOURCE_CHANGED",
    "BATCH_STORE_FAILED",
    "BATCH_TIMEOUT",
    "CAPABILITY_DENIED",
    "CHANGESET_INVALID",
    "CHANGESET_NOT_FOUND",
    "CHAT_FAILED",
    "COMMIT_FAILED",
    "COMMIT_NOT_FOUND",
    "CONSOLE_AUTH_FAILED",
    "CONSOLE_NOT_RUNNING",
    "EXEC_BLOCKED",
    "EXEC_ERROR",
    "EXEC_TIMEOUT",
    "EXTRA_NOT_INSTALLED",
    "FILE_EXISTS",
    "FILE_NOT_FOUND",
    "INTERNAL_ERROR",
    "INVALID_INPUT",
    "INVALID_OUTPUT",
    "INVALID_QUERY",
    "JOB_CANCELLED",
    "JOB_NOT_CANCELLABLE",
    "JOB_NOT_FOUND",
    "JOB_RESULT_INVALID",
    "JOB_SERVICE_CLOSED",
    "JOB_SPEC_INVALID",
    "JOB_TIMEOUT",
    "JOB_WORKER_FAILED",
    "KNOWLEDGE_DISABLED",
    "KNOWLEDGE_NOT_READY",
    "MODEL_BUSY",
    "MODEL_NOT_FOUND",
    "MODEL_READ_ONLY",
    "MODEL_TOO_LARGE",
    "NOT_FOUND",
    "NO_GEOMETRY",
    "NO_MATCH",
    "NO_MODEL_LOADED",
    "PATH_NOT_ALLOWED",
    "PROPERTY_NOT_FOUND",
    "RESTORE_CONFLICT",
    "RESTORE_NOT_FOUND",
    "RESULT_TOO_LARGE",
    "REVISION_CONFLICT",
    "SANDBOX_UNAVAILABLE",
    "SOURCE_CHANGED",
    "TOO_MANY_ELEMENTS",
    "TRANSACTION_INTERRUPTED",
    "TRANSACTION_JOURNAL_BUSY",
    "TRANSACTION_JOURNAL_CORRUPT",
    "TRANSACTION_JOURNAL_INVALID",
    "TRANSACTION_RECOVERY_REQUIRED",
    "UNSAVED_CHANGES",
    "VALIDATION_FAILED",
    "VIEWER_ERROR",
    "VIEWER_NOT_CONNECTED",
    "VIEWER_TIMEOUT",
    "WORKFLOW_CANCELLED",
    "WORKFLOW_DEPENDENCY_FAILED",
    "WORKFLOW_INPUT_EMPTY",
    "WORKFLOW_INPUT_LIMIT",
    "WORKFLOW_INTERRUPTED",
    "WORKFLOW_MANIFEST_INVALID",
    "WORKFLOW_MANIFEST_TOO_LARGE",
    "WORKFLOW_NOT_FOUND",
    "WORKFLOW_NOT_RESUMABLE",
    "WORKFLOW_PATH_INVALID",
    "WORKFLOW_SERVICE_CLOSED",
    "WORKFLOW_SOURCE_CHANGED",
    "WORKFLOW_STEP_FAILED",
    "WORKFLOW_STORE_CORRUPT",
    "WORKFLOW_STORE_FAILED",
    "WORKFLOW_SUPERVISOR_FAILED",
    "WORKFLOW_TIMEOUT",
    "WORKSPACE_BUDGET",
    "WORKSPACE_DISABLED",
)


class ToolError(Exception):
    """An operation failure that can be returned to any interface."""

    def __init__(self, code: str, message: str, hint: str, data: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data


class ErrorInfo(BaseModel):
    code: str = Field(description="Stable machine-readable code; see ERROR_CODES.")
    message: str = Field(description="What went wrong, in one sentence.")
    hint: str = Field(description="What to do next; follow it instead of retrying blindly.")


class Envelope(BaseModel):
    """The public tool-result contract."""

    ok: bool
    data: dict[str, Any] | None = None
    error: ErrorInfo | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _jsonable(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def ok(
    data: dict[str, Any], meta: dict[str, Any], *, char_limit: int = 40_000, **extra_meta: Any
) -> Envelope:
    payload = _jsonable(data)
    merged = _jsonable({**meta, **extra_meta})
    rendered = dump({"ok": True, "data": payload, "meta": merged})
    from ifc_console.policy.untrusted import NOTE, scan

    suspicious = scan(rendered)
    if suspicious:
        merged["injection_warning"] = {"note": NOTE, "excerpts": suspicious}
        rendered = dump({"ok": True, "data": payload, "meta": merged})
    if len(rendered) > char_limit:
        payload = {
            "preview": rendered[:char_limit],
            "note": "result truncated: refine the query, lower `limit`, or select fewer fields",
        }
        merged["truncated"] = True
    return Envelope(ok=True, data=payload, meta=merged)


def err(
    code: str,
    message: str,
    hint: str,
    meta: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> Envelope:
    return Envelope(
        ok=False,
        error=ErrorInfo(code=code, message=message, hint=hint),
        data=_jsonable(data) if data else None,
        meta=_jsonable(meta),
    )


def from_tool_error(exc: ToolError, meta: dict[str, Any]) -> Envelope:
    return err(exc.code, exc.message, exc.hint, meta, data=exc.data)
