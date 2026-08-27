from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

import ifc_console.integrations.mcp as mcp_integration
from ifc_console.integrations.mcp import McpToolSource
from ifc_console.toolsets import FunctionToolSource, Toolset


@pytest.mark.asyncio
async def test_function_tools_are_namespaced_filtered_and_validated():
    source = FunctionToolSource(namespace="company", source_id="company-checks")

    @source.tool(tags={"validation"})
    async def repeat(value: str, count: int = 1) -> dict:
        return {"value": value * count}

    tools = await Toolset.build(source)

    assert tools.names == ("company__repeat",)
    definition = tools.require("company__repeat")
    assert {"python", "company", "validation"} <= definition.tags
    assert tools.include(tags={"validation"}).names == tools.names
    assert tools.exclude("*repeat").names == ()

    result = await tools.call("company__repeat", {"value": "IFC", "count": 2})
    assert result["ok"] is True
    assert result["data"] == {"value": "IFCIFC"}
    assert result["meta"]["tool_source"] == "company-checks"

    invalid = await tools.call("company__repeat", {"value": "IFC", "extra": True})
    assert invalid["ok"] is False
    assert invalid["error"]["code"] == "INVALID_INPUT"


class RowsData(BaseModel):
    rows: list[dict[str, Any]]
    total: int


class StatusData(BaseModel):
    loaded: bool
    schema_name: str


@pytest.mark.asyncio
async def test_only_a_small_bounded_result_shape_is_published():
    """54 byte-identical Envelope schemas taught a client nothing and cost it
    thousands of characters on every listing. A schema also makes the
    transport send the result twice, which a bulk payload never repays."""
    source = FunctionToolSource(namespace="co")

    @source.tool(data_model=StatusData)
    async def described(query: str) -> dict:
        return {"ok": True, "data": {"loaded": True, "schema_name": "IFC4"}}

    @source.tool(data_model=RowsData)
    async def bulk(query: str) -> dict:
        return {"ok": True, "data": {"rows": [], "total": 0}}

    @source.tool()
    async def undeclared(query: str) -> dict:
        return {"ok": True, "data": {}}

    tools = await Toolset.build(source)

    assert tools.require("co__undeclared").output_schema is None
    assert tools.require("co__bulk").output_schema is None
    # the declared shape is still reachable, just not on the listing path
    assert tools.require("co__bulk").data_schema["properties"].keys() == {"rows", "total"}

    schema = tools.require("co__described").output_schema
    assert schema is not None
    assert "StatusData" in schema["$defs"]
    # a paged result adds a `truncation` key, so the declared shape documents
    # the payload without filtering it
    assert {"additionalProperties": True, "type": "object"} in schema["properties"]["data"]["anyOf"]


@pytest.mark.asyncio
async def test_a_truncated_payload_survives_the_declared_envelope():
    from ifc_console.core.operations import OperationRegistry

    registry = OperationRegistry()

    @registry.tool(data_model=StatusData)
    async def paged() -> dict:
        return {}

    envelope = registry.require("paged").envelope_model
    assert envelope is not None
    truncated = {"truncation": {"kept_chars": 8, "of_chars": 90}, "preview": "{'loaded'"}
    validated = envelope.model_validate({"ok": True, "data": truncated, "meta": {}})
    assert validated.model_dump(mode="json")["data"] == truncated


@pytest.mark.asyncio
async def test_input_schemas_carry_no_titles_restating_the_key():
    source = FunctionToolSource(namespace="co")

    @source.tool()
    async def probe(global_ids: list[str], max_size: int = 5) -> dict:
        return {}

    tools = await Toolset.build(source)
    schema = tools.require("co__probe").input_schema

    assert "title" not in json.dumps(schema)
    assert set(schema["properties"]) == {"global_ids", "max_size"}


