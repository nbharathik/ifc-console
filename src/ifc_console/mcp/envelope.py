"""Response envelope: every tool returns one {ok, data|error, meta} object.

Tools return the Envelope model, so the SDK emits machine-readable
structuredContent plus an outputSchema while keeping the JSON text block for
text-only clients. Errors are returned as data (ok:false), never as MCP
protocol errors, so the LLM can read `hint` and self-correct. Every error we
construct carries a hint.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

# The public error-code registry. Appending is additive; renaming or removing
# a code is a breaking change (enforced by the API snapshot test).
ERROR_CODES = (
    "ASK_MODE_BLOCKED",
    "EXEC_BLOCKED",
    "EXEC_ERROR",
    "EXEC_TIMEOUT",
    "EXTRA_NOT_INSTALLED",
    "FILE_EXISTS",
    "FILE_NOT_FOUND",
    "INTERNAL_ERROR",
    "INVALID_INPUT",
    "INVALID_QUERY",
    "MODEL_BUSY",
    "MODEL_TOO_LARGE",
    "NOT_FOUND",
    "NO_MODEL_LOADED",
    "PATH_NOT_ALLOWED",
    "RESULT_TOO_LARGE",
    "UNSAVED_CHANGES",
    "VIEWER_ERROR",
    "VIEWER_NOT_CONNECTED",
    "VIEWER_TIMEOUT",
)


class ToolError(Exception):
    """Raise inside a tool handler; the wrapper renders it as an err envelope."""

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


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def _jsonable(obj: Any) -> Any:
    """Force JSON-native values (entities and paths become strings)."""
    return json.loads(json.dumps(obj, ensure_ascii=False, default=str))


def ok(
    data: dict[str, Any], meta: dict[str, Any], *, char_limit: int = 40_000, **extra_meta
) -> Envelope:
    payload = _jsonable(data)
    merged = _jsonable({**meta, **extra_meta})
    dumped = _dump({"ok": True, "data": payload, "meta": merged})
    if len(dumped) > char_limit:
        payload = {
            "preview": dumped[:char_limit],
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
