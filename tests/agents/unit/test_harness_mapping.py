"""ACP updates become the panel's AgentEvents; engines come from settings."""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    ContentToolCallContent,
    PermissionOption,
    PlanEntry,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    Usage,
)

from ifc_console.agents.harness.acp import RunState, envelope_from_update, native_tool_name
from ifc_console.agents.harness.agent import HarnessAgent, HarnessPack, session_instructions
from ifc_console.agents.harness.engine import (
    EngineRegistry,
    EngineSpec,
    mcp_servers_for,
    template_settings,
)
from ifc_console.agents.models import AgentLimits, ApprovalDecision

OPTIONS = [
    PermissionOption(option_id="once", name="once", kind="allow_once"),
    PermissionOption(option_id="always", name="always", kind="allow_always"),
    PermissionOption(option_id="no", name="no", kind="reject_once"),
]


def _text(text: str) -> TextContentBlock:
    return TextContentBlock(type="text", text=text)


def _start(call_id: str, title: str, **extra):
    return ToolCallStart(
        session_update="tool_call", tool_call_id=call_id, title=title, status="pending", **extra
    )


def _finish(call_id: str, **extra):
    return ToolCallProgress(
        session_update="tool_call_update", tool_call_id=call_id, status="completed", **extra
    )


class Decide:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.requests = []

    async def request(self, request):
        self.requests.append(request)
        return ApprovalDecision(approved=self.approved, decided_by="test")


def _run(decider=None, **limits) -> RunState:
    return RunState(
        run_id="run",
        thread_id="thread",
        engine="fake",
        limits=AgentLimits(**limits),
        decider=decider or Decide(True),
    )


def _drain(run: RunState) -> list:
    events = []
    while not run.queue.empty():
        events.append(run.queue.get_nowait())
    return events


def test_native_tool_name_strips_every_known_prefix():
    for title in (
        "mcp__ifc-console__query_elements",
        "ifc-console__query_elements",
        "ifc-console_query_elements",
        "ifc-console.query_elements",
        "ifc_console_query_elements",
        "query_elements",
    ):
        assert native_tool_name(title) == "query_elements"
    assert native_tool_name("bash") == "bash"
    assert native_tool_name("") == "tool"


def test_envelope_from_raw_output_variants():
    envelope = {"ok": True, "data": {"rows": []}, "meta": {"returned": 0}}
    done = _finish("c", raw_output=envelope)
    assert envelope_from_update(done, engine="fake", limit=100)["meta"] == {
        "returned": 0,
        "tool_source": "harness:fake",
    }
    mcp_shaped = {"content": [{"type": "text", "text": '{"ok": false, "error": {"code": "X"}}'}]}
    parsed = envelope_from_update(_finish("c", raw_output=mcp_shaped), engine="fake", limit=100)
    assert parsed["ok"] is False and parsed["error"]["code"] == "X"
    wrapped = envelope_from_update(_finish("c", raw_output="file-a\nfile-b"), engine="fake", limit=6)
    assert wrapped == {
        "ok": True,
        "data": {"output": "file-a"},
        "meta": {"tool_source": "harness:fake"},
    }
    failed = ToolCallProgress(
        session_update="tool_call_update",
        tool_call_id="c",
        status="failed",
        content=[ContentToolCallContent(type="content", content=_text("denied"))],
    )
    result = envelope_from_update(failed, engine="fake", limit=100, failed=True)
    assert result["ok"] is False and result["error"]["message"] == "denied"


def test_updates_map_to_agent_events_in_order():
    run = _run()
    run.on_update(AgentThoughtChunk(session_update="agent_thought_chunk", content=_text("hm")))
    run.on_update(AgentMessageChunk(session_update="agent_message_chunk", content=_text("hi ")))
    run.on_update(_start("c1", "ifc-console_query_elements", raw_input={"selector": "IfcWall"}))
    run.on_update(
        ToolCallProgress(session_update="tool_call_update", tool_call_id="c1", status="in_progress")
    )
    run.on_update(_finish("c1", raw_output={"ok": True, "data": {}, "meta": {"returned": 3}}))
    run.on_update(_finish("c1", raw_output={"ok": True}))  # a second completion is ignored
    run.on_update(
        AgentPlanUpdate(
            session_update="plan",
            entries=[PlanEntry(content="look", priority="high", status="completed")],
        )
    )
    # an update for a call the engine never announced synthesises the start
    run.on_update(_finish("c2", raw_output="done"))
    kinds = [event.type for event in _drain(run)]
    assert kinds == [
        "reasoning_delta",
        "text_delta",
        "tool_call_started",
        "tool_progress",
        "tool_call_finished",
        "reasoning_delta",
        "tool_call_started",
        "tool_call_finished",
    ]
    assert run.answer == ["hi "]
    assert [record.name for record in run.records] == ["query_elements", "tool"]
    assert run.records[0].summary == "3 row(s)"
    assert run.calls["c1"].arguments == {"selector": "IfcWall"}


