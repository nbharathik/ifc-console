"""Structured answers, parallel read-only rounds, describe(), and the fakes."""

from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel, Field

from ifc_console.agents.agent import Agent, AgentRunError
from ifc_console.core.operations import OperationAnnotations
from ifc_console.testing import (
    RecordingThreadStore,
    ScriptedAgentModel,
    error_envelope,
    ok_envelope,
    text_round,
    tool_call_round,
)
from ifc_console.toolsets import FunctionToolSource, Toolset

pytestmark = pytest.mark.asyncio

READ_ONLY = OperationAnnotations(readOnlyHint=True, destructiveHint=False)


class Report(BaseModel):
    element: str
    value: float = Field(gt=0)
    unit: str


async def build_tools(delay: float = 0.0) -> Toolset:
    source = FunctionToolSource(namespace="test")

    @source.tool(annotations=READ_ONLY)
    async def measure(target: str) -> dict:
        if delay:
            await asyncio.sleep(delay)
        return ok_envelope({"target": target, "value": 200.0})

    @source.tool(annotations=READ_ONLY)
    async def lookup(term: str) -> dict:
        if delay:
            await asyncio.sleep(delay)
        return ok_envelope({"term": term})

    return await Toolset.build(source)


def agent_for(model: ScriptedAgentModel, tools: Toolset, **kwargs) -> Agent:
    return Agent(
        name="test-agent",
        model=model,
        tools=tools,
        instructions="Test instructions.",
        **kwargs,
    )


class TestResponseModel:
    async def test_valid_answer_becomes_data(self):
        tools = await build_tools()
        model = ScriptedAgentModel(
            [text_round('{"element": "Wall-1", "value": 200.0, "unit": "MILLIMETRE"}')]
        )
        agent = agent_for(model, tools)
        result = await agent.run("measure", response_model=Report)
        assert isinstance(result.data, Report)
        assert result.data.value == 200.0
        # the schema travels in the prompt
        assert "json" in model.turns[0]["messages"][-1].text.lower()

    async def test_fenced_json_is_accepted(self):
        tools = await build_tools()
        model = ScriptedAgentModel(
            [text_round('```json\n{"element": "W", "value": 1.0, "unit": "m"}\n```')]
        )
        result = await agent_for(model, tools).run("measure", response_model=Report)
        assert result.data.element == "W"

    async def test_invalid_answer_is_retried_once_with_the_error(self):
        tools = await build_tools()
        model = ScriptedAgentModel(
            [
                text_round('{"element": "Wall-1", "value": -5, "unit": "mm"}'),
                text_round('{"element": "Wall-1", "value": 5, "unit": "mm"}'),
            ]
        )
        result = await agent_for(model, tools).run("measure", response_model=Report)
        assert result.data.value == 5
        assert len(model.turns) == 2
        assert "did not validate" in model.turns[1]["messages"][-1].text

    async def test_two_bad_answers_fail_clearly(self):
        tools = await build_tools()
        model = ScriptedAgentModel([text_round("no json"), text_round("still none")])
        with pytest.raises(AgentRunError) as excinfo:
            await agent_for(model, tools).run("measure", response_model=Report)
        assert "Report" in str(excinfo.value)


class TestParallelReadOnly:
    async def test_read_only_round_runs_concurrently(self):
        tools = await build_tools(delay=0.15)
        model = ScriptedAgentModel(
            [
                tool_call_round(
                    {"name": "test__measure", "arguments": '{"target": "a"}'},
                    {"name": "test__lookup", "arguments": '{"term": "b"}'},
                ),
                text_round("done"),
            ]
        )
        agent = agent_for(model, tools)
        events = []
        started = time.perf_counter()
        async for event in agent.stream("go"):
            events.append(event)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.28, f"round did not parallelize ({elapsed:.2f}s)"
        kinds = [e.type for e in events if e.type.startswith("tool_call")]
        assert kinds == [
            "tool_call_started",
            "tool_call_started",
            "tool_call_finished",
            "tool_call_finished",
        ]
        finished = [e for e in events if e.type == "tool_call_finished"]
        assert all(e.result["ok"] for e in finished)

    async def test_opt_out_serializes_again(self):
        from ifc_console.agents.models import AgentLimits

        tools = await build_tools(delay=0.1)
        model = ScriptedAgentModel(
            [
                tool_call_round(
                    {"name": "test__measure", "arguments": '{"target": "a"}'},
                    {"name": "test__lookup", "arguments": '{"term": "b"}'},
                ),
                text_round("done"),
            ]
        )
        agent = agent_for(model, tools, limits=AgentLimits(parallel_read_only=False))
        started = time.perf_counter()
        await agent.run("go")
        assert time.perf_counter() - started >= 0.2

    async def test_mixed_rounds_fall_back_to_sequential(self):
        """A round with one unknown tool must keep the sequential error path."""
        tools = await build_tools()
        model = ScriptedAgentModel(
            [
                tool_call_round(
                    {"name": "test__measure", "arguments": '{"target": "a"}'},
                    {"name": "test__nope", "arguments": "{}"},
                ),
                text_round("done"),
            ]
        )
        result = await agent_for(model, tools).run("go")
        outcomes = {record.name: record.ok for record in result.tool_calls}
        assert outcomes["test__measure"] is True
        assert outcomes["test__nope"] is False


class TestDescribe:
    async def test_lines_carry_name_first_sentence_and_approval(self):
        source = FunctionToolSource(namespace="co")

        @source.tool(annotations=READ_ONLY)
        async def read_thing() -> dict:
            """Reads the thing. Second sentence is dropped."""
            return ok_envelope()

        @source.tool(requires_approval=True)
        async def write_thing() -> dict:
            """Writes the thing."""
            return ok_envelope()

        tools = await Toolset.build(source)
        text = tools.describe()
        assert "- co__read_thing: Reads the thing." in text
        assert "Second sentence" not in text
        assert "- co__write_thing: Writes the thing. (needs host approval)" in text


class TestFakes:
    async def test_recording_store_and_envelopes(self):
        tools = await build_tools()
        store = RecordingThreadStore()
        model = ScriptedAgentModel([text_round("hello")])
        agent = agent_for(model, tools, thread_store=store)
        result = await agent.run("hi", thread_id="t-1")
        assert result.text == "hello"
        assert store.saves and store.saves[-1][0] == "t-1"
        assert error_envelope("X", "y")["error"]["code"] == "X"

    async def test_exhausted_script_fails_the_run_visibly(self):
        tools = await build_tools()
        model = ScriptedAgentModel([])
        with pytest.raises(AgentRunError) as excinfo:
            await agent_for(model, tools).run("hi")
        assert "ran out of rounds" in str(excinfo.value)
