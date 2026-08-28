"""Transport-neutral operation result envelope and errors."""

from __future__ import annotations

import json
from collections.abc import Callable
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
    "BATCH_CANCELLED",
    "BATCH_CHILD_FAILED",
    "BATCH_INTERRUPTED",
    "BATCH_NOT_FOUND",
    "BATCH_NOT_RESUMABLE",
    "BATCH_SERVICE_CLOSED",
    "BATCH_SOURCE_CHANGED",
    "BATCH_STORE_FAILED",
    "BATCH_SUPERVISOR_FAILED",
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
    "FRAME_UNAVAILABLE",
    "GEOMETRY_ANALYSIS_FAILED",
    "INTERNAL_ERROR",
    "INVALID_GEOMETRY",
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
    "STORE_BUSY",
    "TOO_MANY_ELEMENTS",
    "TRANSACTION_INTERRUPTED",
    "TRANSACTION_JOURNAL_BUSY",
    "TRANSACTION_JOURNAL_CORRUPT",
    "TRANSACTION_JOURNAL_INVALID",
    "TRANSACTION_RECOVERY_REQUIRED",
    "UNSAVED_CHANGES",
    "VALIDATION_FAILED",
    "VIEWER_BUSY",
    "VIEWER_ERROR",
    "VIEWER_NOT_CONNECTED",
    "VIEWER_TIMEOUT",
    "VIEWER_UNAVAILABLE",
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


# Bulk results carry their rows under one of these names. Paging that list
# beats handing back a clipped string the caller can no longer parse.
_PAGE_KEYS = ("rows", "elements", "results", "hits", "groups", "measurements", "issues")

_PREVIEW_NOTE = "result truncated: refine the query, lower `limit`, or select fewer fields"


def _render(payload: dict[str, Any], meta: dict[str, Any]) -> str:
    return dump({"ok": True, "data": payload, "meta": meta})


def _page_key(payload: dict[str, Any]) -> str | None:
    """The one list worth paging, or None when the shape is not a bulk result."""
    lists = [key for key, value in payload.items() if isinstance(value, list) and value]
    for name in _PAGE_KEYS:
        if name in lists:
            return name
    return lists[0] if len(lists) == 1 else None


def _largest_fitting(build: Callable[[int], tuple], high: int, char_limit: int) -> tuple | None:
    """Binary-search the biggest prefix whose rendered envelope fits.

    Prefix length only ever grows the rendering, so the search is sound.
    """
    low, best = 0, None
    while low <= high:
        mid = (low + high) // 2
        payload, meta = build(mid)
        if len(_render(payload, meta)) <= char_limit:
            best = (payload, meta)
            low = mid + 1
        else:
            high = mid - 1
    return best


def _paged(
    payload: dict[str, Any], merged: dict[str, Any], key: str, char_limit: int
) -> tuple | None:
    """Keep the rows that fit and say exactly where the next page starts.

    `truncation` is the first key in data on purpose: consumers clip tool
    results from the front, so the signal has to arrive before the rows.
    """
    items = payload[key]
    # Only a tool that reports its offset can be resumed by one; the rest are
    # told to ask for a smaller batch instead of a page that does not exist.
    paginated = isinstance(merged.get("offset"), int)
    offset = merged["offset"] if paginated else 0

    def build(kept: int) -> tuple[dict[str, Any], dict[str, Any]]:
        cut: dict[str, Any] = {"key": key, "kept": kept, "of": len(items)}
        if paginated:
            cut["next_offset"] = offset + kept
            cut["retry"] = (
                f"the result did not fit; call the same tool with offset="
                f"{offset + kept} for the next page, or select fewer fields"
            )
        else:
            cut["retry"] = "the result did not fit; ask for the remaining items in a smaller batch"
        head = {"truncation": cut}
        body = {k: (v[:kept] if k == key else v) for k, v in payload.items()}
        meta = {**merged, "truncated": True}
        if "returned" in meta:
            meta["returned"] = kept
        return {**head, **body}, meta

    # the full list is known not to fit, and the header only adds to it
    return _largest_fitting(build, len(items) - 1, char_limit)


def _previewed(
    payload: dict[str, Any], merged: dict[str, Any], char_limit: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Fallback for shapes with no pageable list.

    The preview is sized against the finished envelope, so char_limit is a
    ceiling rather than a floor.
    """
    source = dump(payload)
    meta = {**merged, "truncated": True}
    pinned: dict[str, Any] = {}
    change_set = payload.get("change_set")
    if isinstance(change_set, dict) and isinstance(change_set.get("change_set_id"), str):
        # Keep the approval handle even when the inline ChangeSet does not fit.
        pinned["change_set"] = {"change_set_id": change_set["change_set_id"]}

    def build(kept: int) -> tuple[dict[str, Any], dict[str, Any]]:
        head = {"truncation": {"kept_chars": kept, "of_chars": len(source), "note": _PREVIEW_NOTE}}
        return {**head, **pinned, "preview": source[:kept]}, meta

    return _largest_fitting(build, len(source), char_limit) or build(0)


def ok(
    data: dict[str, Any], meta: dict[str, Any], *, char_limit: int = 40_000, **extra_meta: Any
) -> Envelope:
    payload = _jsonable(data)
    merged = _jsonable({**meta, **extra_meta})
    rendered = _render(payload, merged)
    from ifc_console.policy.untrusted import NOTE, scan

    suspicious = scan(rendered)
    if suspicious:
        merged["injection_warning"] = {"note": NOTE, "excerpts": suspicious}
        rendered = _render(payload, merged)
    if len(rendered) > char_limit:
        key = _page_key(payload)
        paged = _paged(payload, merged, key, char_limit) if key else None
        payload, merged = paged or _previewed(payload, merged, char_limit)
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
