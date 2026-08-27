"""MCP resources, prompts, and the shape of the published tool contract."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

RESOURCE_URIS = {
    "ifc://model/summary",
    "ifc://model/spatial-tree",
    "ifc://session/audit",
}

PROMPT_NAMES = {
    "model_audit",
    "qto_report",
    "explain_element",
    "find_unclassified",
    "validate_against_ids",
    "selector_help",
}


async def _read_json(session, uri: str) -> dict:
    result = await session.read_resource(uri)
    return json.loads(result.contents[0].text)


async def test_resources_listed_and_readable(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    listed = await h.session.list_resources()
    uris = {str(resource.uri) for resource in listed.resources}
    assert uris >= RESOURCE_URIS

    summary = await _read_json(h.session, "ifc://model/summary")
    assert summary["loaded"] is True
    assert summary["schema"] == "IFC4"

    tree = await _read_json(h.session, "ifc://model/spatial-tree")
    assert tree["tree"]["class"] == "IfcProject"

    audit = await _read_json(h.session, "ifc://session/audit")
    assert isinstance(audit["records"], list)


async def test_resources_degrade_without_model(harness_factory):
    h = await harness_factory(model=None)
    summary = await _read_json(h.session, "ifc://model/summary")
    assert summary["loaded"] is False
    assert "open_ifc_file" in summary["hint"]


async def test_element_resource_template(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    templates = await h.session.list_resource_templates()
    uris = {template.uriTemplate for template in templates.resourceTemplates}
    assert "ifc://element/{global_id}" in uris

    def wall_guid() -> str:
        return h.core.session.ifc.by_type("IfcWall")[0].GlobalId

    guid = await h.core.session.run(wall_guid)
    detail = await _read_json(h.session, f"ifc://element/{guid}")
    assert detail["found"] is True
    assert detail["class"] == "IfcWall"

    missing = await _read_json(h.session, "ifc://element/notaguid123")
    assert missing["found"] is False


async def test_prompt_library(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    listed = await h.session.list_prompts()
    names = {prompt.name for prompt in listed.prompts}
    assert names >= PROMPT_NAMES

    audit = await h.session.get_prompt("model_audit", {})
    text = audit.messages[0].content.text
    assert "validate_model" in text and "orient" in text

    explain = await h.session.get_prompt("explain_element", {"global_id": "abc123"})
    assert "abc123" in explain.messages[0].content.text

    helper = await h.session.get_prompt("selector_help", {})
    assert "IfcWall" in helper.messages[0].content.text


async def test_selector_help_covers_the_whole_grammar(harness_factory, work_model: Path):
    """The facets past the basic sheet are what turn a spatial or
    classification question into one line instead of hand-written code."""
    h = await harness_factory(model=work_model)
    text = (await h.session.get_prompt("selector_help", {})).messages[0].content.text

    for facet in ("classification=", "location=", "parent=", "group=", 'query:"storey.Name"'):
        assert facet in text, facet
    # the argument-name mapping the model needs to call the other tools
    assert "measure_elements" in text and "set_a" in text


async def test_every_published_selector_example_parses(minimal_ifc4_path: Path):
    """A cheat sheet the grammar rejects is worse than no cheat sheet."""
    import ifcopenshell
    import ifcopenshell.util.selector as selector_util

    from ifc_console.mcp.prompts import SELECTOR_EXTRAS

    ifc = ifcopenshell.open(str(minimal_ifc4_path))
    examples = [
        candidate
        for candidate in (
            re.split(r"\s{2,}", line.strip())[0] for line in SELECTOR_EXTRAS.splitlines()
        )
        if candidate.startswith("Ifc") or re.fullmatch(r"[0-3][A-Za-z0-9_$]{21}", candidate)
    ]
    assert len(examples) >= 15
    for example in examples:
        selector_util.filter_elements(ifc, example)


async def test_find_unclassified_prompt_stays_on_the_selector(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    text = (await h.session.get_prompt("find_unclassified", {})).messages[0].content.text

    assert "classification=NULL" in text
    assert "execute_ifc_code" not in text


async def test_only_declared_result_shapes_are_published(ask_harness):
    """A bare Envelope schema is the same for every tool, so publishing one per
    tool is pure context cost and makes FastMCP send each result twice."""
    listed = await ask_harness.session.list_tools()
    with_schema = [tool for tool in listed.tools if tool.outputSchema]

    assert 0 < len(with_schema) < len(listed.tools) // 4
    for tool in with_schema:
        assert tool.outputSchema["$defs"], tool.name
    # every remaining title restated the key it sat under
    for tool in listed.tools:
        for name, field in (tool.inputSchema.get("properties") or {}).items():
            assert "title" not in field, f"{tool.name}.{name}"
        assert "title" not in tool.inputSchema, tool.name


async def test_a_tool_accepts_the_name_another_tool_uses(ask_harness):
    """query_elements takes `query` and measure_elements takes `selector`;
    calling either with the other's name must not cost a round."""
    listed = await ask_harness.session.list_tools()
    schema = next(t.inputSchema for t in listed.tools if t.name == "query_elements")
    assert "query" in schema["properties"] and "selector" not in schema["properties"]

    canonical = await ask_harness.call("query_elements", query="IfcWall")
    aliased = await ask_harness.call("query_elements", selector="IfcWall")

    assert canonical["ok"] is True
    assert aliased["data"] == canonical["data"]