@pytest.mark.asyncio
async def test_the_other_tools_name_for_an_argument_is_accepted():
    """query_elements takes `query`, measure_elements takes `selector`. The
    published schema keeps one name; both are accepted."""
    source = FunctionToolSource(namespace="co")

    @source.tool()
    async def find(query: str, limit: int = 10) -> dict:
        return {"query": query, "limit": limit}

    tools = await Toolset.build(source)

    assert set(tools.require("co__find").input_schema["properties"]) == {"query", "limit"}
    for alias in ("query", "selector", "term"):
        result = await tools.call("co__find", {alias: "IfcWall", "max_results": 2})
        assert result["data"] == {"query": "IfcWall", "limit": 2}, alias


@pytest.mark.asyncio
async def test_describe_drops_the_category_tag():
    source = FunctionToolSource(namespace="co")

    @source.tool(description="[QUERY] Count the walls. Ignored second sentence.")
    async def count_walls() -> dict:
        return {}

    tools = await Toolset.build(source)

    assert tools.describe() == "- co__count_walls: Count the walls."


@pytest.mark.asyncio
async def test_an_alias_that_is_a_real_argument_stays_unambiguous():
    from ifc_console.core.operations import argument_aliases

    assert "selector" not in argument_aliases("query", {"query", "selector"})
    assert "term" in argument_aliases("query", {"query", "selector"})


@pytest.mark.asyncio
async def test_toolset_refuses_collisions_without_namespaces():
    first = FunctionToolSource(namespace="", source_id="first")
    second = FunctionToolSource(namespace="", source_id="second")

    @first.tool(name="status")
    async def first_status() -> dict:
        return {"source": "first"}

    @second.tool(name="status")
    async def second_status() -> dict:
        return {"source": "second"}

    with pytest.raises(ValueError, match="collision"):
        await Toolset.build(first, second)


class _FakeMcpSession:
    async def list_tools(self, cursor=None):
        assert cursor is None
        tool = SimpleNamespace(
            name="lookup",
            title="Lookup",
            description="Look up a company record.",
            inputSchema={"type": "object", "properties": {"id": {"type": "string"}}},
            outputSchema={"type": "object"},
            annotations=SimpleNamespace(model_dump=lambda **_kwargs: {"readOnlyHint": True}),
            meta={
                "tags": ["erp"],
                "ifcConsole": {"requiredCapabilities": ["model.read"]},
            },
        )
        return SimpleNamespace(tools=[tool], nextCursor=None)

    async def call_tool(self, name, arguments):
        assert name == "lookup"
        return SimpleNamespace(
            structuredContent={"record": arguments["id"]},
            content=[],
            isError=False,
        )


@pytest.mark.asyncio
async def test_mcp_tools_compose_with_a_namespace():
    source = McpToolSource(_FakeMcpSession(), namespace="erp")
    tools = await Toolset.build(source)

    assert tools.names == ("erp__lookup",)
    assert "erp" in tools.require("erp__lookup").tags
    assert tools.require("erp__lookup").required_capabilities == ("model.read",)
    assert tools.require("erp__lookup").requires_approval is False
    result = await tools.call("erp__lookup", {"id": "A-42"})
    assert result["data"] == {"record": "A-42"}


@pytest.mark.asyncio
async def test_mcp_source_recovers_an_unstructured_ifc_console_envelope():
    expected = {
        "ok": False,
        "error": {
            "code": "VIEWER_NOT_CONNECTED",
            "message": "no viewer tab is connected",
            "hint": "Call open_viewer.",
        },
        "meta": {"viewer": False},
    }

    class Session:
        async def call_tool(self, name, arguments):
            assert name == "get_viewer_selection"
            assert arguments == {}
            return SimpleNamespace(
                structuredContent=None,
                content=[SimpleNamespace(text=json.dumps(expected))],
                isError=False,
            )

    result = await McpToolSource(Session()).call_tool("get_viewer_selection", {})

    assert result == expected


