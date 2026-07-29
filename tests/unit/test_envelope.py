"""Envelope invariants (plan 03 §1.2), now as the structured-output model."""

from __future__ import annotations

from ifc_console.mcp.envelope import ERROR_CODES, Envelope, ToolError, err, from_tool_error, ok


def test_ok_shape() -> None:
    out = ok({"x": 1}, {"mode": "ask", "model": "a.ifc"}, total=3).model_dump()
    assert out["ok"] is True
    assert out["data"] == {"x": 1}
    assert out["meta"]["mode"] == "ask"
    assert out["meta"]["total"] == 3
    assert out["error"] is None


def test_ok_truncates_oversized_payload() -> None:
    big = {"blob": "x" * 5000}
    out = ok(big, {"mode": "edit"}, char_limit=1000).model_dump()
    assert out["meta"]["truncated"] is True
    assert "preview" in out["data"]


def test_ok_makes_arbitrary_values_serializable() -> None:
    class Odd:
        def __str__(self) -> str:
            return "odd-thing"

    out = ok({"value": Odd()}, {"mode": "ask"})
    assert out.data == {"value": "odd-thing"}
    out.model_dump_json()  # must never raise


def test_err_has_mandatory_hint() -> None:
    out = err("NO_MODEL_LOADED", "none", "open one", {"mode": "ask"}).model_dump()
    assert out["ok"] is False
    assert out["error"]["code"] == "NO_MODEL_LOADED"
    assert out["error"]["hint"] == "open one"


def test_from_tool_error_carries_data() -> None:
    exc = ToolError("INVALID_QUERY", "bad", "fix it", data={"syntax_help": "..."})
    out = from_tool_error(exc, {"mode": "ask"}).model_dump()
    assert out["error"]["code"] == "INVALID_QUERY"
    assert out["data"]["syntax_help"] == "..."
    assert out["meta"]["mode"] == "ask"


def test_error_code_registry_is_sorted_and_unique() -> None:
    assert list(ERROR_CODES) == sorted(set(ERROR_CODES))


def test_envelope_schema_names_the_contract() -> None:
    schema = Envelope.model_json_schema()
    assert set(schema["properties"]) == {"ok", "data", "error", "meta"}