async def test_permission_round_trip_selects_the_matching_option():
    decider = Decide(True)
    run = _run(decider)
    call = ToolCallUpdate(tool_call_id="c1", title="bash", raw_input={"command": "ls"})
    response = await run.on_permission(call, OPTIONS)
    assert response.outcome.outcome == "selected" and response.outcome.option_id == "once"
    kinds = [event.type for event in _drain(run)]
    assert kinds == ["tool_call_started", "approval_requested", "approval_resolved"]
    assert decider.requests[0].tool_name == "bash"
    assert decider.requests[0].arguments == {"command": "ls"}

    denied = await _run(Decide(False)).on_permission(call, OPTIONS)
    assert denied.outcome.option_id == "no"

    cancelled = _run(Decide(True))
    cancelled.cancelled = True
    assert (await cancelled.on_permission(call, OPTIONS)).outcome.outcome == "cancelled"


async def test_permission_wait_is_credited_and_times_out():
    class Slow:
        async def request(self, request):
            await asyncio.sleep(0.2)
            return ApprovalDecision(approved=True)

    run = _run(Slow(), approval_timeout_s=0.05)
    response = await run.on_permission(ToolCallUpdate(tool_call_id="c", title="bash"), OPTIONS)
    assert response.outcome.option_id == "no"
    assert run.approval_credit_s >= 0.03  # Windows timers are coarse
    decision = [e for e in _drain(run) if e.type == "approval_resolved"][0].decision
    assert decision.decided_by == "timeout"


def test_budget_counts_tool_starts():
    run = _run(max_tool_calls=2)
    run.on_update(_start("a", "x"))
    assert not run.budget_exceeded
    run.on_update(_start("b", "y"))
    assert run.budget_exceeded


def test_session_instructions_retitle_the_tool_list():
    text = session_instructions("Be careful.\n\nYour tools:\nquery_elements: find things")
    assert "Your tools:" not in text
    assert "MCP server named 'ifc-console'" in text
    assert text.endswith("Tools:\nquery_elements: find things")
    assert "MCP server" in session_instructions("")


def test_engine_registry_reads_settings_and_lists_providers(tmp_path: Path):
    from ifc_console.settings import Settings

    settings = Settings.model_validate(
        {
            "harness": {
                "enabled": True,
                "engines": [
                    {"name": "fake", "command": sys.executable, "args": ["-m", "x"]},
                    {"name": "missing", "command": "no-such-engine-binary"},
                ],
            }
        }
    )
    registry = EngineRegistry(settings)
    rows = registry.provider_rows()
    assert [row["id"] for row in rows] == ["harness:fake", "harness:missing"]
    assert rows[0]["family"] == "harness" and rows[0]["installed"] is True
    assert rows[1]["installed"] is False and "not found" in rows[1]["note"]
    assert registry.by_provider("harness:fake").name == "fake"
    assert registry.by_provider("openai") is None
    assert not EngineRegistry(Settings()).provider_rows()
    template = template_settings("opencode")
    assert template.command == "opencode" and template.args == ["acp"]
    with pytest.raises(KeyError):
        template_settings("nope")


def test_each_engine_receives_only_the_provider_keys_it_names(tmp_path: Path, monkeypatch):
    from ifc_console.agents.harness.engine import TEMPLATES, engine_environment
    from ifc_console.settings import HarnessEngineSettings

    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OTHER_KEY"):
        monkeypatch.setenv(name, f"secret-{name}")
    core = SimpleNamespace(store=SimpleNamespace(home=tmp_path))

    def env_for(row: HarnessEngineSettings) -> dict[str, str]:
        return engine_environment(core, EngineSpec.from_settings(row))

    codex = env_for(template_settings("codex"))
    assert codex["OPENAI_API_KEY"] == "secret-OPENAI_API_KEY"
    assert not {"ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OTHER_KEY"} & set(codex)
    assert set(env_for(template_settings("claude"))) == {"IFC_CONSOLE_HOME", "ANTHROPIC_API_KEY"}
    assert set(env_for(template_settings("gemini"))) == {"IFC_CONSOLE_HOME", "GEMINI_API_KEY"}
    # engines with their own credential flow get nothing unless the user says so
    assert set(env_for(template_settings("opencode"))) == {"IFC_CONSOLE_HOME"}
    custom = HarnessEngineSettings(name="mine", command="x", forward_keys=["OTHER_KEY"])
    assert set(env_for(custom)) == {"IFC_CONSOLE_HOME", "OTHER_KEY"}
    silent = template_settings("codex").model_copy(update={"forward_keys": []})
    assert set(env_for(silent)) == {"IFC_CONSOLE_HOME"}
    assert TEMPLATES["codex"]["keys"] == ["OPENAI_API_KEY"]


