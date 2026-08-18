from __future__ import annotations

from pathlib import Path

import pytest

from examples.sdk.property_agent.app import _review_and_commit, build_property_source
from ifc_console.runtime import LocalRuntime


@pytest.mark.asyncio
async def test_property_agent_tool_creates_only_a_preview(
    tmp_path: Path,
    work_model: Path,
):
    proposals: list[str] = []
    async with await LocalRuntime.open(work_model, home=tmp_path / "home") as runtime:
        wall = (await runtime.workspace.search("Wall-1"))[0]
        source = build_property_source(
            runtime,
            pset_name="Company_ElementData",
            property_name="Thickness",
            proposed_change_sets=proposals,
        )
        tools = await runtime.tools("property__propose_thickness", sources=(source,))

        result = await tools.call(
            "property__propose_thickness",
            {"global_ids": [wall["global_id"]], "thickness": 200.0},
        )

        assert result["ok"] is True
        assert result["data"]["model_length_unit"] == "MILLIMETRE"
        assert result["data"]["host_action_required"]
        assert len(proposals) == 1
        assert runtime.mode == "ask"
        assert runtime.workbench.model["dirty"] is False


@pytest.mark.asyncio
async def test_property_tools_bind_to_langchain_when_available_in_the_app_environment(
    tmp_path: Path,
    work_model: Path,
):
    pytest.importorskip("langchain")
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    class ToolCallingFake(FakeMessagesListChatModel):
        def bind_tools(self, _tools, **_kwargs):
            return self

    async with await LocalRuntime.open(work_model, home=tmp_path / "home") as runtime:
        source = build_property_source(
            runtime,
            pset_name="Company_ElementData",
            property_name="Thickness",
        )
        toolset = await runtime.tools("property__propose_thickness", sources=(source,))
        agent = create_agent(
            model=ToolCallingFake(responses=[AIMessage(content="Ready.")]),
            tools=toolset.as_langchain_tools(),
        )

        result = await agent.ainvoke({"messages": [{"role": "user", "content": "Inspect Wall-1"}]})

        assert result["messages"][-1].text == "Ready."


@pytest.mark.asyncio
async def test_property_agent_commit_stays_in_host_code(
    tmp_path: Path,
    work_model: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    proposals: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    async with await LocalRuntime.open(work_model, home=tmp_path / "home") as runtime:
        wall = (await runtime.workspace.search("Wall-1"))[0]
        source = build_property_source(
            runtime,
            pset_name="Company_ElementData",
            property_name="Thickness",
            proposed_change_sets=proposals,
        )
        tools = await runtime.tools("property__propose_thickness", sources=(source,))
        preview = await tools.call(
            "property__propose_thickness",
            {"global_ids": [wall["global_id"]], "thickness": 200.0},
        )

        assert preview["ok"] is True
        await _review_and_commit(runtime, proposals[0])

        properties = await runtime.workbench.psets(wall["global_id"])
        assert properties["results"][0]["psets"]["Company_ElementData"]["Thickness"] == 200.0
        assert runtime.mode == "ask"
