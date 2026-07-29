"""MCP resources and prompts: the non-tool protocol surface."""

from __future__ import annotations

import json
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
