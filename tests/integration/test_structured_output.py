"""Structured output: a tool advertises an outputSchema only when it declared a
bounded result shape, because a bare Envelope schema is identical for every tool
and declaring one also makes the transport send each result twice. The text
block always carries the whole {ok, data, error, meta} envelope, so nothing is
reachable only through structuredContent."""

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


async def test_an_error_is_readable_whether_or_not_a_schema_was_declared(harness_factory):
    """A failure has to be machine readable from the text block alone, because
    that is the only channel every tool shares."""
    h = await harness_factory(model=None)
    result = await h.session.call_tool("validate_model", {})
    payload = json.loads(result.content[0].text)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NO_MODEL_LOADED"
    assert payload["error"]["hint"]
    # and where a tool did declare a bounded shape, the structured copy agrees
    status = await h.session.call_tool("get_session_status", {})
    assert status.structuredContent is not None
    assert status.structuredContent == json.loads(status.content[0].text)


async def test_only_a_declared_bounded_shape_is_advertised(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    listed = await h.session.list_tools()
    schemas = {tool.name: tool.outputSchema for tool in listed.tools}

    # A tool with a small declared payload publishes it, and what it publishes
    # is the envelope wrapping that payload rather than a bare Envelope.
    status = schemas["get_session_status"]
    assert status is not None
    assert set(status.get("properties", {})) >= {"ok", "data", "error", "meta"}

    # A bulk result publishes nothing: the schema would be identical for every
    # such tool, and declaring it would double every result on the wire.
    assert schemas["query_elements"] is None
    assert schemas["get_element_geometry"] is None

    # Whatever is advertised must be worth its size.
    for name, schema in schemas.items():
        if schema is not None:
            assert len(json.dumps(schema)) <= 2_500, name