def test_forward_keys_must_be_environment_variable_names():
    from pydantic import ValidationError

    from ifc_console.settings import HarnessEngineSettings

    with pytest.raises(ValidationError):
        HarnessEngineSettings(name="x", command="x", forward_keys=["not a name"])


def test_npx_engines_run_a_pinned_adapter_version():
    from ifc_console.agents.harness.engine import TEMPLATES

    for name in ("codex", "claude"):
        spec = TEMPLATES[name]["args"][-1]
        assert re.fullmatch(r"@[a-z-]+/[a-z-]+@\d+\.\d+\.\d+", spec), spec


def test_mcp_injection_uses_the_bridge_with_the_console_home(tmp_path: Path):
    core = SimpleNamespace(
        port=8399,
        token="tok",
        mcp_url="http://127.0.0.1:8399/mcp",
        store=SimpleNamespace(home=tmp_path),
        settings=SimpleNamespace(server=SimpleNamespace(persistent_token=True)),
    )
    engine = EngineSpec(name="fake", label="Fake", command="x")
    [server] = mcp_servers_for(core, engine)
    assert server.name == "ifc-console"
    assert server.command == sys.executable
    assert server.args[-2:] == ["--port", "8399"] and "--token" not in server.args
    assert [(e.name, e.value) for e in server.env] == [("IFC_CONSOLE_HOME", str(tmp_path))]

    core.settings.server.persistent_token = False
    [server] = mcp_servers_for(core, engine)
    assert server.args[-2:] == ["--token", "tok"]

    http = EngineSpec(name="fake", label="Fake", command="x", mcp="http")
    [server] = mcp_servers_for(core, http, http_supported=True)
    assert server.type == "http" and server.url == core.mcp_url
    assert server.headers[0].value == "Bearer tok"
    # an engine without http MCP falls back to the bridge
    assert mcp_servers_for(core, http, http_supported=False)[0].name == "ifc-console"


class StubSession:
    """A session whose engine is a script: no subprocess involved."""

    def __init__(self, script) -> None:
        self.script = script
        self.run = None
        self.cancelled = ""
        self.turns = 0
        self.alive = True
        self.engine = SimpleNamespace(name="fake")
        self.thread_id = "t"

    def supports_images(self) -> bool:
        return False

    async def prompt(self, run, blocks):
        self.run = run
        self.turns += 1
        try:
            return await self.script(self, run, blocks)
        finally:
            self.run = None

    async def cancel(self, reason: str) -> None:
        self.cancelled = reason
        if self.run is not None:
            self.run.cancelled = True
            self.run.cancel_reason = reason
            self.run.cancel_pending()


class StubHost:
    def __init__(self, session: StubSession) -> None:
        self.session = session
        self.created = 0
        self.discarded = 0
        self.released = []

    async def session_for(self, engine, thread_id, *, instructions=""):
        self.created += 1
        self.instructions = instructions
        return self.session, self.created == 1

    async def discard(self, session) -> None:
        self.discarded += 1
        session.alive = False

    def release_thread(self, thread_id: str) -> None:
        self.released.append(thread_id)


def _agent(host, **kwargs) -> HarnessAgent:
    engine = EngineSpec(name="fake", label="Fake", command="x")
    return HarnessAgent(
        name="fake-agent",
        engine=engine,
        host=host,
        tools=SimpleNamespace(),
        instructions="Role text.\n\nYour tools:\nquery_elements: q",
        **kwargs,
    )


