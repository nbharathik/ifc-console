"""Response envelope: every tool returns one JSON object.

Errors are returned as data (ok:false), never as MCP protocol errors, so the
LLM can read `hint` and self-correct. Every error we construct carries a hint.
"""

from __future__ import annotations

import json
from typing import Any


class ToolError(Exception):
    """Raise inside a tool handler; the wrapper renders it as an err envelope."""

    def __init__(self, code: str, message: str, hint: str, data: dict[str, Any] | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.hint = hint
        self.data = data


def _dump(obj: dict[str, Any]) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, default=str)


def ok(data: Any, meta: dict[str, Any], *, char_limit: int = 40_000, **extra_meta) -> str:
    env = {"ok": True, "data": data, "meta": {**meta, **extra_meta}}
    dumped = _dump(env)
    if len(dumped) > char_limit:
        env["data"] = {
            "preview": dumped[:char_limit],
            "note": "result truncated: refine the query, lower `limit`, or select fewer fields",
        }
        env["meta"]["truncated"] = True
        dumped = _dump(env)
    return dumped


def err(
    code: str,
    message: str,
    hint: str,
    meta: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> str:
    env: dict[str, Any] = {
        "ok": False,
        "error": {"code": code, "message": message, "hint": hint},
        "meta": meta,
    }
    if data:
        env["data"] = data
    return _dump(env)


def from_tool_error(exc: ToolError, meta: dict[str, Any]) -> str:
    return err(exc.code, exc.message, exc.hint, meta, data=exc.data)