@pytest.mark.asyncio
async def test_mcp_source_normalizes_native_images_for_agent_vision():
    class Session:
        async def call_tool(self, name, arguments):
            assert name == "get_viewer_screenshot"
            return SimpleNamespace(
                structuredContent=None,
                content=[
                    SimpleNamespace(
                        model_dump=lambda **_kwargs: {
                            "type": "image",
                            "data": "iVBORw0KGgo=",
                            "mimeType": "image/png",
                        }
                    ),
                    SimpleNamespace(
                        model_dump=lambda **_kwargs: {
                            "type": "text",
                            "text": "viewer screenshot 1x1 png",
                        }
                    ),
                ],
                isError=False,
            )

    result = await McpToolSource(Session()).call_tool("get_viewer_screenshot", {})

    assert result["data"] == {
        "images": [{"media_type": "image/png", "data": "iVBORw0KGgo="}],
        "note": "viewer screenshot 1x1 png",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "annotations",
    [
        None,
        {"readOnlyHint": False, "destructiveHint": False},
        {"readOnlyHint": True, "destructiveHint": True},
    ],
)
async def test_mcp_tools_require_approval_unless_explicitly_read_only(annotations):
    class Session:
        async def list_tools(self, cursor=None):
            assert cursor is None
            raw_annotations = (
                None
                if annotations is None
                else SimpleNamespace(model_dump=lambda **_kwargs: annotations)
            )
            tool = SimpleNamespace(
                name="publish",
                title="Publish",
                description="Publish a record.",
                inputSchema={"type": "object"},
                outputSchema=None,
                annotations=raw_annotations,
                meta=None,
            )
            return SimpleNamespace(tools=[tool], nextCursor=None)

    tools = await Toolset.build(McpToolSource(Session(), namespace="erp"))

    assert tools.require("erp__publish").requires_approval is True


@pytest.mark.asyncio
async def test_mcp_tool_listing_rejects_a_repeated_cursor():
    cursors: list[str | None] = []

    class Session:
        async def list_tools(self, cursor=None):
            cursors.append(cursor)
            return SimpleNamespace(tools=[], nextCursor="again")

    with pytest.raises(RuntimeError, match="repeated a pagination cursor"):
        await McpToolSource(Session()).list_tools()

    assert cursors == [None, "again"]


@pytest.mark.asyncio
async def test_mcp_tool_listing_has_a_page_cap(monkeypatch):
    monkeypatch.setattr(mcp_integration, "_MAX_TOOL_PAGES", 2)
    cursors: list[str | None] = []

    class Session:
        async def list_tools(self, cursor=None):
            cursors.append(cursor)
            return SimpleNamespace(tools=[], nextCursor=f"page-{len(cursors)}")

    with pytest.raises(RuntimeError, match="exceeded 2 pages"):
        await McpToolSource(Session()).list_tools()

    assert cursors == [None, "page-1"]


@pytest.mark.asyncio
async def test_mcp_tool_listing_has_a_tool_cap(monkeypatch):
    monkeypatch.setattr(mcp_integration, "_MAX_TOOLS", 2)

    class Session:
        async def list_tools(self, cursor=None):
            assert cursor is None
            return SimpleNamespace(tools=[object(), object(), object()], nextCursor=None)

    with pytest.raises(RuntimeError, match="exceeded 2 tools"):
        await McpToolSource(Session()).list_tools()


@pytest.mark.asyncio
async def test_langchain_adapter_keeps_schema_metadata_and_structured_result():
    pytest.importorskip("langchain_core")
    source = FunctionToolSource(namespace="company", source_id="company-tools")

    @source.tool(tags={"property"})
    async def inspect_name(name: str) -> dict:
        return {"name": name}

    toolset = await Toolset.build(source)
    tool = toolset.as_langchain_tools()[0]

    assert tool.name == "company__inspect_name"
    assert "name" in tool.args_schema["properties"]
    assert tool.metadata["source"] == "company-tools"
    content, artifact = await tool.coroutine(name="Wall-1")
    assert '"Wall-1"' in content
    assert artifact["data"] == {"name": "Wall-1"}
