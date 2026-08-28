from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pytest
from ifc_console.toolsets import FunctionToolSource, ToolDefinition, Toolset

from ifc_console_agents import (
    Agent,
    AgentLimits,
    AgentMessage,
    AgentToolSource,
    CallbackApprovalHandler,
    InMemoryThreadStore,
    JsonThreadStore,
    ThreadStoreError,
)
from ifc_console_agents import agent as agent_module
from ifc_console_agents.agent import report_progress


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
        # the budget wrap-up round is deliberately tool free, so an empty
        # toolset is valid; None would still be a caller bug
        assert tools is not None
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
    # The second round is the wrap-up: a run that spends its budget still gets
    # one tool-free round to answer, rather than throwing away what it paid for.
    model = ScriptedModel(
        [
            [{"type": "tool_calls", "calls": calls}],
            [{"type": "content", "text": "Two of the sums did not fit the budget."}],
        ]
    )
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

    assert events[-1].type == "run_completed"
    result = events[-1].run_result
    assert result.stopped_reason == "tool_budget"
    assert "did not fit the budget" in result.text
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
    # A decision that never arrives is bounded by approval_timeout_s, not by the
    # run deadline, but the safety property is the same: an approval that timed
    # out must never let the protected tool run.
    agent = Agent(
        name="publisher",
        model=model,
        tools=tools,
        instructions="Publish with approval.",
        approval_handler=CallbackApprovalHandler(delayed_approval),
        limits=AgentLimits(approval_timeout_s=0.03),
    )

    events = [event async for event in agent.stream("Publish")]

    assert events[-1].type == "run_failed"
    finished = next(event for event in events if event.type == "tool_call_finished")
    assert finished.result["error"]["code"] == "TIMEOUT"
    assert executed is False


@pytest.mark.asyncio
async def test_time_spent_waiting_for_a_human_is_not_charged_to_the_run():
    """A person thinking about an approval must not fail the run under them."""
    source = FunctionToolSource(namespace="company")
    executed = False

    @source.tool(requires_approval=True)
    async def publish() -> dict:
        nonlocal executed
        executed = True
        return {"published": True}

    async def slow_human(_request):
        await asyncio.sleep(0.25)
        return True

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [
                        {"id": "publish-1", "name": "company__publish", "arguments": "{}"}
                    ],
                }
            ],
            [{"type": "content", "text": "Published."}],
        ]
    )
    agent = Agent(
        name="publisher",
        model=model,
        tools=tools,
        instructions="Publish with approval.",
        approval_handler=CallbackApprovalHandler(slow_human),
        # a deadline far shorter than the deliberation it must not count
        limits=AgentLimits(timeout_s=0.15, approval_timeout_s=5.0),
    )

    events = [event async for event in agent.stream("Publish")]

    assert events[-1].type == "run_completed"
    assert executed is True


@pytest.mark.asyncio
async def test_a_running_tool_reports_progress_instead_of_looking_hung():
    source = FunctionToolSource(namespace="company")

    @source.tool()
    async def scan() -> dict:
        for step in (1, 2, 3):
            report_progress(step, 3, "reading elements")
            await asyncio.sleep(0.01)
        return {"scanned": 3}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [{"id": "scan-1", "name": "company__scan", "arguments": "{}"}],
                }
            ],
            [{"type": "content", "text": "Scanned."}],
        ]
    )
    agent = Agent(name="scanner", model=model, tools=tools, instructions="Scan.")

    events = [event async for event in agent.stream("Scan")]

    progress = [event for event in events if event.type == "tool_progress"]
    assert progress, "a tool that reports progress must reach the caller"
    assert progress[0].tool_call_id == "scan-1"
    assert progress[0].tool_name == "company__scan"
    assert [event.progress.done for event in progress][-1] == 3
    assert progress[0].progress.total == 3
    assert progress[0].progress.note == "reading elements"
    kinds = [event.type for event in events]
    assert kinds.index("tool_call_started") < kinds.index("tool_progress")
    assert kinds.index("tool_progress") < kinds.index("tool_call_finished")


