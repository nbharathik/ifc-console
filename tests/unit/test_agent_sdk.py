from __future__ import annotations

import asyncio
import json
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
async def test_agent_accumulates_usage_across_tool_rounds():
    source = FunctionToolSource(namespace="company")

    @source.tool()
    async def check() -> dict:
        return {"checked": True}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {"type": "usage", "in": 30, "out": 4},
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "check-1",
                            "name": "company__check",
                            "arguments": "{}",
                        }
                    ],
                },
            ],
            [
                {"type": "content", "text": "Checked."},
                {"type": "usage", "in": 50, "out": 6},
            ],
        ]
    )
    agent = Agent(
        name="usage",
        model=model,
        tools=tools,
        instructions="Check once.",
    )

    result = await agent.run("Check")

    assert result.usage.input_tokens == 80
    assert result.usage.output_tokens == 10


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
async def test_agent_tool_budget_leaves_no_dangling_tool_calls():
    source = FunctionToolSource(namespace="company")

    @source.tool()
    async def add(left: int, right: int) -> dict:
        return {"total": left + right}

    tools = await Toolset.build(source)
    calls = [
        {"id": f"sum-{index}", "name": "company__add", "arguments": '{"left": 1, "right": 1}'}
        for index in range(3)
    ]
    model = ScriptedModel([[{"type": "tool_calls", "calls": calls}]])
    store = InMemoryThreadStore()
    agent = Agent(
        name="bounded",
        model=model,
        tools=tools,
        instructions="Use tools for arithmetic.",
        thread_store=store,
        limits=AgentLimits(max_tool_calls=1),
    )

    events = [event async for event in agent.stream("Add", thread_id="thread-2")]

    assert events[-1].type == "run_failed"
    assert "1 tool calls" in (events[-1].text or "")
    finished = [event for event in events if event.type == "tool_call_finished"]
    assert [event.result["error"]["code"] for event in finished[1:]] == [
        "LIMIT_REACHED",
        "LIMIT_REACHED",
    ]
    saved = await store.load("thread-2")
    assistant = next(message for message in saved if message.role == "assistant")
    tool_ids = [message.tool_call_id for message in saved if message.role == "tool"]
    assert tool_ids == [call["id"] for call in assistant.tool_calls]


@pytest.mark.asyncio
async def test_agent_timeout_cancels_a_running_tool_and_completes_its_record():
    source = FunctionToolSource(namespace="company")
    completed = False

    @source.tool()
    async def slow_check() -> dict:
        nonlocal completed
        await asyncio.sleep(0.2)
        completed = True
        return {"checked": True}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "slow-1",
                            "name": "company__slow_check",
                            "arguments": "{}",
                        }
                    ],
                }
            ]
        ]
    )
    store = InMemoryThreadStore()
    agent = Agent(
        name="bounded",
        model=model,
        tools=tools,
        instructions="Run the check.",
        thread_store=store,
        limits=AgentLimits(timeout_s=0.03),
    )

    events = [event async for event in agent.stream("Check", thread_id="slow-thread")]
    await asyncio.sleep(0.05)

    assert events[-1].type == "run_failed"
    assert "timeout" in (events[-1].text or "")
    finished = next(event for event in events if event.type == "tool_call_finished")
    assert finished.result["error"]["code"] == "TIMEOUT"
    assert completed is False
    saved = await store.load("slow-thread")
    assert [message.role for message in saved] == ["user", "assistant", "tool"]


@pytest.mark.asyncio
async def test_agent_timeout_cancels_a_pending_approval():
    source = FunctionToolSource(namespace="company")
    executed = False

    @source.tool(requires_approval=True)
    async def publish() -> dict:
        nonlocal executed
        executed = True
        return {"published": True}

    async def delayed_approval(_request):
        await asyncio.sleep(0.2)
        return True

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": "publish-slow",
                            "name": "company__publish",
                            "arguments": "{}",
                        }
                    ],
                }
            ]
        ]
    )
    agent = Agent(
        name="publisher",
        model=model,
        tools=tools,
        instructions="Publish with approval.",
        approval_handler=CallbackApprovalHandler(delayed_approval),
        limits=AgentLimits(timeout_s=0.03),
    )

    events = [event async for event in agent.stream("Publish")]

    assert events[-1].type == "run_failed"
    finished = next(event for event in events if event.type == "tool_call_finished")
    assert finished.result["error"]["code"] == "TIMEOUT"
    assert executed is False


@pytest.mark.asyncio
async def test_agent_releases_thread_locks_after_high_cardinality_churn():
    agent = Agent(
        name="short-lived-threads",
        model=AnswerModel(),
        tools=Toolset(),
        instructions="Answer directly.",
    )

    for index in range(50):
        await agent.run("Review", thread_id=f"thread-{index}")

    assert agent._thread_locks == {}
    assert agent._thread_lock_users == {}


@pytest.mark.asyncio
async def test_agent_pairs_started_and_finished_events_for_invalid_arguments():
    source = FunctionToolSource(namespace="company")

    @source.tool()
    async def noop() -> dict:
        return {}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [{"id": "bad-1", "name": "company__missing", "arguments": "{broken"}],
                }
            ],
            [{"type": "content", "text": "Recovered."}],
        ]
    )
    agent = Agent(
        name="pairing",
        model=model,
        tools=tools,
        instructions="Recover from bad calls.",
    )

    events = [event async for event in agent.stream("Try the tool")]

    kinds = [event.type for event in events]
    assert kinds.index("tool_call_started") < kinds.index("tool_call_finished")
    finished = next(event for event in events if event.type == "tool_call_finished")
    assert finished.result["error"]["code"] == "INVALID_INPUT"
    assert events[-1].type == "run_completed"


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
async def test_json_thread_store_rejects_persisted_records_above_message_limit(tmp_path):
    store = JsonThreadStore(tmp_path / "threads", max_messages=1)
    thread_id = "oversized"
    message = AgentMessage(role="user", text="review this model")
    await store.save(thread_id, [message])
    path = store._path(thread_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["messages"].append(message.model_dump(mode="json"))
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ThreadStoreError, match="message limit"):
        await store.load(thread_id)
    assert await store.list_threads() == ()


@pytest.mark.asyncio
async def test_json_thread_store_listing_ignores_invalid_version_and_identity(tmp_path):
    store = JsonThreadStore(tmp_path / "threads")
    messages = [AgentMessage(role="user", text="review this model")]
    await store.save("valid", messages)
    await store.save("future-version", messages)
    await store.save("hashed-id", messages)

    version_path = store._path("future-version")
    version_payload = json.loads(version_path.read_text(encoding="utf-8"))
    version_payload["version"] = "2"
    version_path.write_text(json.dumps(version_payload), encoding="utf-8")

    identity_path = store._path("hashed-id")
    identity_payload = json.loads(identity_path.read_text(encoding="utf-8"))
    identity_payload["thread_id"] = "spoofed-id"
    identity_path.write_text(json.dumps(identity_payload), encoding="utf-8")

    assert await store.list_threads() == ("valid",)
    with pytest.raises(ThreadStoreError, match="corrupt"):
        await store.load("future-version")
    with pytest.raises(ThreadStoreError, match="corrupt"):
        await store.load("hashed-id")


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
