from __future__ import annotations

from types import SimpleNamespace

import pytest

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
            meta={"tags": ["erp"]},
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
    result = await tools.call("erp__lookup", {"id": "A-42"})
    assert result["data"] == {"record": "A-42"}


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
