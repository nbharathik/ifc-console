from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ifc_console_agents.integrations import langgraph as integration
from ifc_console_agents.models import ApprovalRequest


class FakeGraph:
    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[Any, dict[str, Any], str]] = []

    async def astream(self, value, config, *, stream_mode):
        self.calls.append((value, config, stream_mode))
        for chunk in self.chunks:
            yield chunk


def approval() -> ApprovalRequest:
    return ApprovalRequest(
        request_id="approval-1",
        run_id="source-run",
        thread_id="thread-1",
        tool_call_id="call-1",
        tool_name="commit_change",
        arguments={"change_set_id": "change-1"},
        required_capabilities=("model:commit",),
    )


def test_importing_adapter_does_not_import_langgraph() -> None:
    probe = (
        "import sys; import ifc_console_agents.integrations.langgraph; "
        "print(any(name == 'langgraph' or name.startswith('langgraph.') "
        "for name in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_factory_has_a_clear_optional_dependency_error(monkeypatch) -> None:
    def missing(name: str):
        raise ImportError(name)

    monkeypatch.setattr(integration, "import_module", missing)

    with pytest.raises(
        integration.LangGraphUnavailable, match=r"ifc-console-agents\[graph\]"
    ):
        integration.create_langgraph_workflow(lambda *_args: None)


def test_graph_extra_is_optional() -> None:
    root = Path(__file__).resolve().parents[2]
    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    base, optional = metadata.split("[project.optional-dependencies]", 1)
    graph = optional.split("graph = [", 1)[1].split("]", 1)[0]

    assert "langgraph" not in base.casefold()
    assert '"langgraph>=1,<2"' in graph
    assert '"langgraph-checkpoint-sqlite>=3,<4"' in graph


def test_factory_compiles_the_shared_state_lazily(monkeypatch) -> None:
    graph = FakeGraph([])

    class Builder:
        schema = None
        compiled = None

        def __init__(self, schema):
            Builder.schema = schema

        def compile(self, **kwargs):
            Builder.compiled = kwargs
            return graph

    monkeypatch.setattr(
        integration,
        "_load_langgraph",
        lambda: (Builder, "START", "END", lambda value: ("resume", value)),
    )
    seen = []

    workflow = integration.create_langgraph_workflow(
        lambda builder, start, end: seen.append((builder, start, end)),
        checkpointer="checkpoint-store",
        name="coordination",
        configuration_digest="sha256:test",
    )

    assert Builder.schema is integration.GraphWorkflowState
    assert seen[0][1:] == ("START", "END")
    assert Builder.compiled == {"checkpointer": "checkpoint-store", "name": "coordination"}
    assert workflow.graph is graph
    assert workflow.configuration_digest == "sha256:test"


@pytest.mark.asyncio
async def test_adapter_translates_graph_updates_to_agent_events() -> None:
    chunks = [
        {
            "inspect": integration.graph_update(
                events=[
                    {"type": "text_delta", "text": "Checked. "},
                    {
                        "type": "tool_call_started",
                        "tool_call_id": "call-1",
                        "tool_name": "get_element",
                        "arguments": {"global_id": "wall-1"},
                    },
                    {
                        "type": "tool_call_finished",
                        "tool_call_id": "call-1",
                        "tool_name": "get_element",
                        "arguments": {"global_id": "wall-1"},
                        "result": {"ok": True, "data": {"class": "IfcWall"}},
                    },
                    {
                        "type": "usage",
                        "usage": {"input_tokens": 12, "output_tokens": 3},
                    },
                ]
            )
        },
        {"finish": integration.graph_update(final_text="Checked the wall.")},
    ]
    graph = FakeGraph(chunks)
    workflow = integration.LangGraphWorkflow(graph, name="review")

    events = [event async for event in workflow.stream("Review wall", thread_id="thread-1")]

    assert [event.type for event in events] == [
        "run_started",
        "text_delta",
        "tool_call_started",
        "tool_call_finished",
        "usage",
        "run_completed",
    ]
    completed = events[-1].run_result
    assert completed is not None
    assert completed.text == "Checked the wall."
    assert completed.tool_calls[0].name == "get_element"
    assert completed.usage.input_tokens == 12
    assert graph.calls[0][1]["configurable"]["thread_id"] == "thread-1"
    assert graph.calls[0][2] == "updates"


@pytest.mark.asyncio
async def test_interrupt_pauses_and_resume_uses_the_same_event_boundary() -> None:
    request = approval()
    first = FakeGraph(
        [{"__interrupt__": (SimpleNamespace(value=integration.graph_approval_interrupt(request)),)}]
    )
    workflow = integration.LangGraphWorkflow(
        first,
        name="approval",
        command_factory=lambda value: {"resume": value},
    )

    paused = [event async for event in workflow.stream("Apply", thread_id="thread-1")]

    assert [event.type for event in paused] == [
        "run_started",
        "approval_requested",
    ], [(event.type, event.text) for event in paused]
    assert paused[-1].approval == request

    resumed_graph = FakeGraph(
        [
            {
                "apply": integration.graph_update(
                    events=[{"type": "text_delta", "text": "Approved."}],
                    final_text="Approved.",
                )
            }
        ]
    )
    workflow.graph = resumed_graph
    resumed = [
        event
        async for event in workflow.resume(
            {"approved": True, "request_id": request.request_id},
            thread_id="thread-1",
        )
    ]

    assert [event.type for event in resumed] == [
        "run_started",
        "text_delta",
        "run_completed",
    ]
    assert resumed_graph.calls[0][0] == {"resume": {"approved": True, "request_id": "approval-1"}}
    assert resumed_graph.calls[0][1]["configurable"]["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_checkpoint_delete_and_inline_image_guard() -> None:
    class Checkpointer:
        def __init__(self):
            self.deleted = []

        async def adelete_thread(self, thread_id):
            self.deleted.append(thread_id)

    checkpointer = Checkpointer()
    workflow = integration.LangGraphWorkflow(FakeGraph([]), name="safe", checkpointer=checkpointer)

    assert await workflow.delete_thread("thread-1") is True
    assert checkpointer.deleted == ["thread-1"]
    with pytest.raises(ValueError, match="artifact references"):
        _ = [
            event
            async for event in workflow.stream(
                "Inspect",
                images=[{"media_type": "image/png", "data": "AAAA"}],
            )
        ]


def test_graph_updates_must_be_json_safe_and_lifecycle_free() -> None:
    with pytest.raises(ValueError, match="JSON-serializable"):
        integration.graph_update(data={"runtime": object()})
    with pytest.raises(ValueError, match="literal"):
        integration.graph_update(events=[{"type": "run_completed"}])


@pytest.mark.asyncio
async def test_real_langgraph_update_round_trip_when_extra_is_installed() -> None:
    pytest.importorskip("langgraph")

    async def inspect(_state):
        return integration.graph_update(
            events=[{"type": "text_delta", "text": "Inspected."}],
            final_text="Inspected.",
        )

    def configure(builder, start, end):
        builder.add_node("inspect", inspect)
        builder.add_edge(start, "inspect")
        builder.add_edge("inspect", end)

    workflow = integration.create_langgraph_workflow(configure, name="inspect-smoke")
    events = [event async for event in workflow.stream("Inspect", thread_id="real-thread")]

    assert [event.type for event in events] == [
        "run_started",
        "text_delta",
        "run_completed",
    ]
    assert events[-1].run_result.text == "Inspected."


@pytest.mark.asyncio
async def test_real_langgraph_interrupt_round_trip_when_extra_is_installed() -> None:
    pytest.importorskip("langgraph")
    if sys.version_info < (3, 11):
        pytest.skip("async LangGraph interrupts require Python 3.11 or newer")
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import interrupt

    request = approval()

    async def review(_state):
        decision = interrupt(integration.graph_approval_interrupt(request))
        approved = bool(decision.get("approved"))
        return integration.graph_update(
            events=[
                {
                    "type": "approval_resolved",
                    "decision": {
                        "approved": approved,
                        "decided_by": "test",
                    },
                },
                {
                    "type": "text_delta",
                    "text": "Approved." if approved else "Rejected.",
                },
            ],
            final_text="Approved." if approved else "Rejected.",
        )

    def configure(builder, start, end):
        builder.add_node("review", review)
        builder.add_edge(start, "review")
        builder.add_edge("review", end)

    workflow = integration.create_langgraph_workflow(
        configure,
        checkpointer=InMemorySaver(),
        name="approval-smoke",
    )
    paused = [event async for event in workflow.stream("Apply", thread_id="real-thread")]
    resumed = [
        event
        async for event in workflow.resume(
            {"approved": True},
            thread_id="real-thread",
        )
    ]

    assert [event.type for event in paused] == [
        "run_started",
        "approval_requested",
    ], [(event.type, event.text) for event in paused]
    assert [event.type for event in resumed] == [
        "run_started",
        "approval_resolved",
        "text_delta",
        "run_completed",
    ]
    assert resumed[-1].run_result.text == "Approved."