@pytest.mark.asyncio
async def test_a_silent_tool_still_says_it_is_alive(monkeypatch):
    """Silence is what reads as a hang, so an idle tool gets a heartbeat."""
    monkeypatch.setattr(agent_module, "_HEARTBEAT_S", 0.01)
    source = FunctionToolSource(namespace="company")

    @source.tool()
    async def quiet() -> dict:
        await asyncio.sleep(0.05)
        return {"ok": True}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [{"id": "q-1", "name": "company__quiet", "arguments": "{}"}],
                }
            ],
            [{"type": "content", "text": "Done."}],
        ]
    )
    agent = Agent(name="quiet-runner", model=model, tools=tools, instructions="Wait.")

    events = [event async for event in agent.stream("Wait")]

    progress = [event for event in events if event.type == "tool_progress"]
    assert progress, "a slow silent tool must still report that it is running"
    assert progress[-1].progress.elapsed_s > 0
    assert events[-1].type == "run_completed"


@pytest.mark.asyncio
async def test_a_stopped_run_saves_the_tools_that_actually_ran():
    """Stop, and the thread has to record what happened, side effects and all."""
    source = FunctionToolSource(namespace="company")
    started = asyncio.Event()

    @source.tool()
    async def slow() -> dict:
        started.set()
        await asyncio.sleep(30)
        return {"done": True}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {"type": "content", "text": "Looking now. "},
                {
                    "type": "tool_calls",
                    "calls": [{"id": "slow-1", "name": "company__slow", "arguments": "{}"}],
                },
            ]
        ]
    )
    store = InMemoryThreadStore()
    agent = Agent(
        name="stoppable",
        model=model,
        tools=tools,
        instructions="Look.",
        thread_store=store,
    )

    async def consume() -> None:
        async for _event in agent.stream("Look", thread_id="stopped-thread"):
            pass

    running = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    saved = await store.load("stopped-thread")
    assert [message.role for message in saved] == ["user", "assistant", "tool"]
    assert saved[1].text == "Looking now. "
    aborted = json.loads(saved[-1].text)
    assert aborted["error"]["code"] == "RUN_ABORTED"
    assert saved[-1].tool_call_id == "slow-1"


@pytest.mark.asyncio
async def test_a_stopped_run_keeps_the_answer_it_had_already_written():
    class StallingModel:
        provider_id = "test"
        model_id = "stalling"

        async def stream(self, **_kwargs):
            yield {"type": "content", "text": "Half an answ"}
            await asyncio.sleep(30)
            yield {"type": "content", "text": "er."}

    store = InMemoryThreadStore()
    agent = Agent(
        name="stoppable",
        model=StallingModel(),
        tools=Toolset(),
        instructions="Answer.",
        thread_store=store,
    )
    seen: list[str] = []

    async def consume() -> None:
        async for event in agent.stream("Answer", thread_id="partial-thread"):
            if event.type == "text_delta":
                seen.append(event.text or "")

    running = asyncio.create_task(consume())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if seen:
            break
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    saved = await store.load("partial-thread")
    assert [message.role for message in saved] == ["user", "assistant"]
    assert saved[-1].text == "Half an answ"


@pytest.mark.asyncio
async def test_a_tool_can_see_the_budget_its_result_will_be_held_to():
    """One budget, decided once: paging to it beats being clipped by it."""
    from ifc_console_agents.agent import current_result_budget

    source = FunctionToolSource(namespace="company")
    seen: list[int | None] = []

    @source.tool()
    async def report() -> dict:
        seen.append(current_result_budget())
        return {"ok": True}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [
                {
                    "type": "tool_calls",
                    "calls": [{"id": "r-1", "name": "company__report", "arguments": "{}"}],
                }
            ],
            [{"type": "content", "text": "Reported."}],
        ]
    )
    agent = Agent(
        name="budgeted",
        model=model,
        tools=tools,
        instructions="Report.",
        limits=AgentLimits(max_tool_result_chars=4321),
    )

    await agent.run("Report")

    assert seen == [4321]
    assert current_result_budget() is None


