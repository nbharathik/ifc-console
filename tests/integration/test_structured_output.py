"""Structured output: every tool result carries structuredContent and an
outputSchema advertising the {ok, data, error, meta} envelope."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_results_carry_structured_content_and_text(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    result = await h.session.call_tool("get_session_status", {})
    structured = result.structuredContent
    assert structured is not None
    assert structured["ok"] is True
    assert structured["data"]["model"]["loaded"] is True
    assert structured["meta"]["mode"] == "ask"
    # the text block still carries the same envelope for text-only clients
    text_payload = json.loads(result.content[0].text)
    assert text_payload["ok"] is True
    assert text_payload["data"]["model"]["loaded"] is True


async def test_error_envelopes_are_structured_too(harness_factory):
    h = await harness_factory(model=None)
    result = await h.session.call_tool("validate_model", {})
    structured = result.structuredContent
    assert structured is not None
    assert structured["ok"] is False
    assert structured["error"]["code"] == "NO_MODEL_LOADED"
    assert structured["error"]["hint"]


async def test_tools_advertise_the_envelope_output_schema(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    listed = await h.session.list_tools()
    by_name = {tool.name: tool for tool in listed.tools}
    schema = by_name["query_elements"].outputSchema
    assert schema is not None
    assert set(schema.get("properties", {})) >= {"ok", "data", "error", "meta"}
    # every enveloped tool advertises the same contract
    missing = [
        tool.name
        for tool in listed.tools
        if tool.name
        not in {
            "get_viewer_screenshot",
            "get_project_reference_image",
            "get_project_document_page",
        }
        and tool.outputSchema is None
    ]
    assert missing == []
