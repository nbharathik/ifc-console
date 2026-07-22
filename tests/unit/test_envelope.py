"""Envelope invariants (plan 03 §1.2)."""

from __future__ import annotations

import json

from ifc_console.mcp.envelope import ToolError, err, from_tool_error, ok


def test_ok_shape() -> None:
    out = json.loads(ok({"x": 1}, {"mode": "ask", "model": "a.ifc"}, total=3))
    assert out["ok"] is True
    assert out["data"] == {"x": 1}
    assert out["meta"]["mode"] == "ask"
    assert out["meta"]["total"] == 3


def test_ok_truncates_oversized_payload() -> None:
    big = {"blob": "x" * 5000}
    out = json.loads(ok(big, {"mode": "edit"}, char_limit=1000))
    assert out["meta"]["truncated"] is True
    assert "preview" in out["data"]


def test_err_has_mandatory_hint() -> None:
    out = json.loads(err("NO_MODEL_LOADED", "none", "open one", {"mode": "ask"}))
    assert out["ok"] is False
    assert out["error"]["code"] == "NO_MODEL_LOADED"
    assert out["error"]["hint"] == "open one"


def test_from_tool_error_carries_data() -> None:
    exc = ToolError("INVALID_QUERY", "bad", "fix it", data={"syntax_help": "..."})
    out = json.loads(from_tool_error(exc, {"mode": "ask"}))
    assert out["error"]["code"] == "INVALID_QUERY"
    assert out["data"]["syntax_help"] == "..."
    assert out["meta"]["mode"] == "ask"