async def test_harness_agent_streams_a_turn_and_keeps_the_transcript():
    seen = {}

    async def script(session, run, blocks):
        seen["text"] = blocks[0].text
        run.on_update(AgentMessageChunk(session_update="agent_message_chunk", content=_text("42 ")))
        run.on_update(_start("c1", "ifc-console_query_elements"))
        run.on_update(_finish("c1", raw_output={"ok": True, "data": {}, "meta": {}}))
        run.on_update(AgentMessageChunk(session_update="agent_message_chunk", content=_text("walls")))
        return PromptResponse(
            stop_reason="end_turn", usage=Usage(total_tokens=5, input_tokens=3, output_tokens=2)
        )

    session = StubSession(script)
    host = StubHost(session)
    agent = _agent(host)
    events = [event async for event in agent.stream("how many walls", thread_id="t")]
    kinds = [event.type for event in events]
    assert kinds == [
        "run_started",
        "text_delta",
        "tool_call_started",
        "tool_call_finished",
        "text_delta",
        "usage",
        "run_completed",
    ]
    result = events[-1].run_result
    assert result.text == "42 walls"
    assert result.usage.input_tokens == 3
    assert result.tool_calls[0].name == "query_elements"
    # the first turn carries the session instructions, later turns do not
    assert seen["text"].startswith("[ifc-console session instructions]")
    assert "MCP server named 'ifc-console'" in seen["text"]
    assert seen["text"].endswith("how many walls")
    history = await agent.thread_store.load("t")
    assert [(m.role, m.text) for m in history] == [("user", "how many walls"), ("assistant", "42 walls")]

    second = [event async for event in agent.stream("and doors", thread_id="t")]
    assert second[-1].type == "run_completed"
    assert seen["text"] == "and doors"
    agent.release()
    assert host.released == ["t"]


async def test_harness_agent_reports_refusal_and_budget_and_timeout():
    async def refuse(session, run, blocks):
        return PromptResponse(stop_reason="refusal")

    failed = [e async for e in _agent(StubHost(StubSession(refuse))).stream("x", thread_id="a")]
    assert failed[-1].type == "run_failed" and "refused" in failed[-1].text

    async def chatty(session, run, blocks):
        for index in range(3):
            run.on_update(_start(f"c{index}", "bash"))
            await asyncio.sleep(0.02)  # a real engine waits on the wire between calls
            if run.cancelled:
                return PromptResponse(stop_reason="cancelled")
        return PromptResponse(stop_reason="end_turn")

    budget = StubSession(chatty)
    events = [
        e
        async for e in _agent(StubHost(budget), limits=AgentLimits(max_tool_calls=2)).stream(
            "x", thread_id="b"
        )
    ]
    assert events[-1].type == "run_completed"
    assert events[-1].run_result.stopped_reason == "tool_budget"

    async def slow(session, run, blocks):
        while not run.cancelled:
            await asyncio.sleep(0.01)
        return PromptResponse(stop_reason="cancelled")

    timed = StubSession(slow)
    events = [
        e
        async for e in _agent(StubHost(timed), limits=AgentLimits(timeout_s=0.1)).stream(
            "x", thread_id="c"
        )
    ]
    assert timed.cancelled == "timeout"
    assert events[-1].type == "run_failed" and "timeout" in events[-1].text


async def test_harness_agent_interrupt_cancels_the_engine_and_seals_the_thread():
    async def slow(session, run, blocks):
        run.on_update(AgentMessageChunk(session_update="agent_message_chunk", content=_text("part")))
        while not run.cancelled:
            await asyncio.sleep(0.01)
        return PromptResponse(stop_reason="cancelled")

    session = StubSession(slow)
    agent = _agent(StubHost(session))

    async def consume():
        return [e async for e in agent.stream("x", thread_id="d")]

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.cancelled == "user"
    history = await agent.thread_store.load("d")
    assert [m.text for m in history] == ["x", "part"]


async def test_harness_agent_survives_a_failed_engine_start():
    class Broken:
        async def session_for(self, engine, thread_id, *, instructions=""):
            raise RuntimeError("no such binary")

    events = [e async for e in _agent(Broken()).stream("x", thread_id="e")]
    assert [e.type for e in events] == ["run_started", "run_failed"]
    assert "no such binary" in events[-1].text


async def test_harness_pack_wraps_any_pack():
    class Inner:
        info = SimpleNamespace(name="inner", kind="built-in")
        declared_limits = AgentLimits(max_tool_calls=7)
        preset = "something"

        async def build(self, runtime, *, model, viewer=False, instructions="", model_label=""):
            return SimpleNamespace(
                name="inner-agent",
                tools=SimpleNamespace(),
                instructions="built " + instructions,
                thread_store=None,
                approval_handler=None,
                limits=self.declared_limits,
                middleware=(),
            )

    engine = EngineSpec(name="fake", label="Fake", command="x", args=("acp",))
    pack = HarnessPack(Inner(), engine, StubHost(StubSession(None)))
    assert pack.info.name == "inner" and pack.preset == "something"
    assert pack.declared_limits.max_tool_calls == 7
    assert "engine:fake|x|acp" in pack.configuration_signature()
    agent = await pack.build(None, model=None, instructions="extra")
    assert isinstance(agent, HarnessAgent)
    assert agent.instructions == "built extra" and agent.limits.max_tool_calls == 7