def test_a_clipped_tool_result_keeps_its_error_code_and_meta():
    envelope = {
        "ok": False,
        "error": {"code": "TOO_MANY_ELEMENTS", "message": "x" * 400, "hint": "narrow it"},
        "meta": {"returned": 0, "total": 9000},
        "data": {"rows": ["y" * 200 for _ in range(50)]},
    }

    text = agent_module._tool_text(envelope, 1000)

    assert len(text) <= 1000
    clipped = json.loads(text)
    assert clipped["error"]["code"] == "TOO_MANY_ELEMENTS"
    assert clipped["meta"]["total"] == 9000
    assert clipped["truncation"]["of_chars"] > 600


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
async def test_a_nameless_tool_call_is_answered_rather_than_fatal():
    """Anthropic emits one when a tool block arrives without its header."""
    source = FunctionToolSource(namespace="company")

    @source.tool()
    async def noop() -> dict:
        return {}

    tools = await Toolset.build(source)
    model = ScriptedModel(
        [
            [{"type": "tool_calls", "calls": [{"id": "", "name": "", "arguments": "{}"}]}],
            [{"type": "content", "text": "Recovered."}],
        ]
    )
    agent = Agent(
        name="resilient",
        model=model,
        tools=tools,
        instructions="Recover from bad calls.",
    )

    events = [event async for event in agent.stream("Try it")]

    finished = next(event for event in events if event.type == "tool_call_finished")
    assert finished.result["error"]["code"] == "TOOL_NOT_FOUND"
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
    forwarded: list[Any] = []
    source = AgentToolSource(
        specialist,
        namespace="specialists",
        name="review_fire",
        description="Review IFC fire-safety properties.",
        on_event=forwarded.append,
        parent_thread="panel-abc",
        parent_run_id="run-1",
    )
    tools = await Toolset.build(source)
    definition = tools.require("specialists__review_fire")

    result = await tools.call(
        "specialists__review_fire",
        {"prompt": "Review the wall fire ratings."},
    )

    assert result["ok"] is True
    assert result["data"]["text"] == "Fire review complete."
    # the child's thread is derived from its parent's, never model-supplied
    assert "thread_id" not in definition.input_schema["properties"]
    assert result["data"]["thread_id"] == "panel-abc::sub::review_fire"
    # every child event reaches the host, tagged with where it came from
    assert [event.type for event in forwarded] == [
        "run_started",
        "text_delta",
        "run_completed",
    ]
    assert {event.depth for event in forwarded} == {1}
    assert {event.parent_run_id for event in forwarded} == {"run-1"}


@pytest.mark.asyncio
async def test_a_specialist_with_nobody_watching_can_never_block_on_approval():
    """The deadlock the audit warned about: no sink means no approval wait."""
    source = FunctionToolSource(namespace="risky")
    executed = False

    @source.tool(requires_approval=True)
    async def publish() -> dict:
        nonlocal executed
        executed = True
        return {"published": True}

    never = asyncio.Event()

    async def hangs_forever(_request):
        await never.wait()
        return True

    specialist = Agent(
        name="publisher",
        model=ScriptedModel(
            [
                [
                    {
                        "type": "tool_calls",
                        "calls": [
                            {"id": "pub-1", "name": "risky__publish", "arguments": "{}"}
                        ],
                    }
                ],
                [{"type": "content", "text": "Nothing was published."}],
            ]
        ),
        tools=await Toolset.build(source),
        instructions="Publish only with approval.",
        approval_handler=CallbackApprovalHandler(hangs_forever),
    )
    delegated = AgentToolSource(
        specialist,
        name="publish_for_me",
        description="Ask the specialist to publish.",
    )
    tools = await Toolset.build(delegated)

    result = await asyncio.wait_for(
        tools.call("agents__publish_for_me", {"prompt": "Publish it."}), timeout=5
    )

    assert result["ok"] is True
    assert executed is False


@pytest.mark.asyncio
async def test_a_delegated_agent_may_not_delegate_again():
    specialist = Agent(
        name="fire-specialist",
        model=AnswerModel(),
        tools=Toolset(),
        instructions="Review fire-safety properties.",
    )
    source = AgentToolSource(
        specialist,
        name="review_fire",
        description="Review IFC fire-safety properties.",
        on_event=lambda _event: None,
        depth=1,
    )
    tools = await Toolset.build(source)

    result = await tools.call("agents__review_fire", {"prompt": "Review again."})

    assert result["ok"] is False
    assert result["error"]["code"] == "DELEGATION_DEPTH"
