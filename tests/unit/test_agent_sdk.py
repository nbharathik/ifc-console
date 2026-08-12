from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pytest

from ifc_console.agents import (
    Agent,
    AgentLimits,
    AgentMessage,
    AgentToolSource,
    CallbackApprovalHandler,
    InMemoryThreadStore,
    JsonThreadStore,
    ThreadStoreError,
)
from ifc_console.toolsets import FunctionToolSource, ToolDefinition, Toolset


class ScriptedModel:
    provider_id = "test"
    model_id = "scripted"

    def __init__(self, rounds: Sequence[Sequence[Mapping[str, Any]]]) -> None:
        self.rounds = iter(rounds)

    async def stream(
        self,
        *,
        messages: Sequence[AgentMessage],
        tools: Sequence[ToolDefinition],
        system: str,
        options: Mapping[str, Any],
    ) -> AsyncIterator[Mapping[str, Any]]:
        assert system
        assert tools
        assert messages
        for event in next(self.rounds):
            yield event


class AnswerModel:
    provider_id = "test"
    model_id = "answer"

    async def stream(self, **_kwargs):
        yield {"type": "content", "text": "Fire review complete."}


@pytest.mark.asyncio
async def test_agent_runs_tools_streams_events_and_persists_a_thread():
    source = FunctionToolSource(namespace="company")

    @source.tool(tags={"calculation"})
    async def add(left: int, right: int) -> dict:
        return {"total": left + right}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "sum-1",
                            "name": "company__add",
                            "arguments": '{"left": 2, "right": 3}',
                        }
                    ],
                }
            ],
            [
                {"type": "content", "text": "The total is 5."},
                {"type": "usage", "in": 80, "out": 7},
            ],
        ]
    )
    store = InMemoryThreadStore()
    agent = Agent(
        name="calculator",
        model=model,
        tools=tools,
        instructions="Use tools for arithmetic.",
        thread_store=store,
    )

    events = [event async for event in agent.stream("Add them", thread_id="thread-1")]
    completed = events[-1].run_result

    assert [event.type for event in events] == [
        "run_started",
        "tool_call_started",
        "tool_call_finished",
        "text_delta",
        "usage",
        "run_completed",
    ]
    assert completed is not None
    assert completed.text == "The total is 5."
    assert completed.tool_calls[0].result["data"] == {"total": 5}
    assert [message.role for message in await store.load("thread-1")] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


@pytest.mark.asyncio
async def test_agent_requests_host_approval_for_protected_tools():
    source = FunctionToolSource(namespace="company")
    executed = False

    @source.tool(requires_approval=True)
    async def publish() -> dict:
        nonlocal executed
        executed = True
        return {"published": True}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "publish-1",
                            "name": "company__publish",
                            "arguments": "{}",
                        }
                    ],
                }
            ],
            [{"type": "content", "text": "Publishing was not approved."}],
        ]
    )
    agent = Agent(
        name="publisher",
        model=model,
        tools=tools,
        instructions="Publish only with approval.",
        approval_handler=CallbackApprovalHandler(lambda _request: False),
        limits=AgentLimits(max_tool_rounds=2),
    )

    events = [event async for event in agent.stream("Publish this")]

    assert "approval_requested" in [event.type for event in events]
    assert "approval_resolved" in [event.type for event in events]
    finished = next(event for event in events if event.type == "tool_call_finished")
    assert finished.result["error"]["code"] == "APPROVAL_REQUIRED"
    assert executed is False


@pytest.mark.asyncio
async def test_json_thread_store_persists_atomically_and_bounds_records(tmp_path):
    store = JsonThreadStore(tmp_path / "threads", max_messages=2)
    messages = [AgentMessage(role="user", text="review this model")]

    await store.save("company/project/42", messages)

    assert list(await store.load("company/project/42")) == messages
    assert await store.list_threads() == ("company/project/42",)
    assert list((tmp_path / "threads").glob("*.tmp")) == []
    with pytest.raises(ThreadStoreError, match="message limit"):
        await store.save("too-long", messages * 3)
    assert await store.delete("company/project/42") is True
    assert await store.load("company/project/42") == ()


@pytest.mark.asyncio
async def test_specialist_agent_composes_as_a_namespaced_tool():
    specialist = Agent(
        name="fire-specialist",
        model=AnswerModel(),
        tools=Toolset(),
        instructions="Review fire-safety properties.",
    )
    source = AgentToolSource(
        specialist,
        namespace="specialists",
        name="review_fire",
        description="Review IFC fire-safety properties.",
    )
    tools = await Toolset.build(source)

    result = await tools.call(
        "specialists__review_fire",
        {"prompt": "Review the wall fire ratings."},
    )

    assert result["ok"] is True
    assert result["data"]["text"] == "Fire review complete."
    assert result["data"]["thread_id"].startswith("thread_")
