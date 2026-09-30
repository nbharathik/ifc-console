"""Results are one text block of one-line JSON, and no tool publishes an outputSchema.

A published output schema costs every listing thousands of characters and makes
the transport send each result twice, as text and as structured content. The
text block carries the whole {ok, data, error, meta} envelope, so nothing is
reachable only through structuredContent, and the operation registry keeps the
declared data shapes for callers that want them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_a_result_is_one_line_of_json_in_one_text_block(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    result = await h.session.call_tool("get_session_status", {})

    assert result.structuredContent is None
    assert len(result.content) == 1
    text = result.content[0].text
    assert "\n" not in text
    payload = json.loads(text)
    assert payload["ok"] is True
    assert payload["data"]["model"]["loaded"] is True
    assert payload["meta"]["mode"] == "ask"


async def test_an_error_is_readable_from_the_text_block(harness_factory):
    """A failure has to be machine readable from the text block alone, because
    that is the only channel every tool shares."""
    h = await harness_factory(model=None)
    result = await h.session.call_tool("validate_model", {})
    payload = json.loads(result.content[0].text)

    assert payload["ok"] is False
    assert payload["error"]["code"] == "NO_MODEL_LOADED"
    assert payload["error"]["hint"]
    assert result.structuredContent is None


async def test_no_tool_advertises_an_output_schema(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    listed = await h.session.list_tools()

    assert all(tool.outputSchema is None for tool in listed.tools)


async def test_the_registry_still_declares_the_data_shapes(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    specs = h.core.operations

    assert specs.require("query_elements").data_schema is not None
    assert specs.require("get_session_status").data_schema is not None
