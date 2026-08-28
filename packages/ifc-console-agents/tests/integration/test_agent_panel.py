"""The agent panel over HTTP: listing, streaming, threads, and uploads."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from ifc_console_agents.models import AgentLimits
from ifc_console_agents.packs import AgentPackInfo
from ifc_console_agents.testing import ScriptedAgentModel, text_round, tool_call_round

pytestmark = pytest.mark.asyncio


class ScriptedPack:
    """A pack that ignores the provider model and plays a script instead."""

    def __init__(self, name="scripted", features=(), rounds=None, limits=None):
        self.info = AgentPackInfo(
            name=name,
            title="Scripted",
            description="offline test pack",
            features=tuple(features),
            starters=("say hello",),
        )
        self.declared_limits = limits or AgentLimits()
        self.rounds = rounds or [text_round("hello from the pack")]
        self.built = 0

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console_agents import Agent

        self.built += 1
        tools = await runtime.tools("get_ifc_project_info", "query_elements")
        return Agent(
            name=self.info.name,
            model=ScriptedAgentModel(list(self.rounds)),
            tools=tools,
            instructions="test",
            limits=self.declared_limits,
        )


class DelayedAgentModel:
    """A provider that remains active until a reset cancels its stream."""

    provider_id = "test"
    model_id = "delayed"

    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        self.started = started
        self.release = release

    async def stream(self, **_kwargs):
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        yield {"type": "content", "text": "late answer"}


class DelayedPack(ScriptedPack):
    def __init__(self, started: threading.Event, release: threading.Event) -> None:
        super().__init__(name="delayed")
        self.started = started
        self.release = release

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console_agents import Agent

        self.built += 1
        tools = await runtime.tools("get_ifc_project_info")
        return Agent(
            name=self.info.name,
            model=DelayedAgentModel(self.started, self.release),
            tools=tools,
            instructions="test",
        )


class ApprovalPack(ScriptedPack):
    """A pack whose only tool stops the run and asks the browser."""

    def __init__(self, name="approver"):
        super().__init__(
            name=name,
            rounds=[
                tool_call_round({"name": "risky__publish", "arguments": "{}"}),
                text_round("nothing was published"),
            ],
        )

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console.toolsets import FunctionToolSource, Toolset

        from ifc_console_agents import Agent
        from ifc_console_agents.testing import ok_envelope

        source = FunctionToolSource(namespace="risky")

        @source.tool(requires_approval=True)
        async def publish() -> dict:
            return ok_envelope()

        self.built += 1
        return Agent(
            name=self.info.name,
            model=ScriptedAgentModel(list(self.rounds)),
            tools=await Toolset.build(source),
            instructions="test",
            limits=self.declared_limits,
        )


class ProgressPack(ScriptedPack):
    """A pack whose only tool says how far it has got before it returns."""

    def __init__(self, name="progressive"):
        super().__init__(
            name=name,
            rounds=[
                tool_call_round({"name": "slow__scan", "arguments": "{}"}),
                text_round("scanned"),
            ],
        )

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console.toolsets import FunctionToolSource, Toolset

        from ifc_console_agents import Agent
        from ifc_console_agents.agent import report_progress
        from ifc_console_agents.testing import ok_envelope

        source = FunctionToolSource(namespace="slow")

        @source.tool(tags={"read"})
        async def scan() -> dict:
            report_progress(2, 5, "reading walls")
            await asyncio.sleep(0.01)
            return ok_envelope({"scanned": 5}, returned=5)

        self.built += 1
        return Agent(
            name=self.info.name,
            model=ScriptedAgentModel(list(self.rounds)),
            tools=await Toolset.build(source),
            instructions="test",
            limits=self.declared_limits,
        )


class DocumentPack(ScriptedPack):
    def __init__(self, name="content-reader", rounds=None):
        super().__init__(name=name, features=("files",), rounds=rounds)

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console_agents import Agent

        self.built += 1
        tools = await runtime.tools(
            "list_project_documents",
            "search_ifc_knowledge",
            "get_knowledge_record",
        )
        return Agent(
            name=self.info.name,
            model=ScriptedAgentModel(list(self.rounds)),
            tools=tools,
            instructions="test",
            limits=self.declared_limits,
        )


def _client(core) -> TestClient:
    from ifc_console.mcp.server import build_http_app, build_mcp

    app = build_http_app(core, build_mcp(core))
    return TestClient(app, base_url="http://127.0.0.1")


def _auth(core) -> dict:
    return {"Authorization": f"Bearer {core.token}"}


def _events(response) -> list[dict]:
    events = []
    for line in response.text.split("\n\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _stream_body(**extra) -> dict:
    return {"agent": "scripted", "prompt": "hi", "provider": "local", "model": "m", **extra}


def _write_thread_record(directory: Path, thread_id: str, payload: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha256(thread_id.encode('utf-8')).hexdigest()}.json"
    path.write_text(json.dumps({"thread_id": thread_id, **payload}), encoding="utf-8")
    return path


@pytest.fixture
async def panel_core(core, work_model: Path):
    core.start_audit()
    await core.open_model(work_model)
    core.enable_chat()
    core.agent_packs.register(ScriptedPack())
    core.agent_packs.register(ScriptedPack(name="uploader", features=("files",)))
    return core


async def test_builtin_agents_appear_without_any_setup(core, work_model: Path):
    core.start_audit()
    await core.open_model(work_model)
    core.enable_chat()
    client = _client(core)
    payload = client.get("/api/agents", headers=_auth(core)).json()
    names = [agent["name"] for agent in payload["agents"]]
    assert "measurement" in names
    assert "docs" in names


async def test_routes_are_404_until_chat_is_on(core):
    client = _client(core)
    assert client.get("/api/agents", headers=_auth(core)).status_code == 404


async def test_listing_shows_active_packs(panel_core):
    client = _client(panel_core)
    payload = client.get("/api/agents", headers=_auth(panel_core)).json()
    names = [agent["name"] for agent in payload["agents"]]
    assert {"docs", "measurement", "scripted", "uploader"}.issubset(names)
    scripted = next(agent for agent in payload["agents"] if agent["name"] == "scripted")
    assert scripted["starters"] == ["say hello"]


async def test_custom_agent_builder_lists_blocks_and_persists_a_pack(panel_core):
    client = _client(panel_core)
    blocks = client.get("/api/agents/blocks", headers=_auth(panel_core)).json()["blocks"]
    assert {"documents", "measurements", "viewer"}.issubset(
        {block["name"] for block in blocks}
    )
    response = client.post(
        "/api/agents/custom",
        headers=_auth(panel_core),
        json={
            "title": "Envelope review",
            "description": "Review envelope evidence and measurements.",
            "instructions": "Cite the manual for each reported value.",
            "blocks": ["ifc-context", "documents", "measurements"],
            "starters": ["Review the selected walls"],
        },
    )
    assert response.status_code == 201
    created = response.json()["agent"]
    assert created["name"].startswith("custom-envelope-review")
    assert created["kind"] == "custom"
    listing = client.get("/api/agents", headers=_auth(panel_core)).json()["agents"]
    assert created["name"] in {agent["name"] for agent in listing}
    saved = panel_core.store.project_dir / ".ifc-console" / "agents" / "custom"
    assert list(saved.glob("custom-envelope-review*.json"))


async def test_custom_agent_workflow_is_validated_and_described(panel_core):
    client = _client(panel_core)
    response = client.post(
        "/api/agents/custom",
        headers=_auth(panel_core),
        json={
            "title": "Evidence workflow",
            "description": "Review model evidence before measuring.",
            "instructions": "Cite the source for every finding.",
            "blocks": ["ifc-context", "documents", "measurements"],
            "workflow": {
                "strategy": "evidence-first",
                "max_tool_rounds": 10,
                "max_tool_calls": 15,
            },
        },
    )
    assert response.status_code == 201
    name = response.json()["agent"]["name"]

    workspace = client.get(
        f"/api/agents/workspace?agent={name}", headers=_auth(panel_core)
    ).json()
    assert workspace["workflow"] == {
        "strategy": "evidence-first",
        "max_tool_rounds": 10,
        "max_tool_calls": 15,
    }
    # a pack's declared rounds are its own budget, not something the panel
    # silently halves down to the plain-chat setting
    assert workspace["limits"]["max_tool_rounds"] == 10
    assert workspace["limits"]["max_tool_calls"] == 15
    # the run budget, not chat.timeout_s, which only covers one provider call
    assert workspace["limits"]["timeout_s"] == panel_core.settings.chat.run_timeout_s

    invalid = client.post(
        "/api/agents/custom",
        headers=_auth(panel_core),
        json={
            "title": "Invalid budget",
            "description": "This agent must not be saved.",
            "instructions": "Inspect the model.",
            "blocks": ["ifc-context"],
            "workflow": {"max_tool_rounds": 0, "max_tool_calls": 4},
        },
    )
    assert invalid.status_code == 400
    assert "max_tool_rounds" in invalid.json()["error"]


async def test_custom_agent_content_access_updates_its_blueprint(panel_core):
    client = _client(panel_core)
    path = client.post(
        "/api/agents/content/upload?name=custom-manual.md",
        headers=_auth(panel_core),
        content=b"# Manual\n\ncustom agent evidence",
    ).json()["attachment"]["path"]
    created = client.post(
        "/api/agents/custom",
        headers=_auth(panel_core),
        json={
            "title": "Selected content",
            "description": "Reads only selected project references.",
            "instructions": "Cite the selected manual.",
            "blocks": ["documents"],
        },
    ).json()["agent"]

    saved = client.post(
        "/api/agents/content/access",
        headers=_auth(panel_core),
        json={"agent": created["name"], "mode": "selected", "paths": [path]},
    )
    assert saved.status_code == 200

    record = (
        panel_core.store.project_dir
        / ".ifc-console"
        / "agents"
        / "custom"
        / f"{created['name']}.json"
    )
    assert json.loads(record.read_text(encoding="utf-8"))["content_paths"] == [path]
    workspace = client.get(
        f"/api/agents/workspace?agent={created['name']}", headers=_auth(panel_core)
    ).json()
    assert workspace["content"]["access"] == {"mode": "selected", "paths": [path]}


async def test_stream_speaks_the_chat_vocabulary(panel_core):
    client = _client(panel_core)
    response = client.post(
        "/api/agents/stream", headers=_auth(panel_core), json=_stream_body()
    )
    assert response.status_code == 200
    events = _events(response)
    kinds = [event["type"] for event in events]
    assert kinds[0] == "thread"
    assert "content" in kinds
    assert kinds[-1] == "done"
    text = "".join(e.get("text", "") for e in events if e["type"] == "content")
    assert text == "hello from the pack"


async def test_thread_continuity_reuses_the_agent(panel_core):
    pack = ScriptedPack(
        name="threaded",
        rounds=[text_round("first"), text_round("second")],
    )
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)
    first = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="threaded"),
        )
    )
    thread_id = first[0]["id"]
    second = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="threaded", thread_id=thread_id),
        )
    )
    assert second[0]["id"] == thread_id
    assert pack.built == 1
    text = "".join(e.get("text", "") for e in second if e["type"] == "content")
    assert text == "second"


async def test_changed_agent_configuration_forks_instead_of_loading_hidden_context(
    panel_core,
):
    pack = ScriptedPack(name="configured")
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)
    first = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="configured", model="model-a"),
        )
    )
    original_id = first[0]["id"]

    changed = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(
                agent="configured",
                model="model-b",
                thread_id=original_id,
            ),
        )
    )

    assert changed[0]["id"] != original_id
    assert pack.built == 2


async def test_changed_blueprint_forks_after_the_in_memory_thread_is_gone(panel_core):
    from ifc_console_agents.blueprints import AgentBlueprint, AgentWorkflow

    pack = ScriptedPack(name="blueprinted")
    pack.blueprint = AgentBlueprint(
        name="custom-blueprinted",
        title="Blueprinted",
        description="A mutable blueprint used by this continuity test.",
        instructions="Use the original procedure.",
        blocks=("ifc-context",),
    )
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)
    first = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="blueprinted"),
        )
    )
    original_id = first[0]["id"]

    panel_core.agent_panel.threads.clear()
    pack.blueprint = pack.blueprint.model_copy(
        update={
            "instructions": "Use the revised procedure.",
            "workflow": AgentWorkflow(
                strategy="evidence-first",
                max_tool_rounds=3,
                max_tool_calls=7,
            ),
        }
    )
    changed = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="blueprinted", thread_id=original_id),
        )
    )

    assert changed[0]["id"] != original_id
    assert pack.built == 2


async def test_workspace_reports_host_pack_declared_limits(panel_core):
    pack = ScriptedPack(
        name="bounded",
        limits=AgentLimits(max_tool_rounds=3, max_tool_calls=5),
    )
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)

    workspace = client.get(
        "/api/agents/workspace?agent=bounded", headers=_auth(panel_core)
    ).json()

    assert workspace["limits"]["max_tool_rounds"] == 3
    assert workspace["limits"]["max_tool_calls"] == 5

    events = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="bounded"),
        )
    )
    runtime_limits = panel_core.agent_panel.threads[events[0]["id"]].agent.limits
    assert runtime_limits.max_tool_rounds == 3
    assert runtime_limits.max_tool_calls == 5


async def test_a_preset_keeps_the_rounds_it_declares(panel_core):
    from ifc_console_agents.presets import PRESET_BY_NAME

    preset = PRESET_BY_NAME["measurement"]
    assert preset.max_tool_rounds > panel_core.settings.chat.max_tool_rounds
    client = _client(panel_core)
    workspace = client.get(
        "/api/agents/workspace?agent=measurement", headers=_auth(panel_core)
    ).json()
    assert workspace["limits"]["max_tool_rounds"] == preset.max_tool_rounds


async def test_a_finished_run_only_denies_its_own_pending_approvals():
    """One conversation's teardown must not answer another one's card."""
    from ifc_console_agents.panel import AgentPanelState, PanelApprovalHandler

    state = AgentPanelState()
    first = object()
    second = object()

    async def ask(owner, request_id):
        handler = PanelApprovalHandler(state, owner=owner)
        return await handler.request(SimpleNamespace(request_id=request_id))

    asking_first = asyncio.create_task(ask(first, "req-a"))
    asking_second = asyncio.create_task(ask(second, "req-b"))
    for _ in range(100):
        if len(state.pending_approvals) == 2:
            break
        await asyncio.sleep(0)
    assert len(state.pending_approvals) == 2

    assert state.deny_owned(first) == 1
    assert (await asking_first).approved is False
    assert not asking_second.done()

    state.pending_approvals["req-b"][1].set_result(True)
    assert (await asking_second).approved is True


async def test_panel_lru_never_evicts_a_running_thread():
    from ifc_console_agents.panel import _MAX_THREADS, AgentPanelState

    state = AgentPanelState()
    for index in range(_MAX_THREADS):
        state.remember(f"thread-{index}", SimpleNamespace())
    state.active_streams["thread-0"] = {asyncio.current_task()}

    state.remember("new-thread", SimpleNamespace())

    assert "thread-0" in state.threads
    assert "thread-1" not in state.threads
    assert "new-thread" in state.threads


async def test_one_conversation_finishing_leaves_another_approval_waiting(panel_core):
    panel_core.agent_packs.register(ApprovalPack())
    outcome: dict[str, object] = {}

    with _client(panel_core) as client:
        def ask_for_approval() -> None:
            try:
                outcome["response"] = client.post(
                    "/api/agents/stream",
                    headers=_auth(panel_core),
                    json=_stream_body(agent="approver"),
                )
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=ask_for_approval, daemon=True)
        worker.start()
        try:
            state = None
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = getattr(panel_core, "agent_panel", None)
                if state is not None and state.pending_approvals:
                    break
                time.sleep(0.02)
            assert state is not None and state.pending_approvals, (
                "the approving run never asked"
            )
            request_id = next(iter(state.pending_approvals))

            other = client.post(
                "/api/agents/stream", headers=_auth(panel_core), json=_stream_body()
            )
            assert other.status_code == 200
            assert _events(other)[-1]["type"] == "done"

            waiting = state.pending_approvals.get(request_id)
            assert waiting is not None and not waiting[1].done(), (
                "another run's teardown answered this approval"
            )

            decided = client.post(
                "/api/agents/approve",
                headers=_auth(panel_core),
                json={"request_id": request_id, "approved": False},
            )
            assert decided.status_code == 200
        finally:
            worker.join(timeout=5)

    assert not worker.is_alive()
    events = _events(outcome["response"])
    denied = next(event for event in events if event["type"] == "tool_result")
    assert denied["ok"] is False
    assert denied["summary"] == "APPROVAL_REQUIRED"


async def test_panel_threads_survive_a_server_state_rebuild_and_can_be_deleted(panel_core):
    client = _client(panel_core)
    first = _events(
        client.post("/api/agents/stream", headers=_auth(panel_core), json=_stream_body())
    )
    thread_id = first[0]["id"]
    thread_dir = panel_core.store.project_dir / ".ifc-console" / "agents" / "threads"
    assert list(thread_dir.glob("*.json"))

    panel_core.agent_panel.threads.clear()
    resumed = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(thread_id=thread_id),
        )
    )
    assert resumed[0]["id"] == thread_id

    deleted = client.post(
        "/api/agents/thread/delete",
        headers=_auth(panel_core),
        json={"thread_id": thread_id},
    )
    assert deleted.status_code == 200
    assert not list(thread_dir.glob("*.json"))


async def test_bulk_thread_clear_is_idempotent_and_preserves_non_panel_records(panel_core):
    thread_dir = panel_core.store.project_dir / ".ifc-console" / "agents" / "threads"
    with _client(panel_core) as client:
        response = client.post(
            "/api/agents/stream", headers=_auth(panel_core), json=_stream_body()
        )
        assert response.status_code == 200

        corrupt_panel = _write_thread_record(
            thread_dir,
            "panel-orphan",
            {"version": "broken", "messages": "not-a-list"},
        )
        non_panel = _write_thread_record(
            thread_dir,
            "sdk-thread",
            {"version": "1", "messages": []},
        )

        cleared = client.post("/api/agents/threads/clear", headers=_auth(panel_core))
        assert cleared.status_code == 200
        assert cleared.json() == {"ok": True, "removed_threads": 2, "cancelled_runs": 0}
        assert not corrupt_panel.exists()
        assert non_panel.exists()
        assert not panel_core.agent_panel.threads

        cleared_again = client.post("/api/agents/threads/clear", headers=_auth(panel_core))
        assert cleared_again.status_code == 200
        assert cleared_again.json() == {
            "ok": True,
            "removed_threads": 0,
            "cancelled_runs": 0,
        }
        assert non_panel.exists()


async def test_bulk_thread_clear_cancels_an_active_stream_before_unlinking(panel_core):
    started = threading.Event()
    release = threading.Event()
    panel_core.agent_packs.register(DelayedPack(started, release))
    thread_dir = panel_core.store.project_dir / ".ifc-console" / "agents" / "threads"
    outcome: dict[str, object] = {}

    with _client(panel_core) as client:
        def send_delayed() -> None:
            try:
                outcome["response"] = client.post(
                    "/api/agents/stream",
                    headers=_auth(panel_core),
                    json=_stream_body(agent="delayed"),
                )
            except BaseException as exc:  # cancellation may close the test response
                outcome["error"] = exc

        worker = threading.Thread(target=send_delayed, daemon=True)
        worker.start()
        try:
            assert started.wait(timeout=5), "the delayed provider did not start"
            assert list(thread_dir.glob("*.json")), "the prompt was not persisted"

            cleared = client.post("/api/agents/threads/clear", headers=_auth(panel_core))
            assert cleared.status_code == 200
            assert cleared.json()["cancelled_runs"] == 1
            assert cleared.json()["removed_threads"] == 1
            assert not list(thread_dir.glob("*.json"))
        finally:
            release.set()
            worker.join(timeout=5)

    assert not worker.is_alive()
    assert not list(thread_dir.glob("*.json")), "a cancelled stream recreated its thread"


async def test_content_access_change_cancels_runs_using_the_old_grant(panel_core):
    started = threading.Event()
    release = threading.Event()
    panel_core.agent_packs.register(DelayedPack(started, release))
    outcome: dict[str, object] = {}

    with _client(panel_core) as client:
        def send_delayed() -> None:
            try:
                outcome["response"] = client.post(
                    "/api/agents/stream",
                    headers=_auth(panel_core),
                    json=_stream_body(agent="delayed"),
                )
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=send_delayed, daemon=True)
        worker.start()
        try:
            assert started.wait(timeout=5), "the delayed provider did not start"
            changed = client.post(
                "/api/agents/content/access",
                headers=_auth(panel_core),
                json={"agent": "delayed", "mode": "selected", "paths": []},
            )
            assert changed.status_code == 200
            assert changed.json()["cancelled_runs"] == 1
            assert not panel_core.agent_panel.threads
        finally:
            release.set()
            worker.join(timeout=5)

    assert not worker.is_alive()


async def test_individual_thread_delete_cannot_be_undone_by_an_active_stream(panel_core):
    started = threading.Event()
    release = threading.Event()
    panel_core.agent_packs.register(DelayedPack(started, release))
    thread_dir = panel_core.store.project_dir / ".ifc-console" / "agents" / "threads"
    outcome: dict[str, object] = {}

    with _client(panel_core) as client:
        def send_delayed() -> None:
            try:
                outcome["response"] = client.post(
                    "/api/agents/stream",
                    headers=_auth(panel_core),
                    json=_stream_body(agent="delayed"),
                )
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=send_delayed, daemon=True)
        worker.start()
        try:
            assert started.wait(timeout=5), "the delayed provider did not start"
            thread_id = next(iter(panel_core.agent_panel.threads))
            deleted = client.post(
                "/api/agents/thread/delete",
                headers=_auth(panel_core),
                json={"thread_id": thread_id},
            )
            assert deleted.status_code == 200
            assert deleted.json() == {"ok": True, "removed": True, "cancelled_runs": 1}
            assert not list(thread_dir.glob("*.json"))
        finally:
            release.set()
            worker.join(timeout=5)

    assert not worker.is_alive()
    assert not list(thread_dir.glob("*.json")), "a cancelled stream recreated its thread"


async def test_interrupting_a_run_records_that_it_was_stopped(panel_core):
    """Stop has to leave the thread agreeing with what the reader saw."""
    started = threading.Event()
    release = threading.Event()
    panel_core.agent_packs.register(DelayedPack(started, release))
    outcome: dict[str, object] = {}

    with _client(panel_core) as client:
        def send_delayed() -> None:
            try:
                outcome["response"] = client.post(
                    "/api/agents/stream",
                    headers=_auth(panel_core),
                    json=_stream_body(agent="delayed"),
                )
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=send_delayed, daemon=True)
        worker.start()
        try:
            assert started.wait(timeout=5), "the delayed provider did not start"
            thread_id = next(iter(panel_core.agent_panel.threads))
            stopped = client.post(
                "/api/agents/interrupt",
                headers=_auth(panel_core),
                json={"thread_id": thread_id},
            )
            assert stopped.status_code == 200
            assert stopped.json() == {"ok": True, "cancelled_runs": 1, "noted": True}
        finally:
            release.set()
            worker.join(timeout=5)

    from ifc_console_agents.storage import JsonThreadStore

    directory = panel_core.store.project_dir / ".ifc-console" / "agents" / "threads"
    saved = await JsonThreadStore(directory).load(thread_id)
    assert [message.role for message in saved] == ["user", "assistant"]
    assert "stopped this run" in saved[-1].text


async def test_interrupt_rejects_an_id_that_is_not_a_panel_thread(panel_core):
    client = _client(panel_core)
    refused = client.post(
        "/api/agents/interrupt",
        headers=_auth(panel_core),
        json={"thread_id": "../etc/passwd"},
    )
    assert refused.status_code == 400


async def test_truncating_a_thread_removes_the_attempt_being_retried(panel_core):
    pack = ScriptedPack(
        name="retryable",
        rounds=[text_round("first answer"), text_round("second answer")],
    )
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)
    first = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="retryable"),
        )
    )
    thread_id = first[0]["id"]
    client.post(
        "/api/agents/stream",
        headers=_auth(panel_core),
        json=_stream_body(agent="retryable", thread_id=thread_id),
    )

    from ifc_console_agents.storage import JsonThreadStore

    store = JsonThreadStore(panel_core.store.project_dir / ".ifc-console" / "agents" / "threads")
    assert len(await store.load(thread_id)) == 4

    cut = client.post(
        "/api/agents/thread/truncate",
        headers=_auth(panel_core),
        json={"thread_id": thread_id, "keep_turns": 1},
    )

    assert cut.status_code == 200
    assert cut.json() == {"ok": True, "removed": 2, "messages": 2, "turns": 1}
    kept = await store.load(thread_id)
    assert [message.text for message in kept] == ["hi", "first answer"]
    # keeping more turns than exist is a no-op, not an error
    again = client.post(
        "/api/agents/thread/truncate",
        headers=_auth(panel_core),
        json={"thread_id": thread_id, "keep_turns": 9},
    )
    assert again.json()["removed"] == 0


async def test_truncate_refuses_a_negative_turn_count(panel_core):
    client = _client(panel_core)
    events = _events(
        client.post("/api/agents/stream", headers=_auth(panel_core), json=_stream_body())
    )
    refused = client.post(
        "/api/agents/thread/truncate",
        headers=_auth(panel_core),
        json={"thread_id": events[0]["id"], "keep_turns": -1},
    )
    assert refused.status_code == 400


async def test_tool_calls_stream_as_chips(panel_core):
    pack = ScriptedPack(
        name="tooluser",
        rounds=[
            tool_call_round({"name": "query_elements", "arguments": '{"query": "IfcWall"}'}),
            text_round("done"),
        ],
    )
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)
    events = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="tooluser"),
        )
    )
    call = next(e for e in events if e["type"] == "tool_call")
    result = next(e for e in events if e["type"] == "tool_result")
    assert call["name"] == "query_elements"
    assert result["ok"] is True
    assert "row" in result["summary"]
    # The panel draws the call where it ran, so the events have to carry what
    # the model asked for and what the console returned.
    assert "IfcWall" in call["arguments"]
    assert result["preview"].strip()
    assert result["rows"] is None or isinstance(result["rows"], int)


async def test_a_running_tool_narrates_itself_to_the_panel(panel_core):
    panel_core.agent_packs.register(ProgressPack())
    client = _client(panel_core)
    events = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="progressive"),
        )
    )

    progress = [event for event in events if event["type"] == "tool_progress"]
    assert progress, "a long tool must not look like a hang"
    assert progress[0]["name"] == "slow__scan"
    assert progress[0]["done"] == 2
    assert progress[0]["total"] == 5
    assert progress[0]["note"] == "reading walls"
    kinds = [event["type"] for event in events]
    assert kinds.index("tool_call") < kinds.index("tool_progress") < kinds.index("tool_result")


async def test_an_approval_card_carries_the_change_it_would_make():
    """The reviewer decides before the tool runs, so from arguments alone."""
    from ifc_console_agents.models import AgentEvent, ApprovalRequest
    from ifc_console_agents.panel import _event_payloads

    arguments = {
        "global_ids": ["1A", "2B", "3C"],
        "metric": "area",
        "value": 12.5,
        "unit": "m2",
        "method": "geometry extent",
        "source": "spec.pdf p4",
        "confidence": "high",
    }
    event = AgentEvent(
        type="approval_requested",
        run_id="run-1",
        thread_id="panel-1",
        tool_call_id="call-1",
        tool_name="measure__propose_measured_value",
        arguments=arguments,
        approval=ApprovalRequest(
            request_id="req-1",
            run_id="run-1",
            thread_id="panel-1",
            tool_call_id="call-1",
            tool_name="measure__propose_measured_value",
            arguments=arguments,
        ),
    )

    payload = _event_payloads(event)[0]

    assert payload["type"] == "approval"
    proposal = payload["proposal"]
    assert proposal["pset"] == "IfcConsole_AI_Measurements"
    assert proposal["property"] == "MeasuredArea"
    assert proposal["elements"] == 3
    assert proposal["value"] == 12.5
    assert proposal["unit"] == "m2"
    assert proposal["confidence"] == "high"
    assert proposal["targets"] == ["1A", "2B", "3C"]


async def test_a_paged_tool_result_says_how_much_of_it_arrived():
    from ifc_console_agents.models import AgentEvent
    from ifc_console_agents.panel import _event_payloads

    event = AgentEvent(
        type="tool_call_finished",
        run_id="run-1",
        thread_id="panel-1",
        tool_call_id="call-1",
        tool_name="query_elements",
        result={
            "ok": True,
            "data": {
                "truncation": {"key": "rows", "kept": 50, "of": 312, "next_offset": 50},
                "rows": [],
            },
            "meta": {"returned": 50, "total": 312, "truncated": True},
        },
    )

    payload = _event_payloads(event)[0]

    assert payload["shown"] == 50
    assert payload["of"] == 312
    assert payload["total"] == 312
    assert payload["next_offset"] == 50
    assert payload["truncated"] is True


async def test_a_delegated_run_labels_its_events_with_their_depth():
    from ifc_console_agents.models import AgentEvent
    from ifc_console_agents.panel import _event_payloads

    event = AgentEvent(
        type="text_delta",
        run_id="child-1",
        thread_id="panel-1::sub::review_fire",
        text="child speaking",
        depth=1,
        parent_run_id="run-1",
    )

    assert _event_payloads(event)[0]["depth"] == 1


async def test_a_failed_tool_reports_its_message_to_the_panel(panel_core):
    pack = ScriptedPack(
        name="badtool",
        rounds=[
            tool_call_round({"name": "query_elements", "arguments": '{"query": "NotAnIfcClass"}'}),
            text_round("done"),
        ],
    )
    panel_core.agent_packs.register(pack)
    client = _client(panel_core)
    events = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent="badtool"),
        )
    )
    result = next(e for e in events if e["type"] == "tool_result")
    if result["ok"]:
        pytest.skip("this selector is valid in the test model")
    assert result["summary"]
    assert result["detail"], "a failed call must say why, not just 'failed'"


async def test_plain_chat_has_a_workspace_of_its_own(panel_core):
    client = _client(panel_core)
    response = client.get("/api/agents/workspace?agent=", headers=_auth(panel_core))
    assert response.status_code == 200
    payload = response.json()
    assert payload["plain"] is True
    assert payload["agent"]["name"] == ""
    assert payload["tools"], "plain chat holds the console's whole tool surface"
    assert any(stage["available"] for stage in payload["stages"])
    assert payload["blocks"] == []
    assert payload["files"] == []


async def test_workspace_tools_include_their_expandable_contract(panel_core):
    client = _client(panel_core)
    response = client.get(
        "/api/agents/workspace?agent=general", headers=_auth(panel_core)
    )
    assert response.status_code == 200
    tool = next(
        row for row in response.json()["tools"] if row["name"] == "query_elements"
    )

    assert tool["description"]
    assert tool["summary"] in tool["description"]
    assert tool["input_schema"]["type"] == "object"
    assert "query" in tool["input_schema"]["properties"]
    assert tool["required_capabilities"]
    assert tool["source"]
    assert "output_schema" not in tool


async def test_unknown_agent_is_a_clear_404(panel_core):
    client = _client(panel_core)
    response = client.post(
        "/api/agents/stream",
        headers=_auth(panel_core),
        json=_stream_body(agent="nope"),
    )
    assert response.status_code == 404
    assert "scripted" in response.json()["hint"]


async def test_upload_is_gated_by_the_feature(panel_core):
    client = _client(panel_core)
    denied = client.post(
        "/api/agents/upload?agent=scripted&name=notes.md",
        headers=_auth(panel_core),
        content=b"# notes",
    )
    assert denied.status_code == 403


async def test_upload_stream_is_bounded_without_content_length(panel_core, monkeypatch):
    monkeypatch.setattr("ifc_console_agents.panel._MAX_UPLOAD_BYTES", 5)
    response = _client(panel_core).post(
        "/api/agents/upload?agent=uploader&name=notes.md",
        headers=_auth(panel_core),
        content=iter((b"123", b"456")),
    )
    assert response.status_code == 413


async def test_turn_upload_is_indexed_but_hidden_and_denied_on_a_later_run(panel_core):
    client = _client(panel_core)
    response = client.post(
        "/api/agents/upload?agent=uploader&name=manual.md",
        headers=_auth(panel_core),
        content=b"# Manual\n\n## Walls\n\nlayers rule the thickness",
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["documents"] == 1
    assert payload["records"] >= 1
    assert payload["indexed"] is True
    assert payload["attachment"]["path"].endswith("manual.md")
    assert "/.turns/" in payload["attachment"]["path"]
    assert payload["files"] == []
    hits = panel_core.project_knowledge.search("wall thickness layers")
    assert hits and hits[0]["meta"]["path"].endswith("manual.md")
    path = payload["attachment"]["path"]
    key = hits[0]["key"]

    library = client.get(
        "/api/agents/content?agent=uploader", headers=_auth(panel_core)
    ).json()
    assert path not in {row["path"] for row in library["files"]}

    pack = DocumentPack(
        rounds=[
            tool_call_round(
                {"name": "get_knowledge_record", "arguments": json.dumps({"key": key})}
            ),
            text_round("done"),
            tool_call_round(
                {"name": "get_knowledge_record", "arguments": json.dumps({"key": key})}
            ),
            text_round("done again"),
        ]
    )
    panel_core.agent_packs.register(pack)
    attached = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(
                agent=pack.info.name,
                model="turn-lifecycle",
                attachments=[path],
            ),
        )
    )
    attached_result = next(event for event in attached if event["type"] == "tool_result")
    assert attached_result["ok"] is True
    thread_id = next(event["id"] for event in attached if event["type"] == "thread")

    later = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(
                agent=pack.info.name,
                model="turn-lifecycle",
                thread_id=thread_id,
            ),
        )
    )
    later_result = next(event for event in later if event["type"] == "tool_result")
    assert later_result["summary"] == "CONTENT_ACCESS_DENIED"


async def test_agent_workspace_content_library_persists_selected_access(panel_core):
    client = _client(panel_core)
    first = client.post(
        "/api/agents/content/upload?name=public.md",
        headers=_auth(panel_core),
        content=b"# Public\n\nshared wall guidance",
    ).json()["attachment"]["path"]
    second = client.post(
        "/api/agents/content/upload?name=private.md",
        headers=_auth(panel_core),
        content=b"# Private\n\nrestricted cost guidance",
    ).json()["attachment"]["path"]

    saved = client.post(
        "/api/agents/content/access",
        headers=_auth(panel_core),
        json={"agent": "uploader", "mode": "selected", "paths": [first]},
    )
    assert saved.status_code == 200
    assert saved.json()["access"] == {"mode": "selected", "paths": [first]}

    content = client.get(
        "/api/agents/content?agent=uploader", headers=_auth(panel_core)
    ).json()
    by_path = {row["path"]: row for row in content["files"]}
    assert by_path[first]["allowed"] is True
    assert by_path[second]["allowed"] is False
    assert {"name", "path", "media", "size_bytes", "indexed", "allowed"}.issubset(
        by_path[first]
    )

    workspace = client.get(
        "/api/agents/workspace?agent=uploader", headers=_auth(panel_core)
    ).json()
    assert workspace["content"]["access"]["mode"] == "selected"
    assert [row["path"] for row in workspace["files"]] == [first]

    access_file = (
        panel_core.store.project_dir
        / ".ifc-console"
        / "agents"
        / "content-access.json"
    )
    assert json.loads(access_file.read_text(encoding="utf-8"))["agents"]["uploader"] == [
        first
    ]


async def test_panel_enforces_content_selection_and_turn_attachments(panel_core):
    client = _client(panel_core)
    allowed = client.post(
        "/api/agents/content/upload?name=allowed.md",
        headers=_auth(panel_core),
        content=b"# Allowed\n\nallowed wall guidance",
    ).json()["attachment"]["path"]
    denied = client.post(
        "/api/agents/content/upload?name=denied.md",
        headers=_auth(panel_core),
        content=b"# Denied\n\nprivate wall guidance",
    ).json()["attachment"]["path"]
    key = next(
        hit["key"]
        for hit in panel_core.project_knowledge.search("private wall guidance")
        if hit["meta"]["path"] == denied
    )
    pack = DocumentPack(
        rounds=[
            tool_call_round(
                {"name": "get_knowledge_record", "arguments": json.dumps({"key": key})}
            ),
            text_round("done"),
        ]
    )
    panel_core.agent_packs.register(pack)
    response = client.post(
        "/api/agents/content/access",
        headers=_auth(panel_core),
        json={"agent": pack.info.name, "mode": "selected", "paths": [allowed]},
    )
    assert response.status_code == 200

    blocked = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(agent=pack.info.name),
        )
    )
    blocked_result = next(event for event in blocked if event["type"] == "tool_result")
    assert blocked_result["summary"] == "CONTENT_ACCESS_DENIED"

    attached = _events(
        client.post(
            "/api/agents/stream",
            headers=_auth(panel_core),
            json=_stream_body(
                agent=pack.info.name,
                model="attachment-run",
                attachments=[denied],
            ),
        )
    )
    attached_result = next(event for event in attached if event["type"] == "tool_result")
    assert attached_result["ok"] is True
    assert "private wall guidance" in attached_result["preview"]


async def test_reference_ledger_tracks_images_and_manual_folder_drops(panel_core):
    panel_core.agent_files.directory.mkdir(parents=True, exist_ok=True)
    image = panel_core.agent_files.directory / "detail.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
    client = _client(panel_core)
    response = client.get(
        "/api/agents/files?agent=measurement",
        headers=_auth(panel_core),
    )
    assert response.status_code == 200
    payload = response.json()
    row = next(item for item in payload["files"] if item["name"] == "detail.png")
    assert row["media"] == "image"
    assert row["indexed"] is True


async def test_upload_rejects_unsupported_names(panel_core):
    client = _client(panel_core)
    response = client.post(
        "/api/agents/upload?agent=uploader&name=model.exe",
        headers=_auth(panel_core),
        content=b"nope",
    )
    assert response.status_code == 400


async def test_skills_import_accepts_external_markdown(panel_core):
    client = _client(panel_core)
    body = (
        b"---\nname: sheet-pile-profile\ndescription: Measure a sheet pile\n"
        b"applies_to: IfcMember\n---\n\n1. analyze_element_geometry\n"
    )
    response = client.post(
        "/api/agents/skills/import?name=Sheet Pile.md",
        headers=_auth(panel_core),
        content=body,
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["imported"]["name"] == "sheet-pile-profile"
    assert [row["name"] for row in payload["skills"]] == ["sheet-pile-profile"]
    saved = (
        panel_core.store.project_dir / ".ifc-console" / "agents" / "skills"
        / "sheet-pile-profile.md"
    )
    assert saved.is_file()
    assert "analyze_element_geometry" in saved.read_text(encoding="utf-8")

    rejected = client.post(
        "/api/agents/skills/import?name=notes.txt",
        headers=_auth(panel_core),
        content=b"not markdown",
    )
    assert rejected.status_code == 400


class RecordingPack(ScriptedPack):
    """A scripted pack that keeps its models so tests can read what they saw."""

    def __init__(self, name="recorder"):
        super().__init__(name=name)
        self.models = []

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console_agents import Agent

        self.built += 1
        scripted = ScriptedAgentModel(list(self.rounds))
        self.models.append(scripted)
        tools = await runtime.tools("get_ifc_project_info")
        return Agent(
            name=self.info.name,
            model=scripted,
            tools=tools,
            instructions="test",
            limits=self.declared_limits,
        )


async def test_the_viewer_selection_rides_along_with_the_prompt(panel_core):
    """'This wall' should not cost a get_viewer_selection round."""

    class _Ws:
        async def send_text(self, text: str) -> None:
            return None

    pack = RecordingPack()
    panel_core.agent_packs.register(pack)
    hub = panel_core.viewer_hub
    viewer_client = hub.register(_Ws())
    viewer_client.view_model_id = panel_core.models.active_id
    await hub.handle_frame(
        viewer_client,
        {
            "type": "selection",
            "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"],
            "model_id": panel_core.models.active_id,
        },
    )

    client = _client(panel_core)
    response = client.post(
        "/api/agents/stream",
        headers=_auth(panel_core),
        json=_stream_body(agent="recorder", prompt="How thick is this wall?"),
    )
    assert response.status_code == 200
    seen = pack.models[-1].turns[0]["messages"]
    user_text = next(m.text for m in reversed(seen) if m.role == "user")
    assert "Viewer context" in user_text
    assert "2O2Fr$t4X7Zf8NOew3FL9r" in user_text
    assert "How thick is this wall?" in user_text


async def test_selections_from_two_ifc_files_ride_with_one_prompt(
    panel_core, work_model: Path, tmp_path: Path
):
    class _Ws:
        async def send_text(self, text: str) -> None:
            return None

    pack = RecordingPack()
    panel_core.agent_packs.register(pack)
    active_id = panel_core.models.active_id
    annex = tmp_path / "annex.ifc"
    shutil.copy2(work_model, annex)
    annex_id = await panel_core.open_model(annex, attach=True, alias="annex")
    hub = panel_core.viewer_hub
    viewer_client = hub.register(_Ws())
    await hub.handle_frame(
        viewer_client,
        {
            "type": "selection",
            "guids": ["annex-guid"],
            "model_id": annex_id,
            "selections": [
                {"model_id": active_id, "guids": ["main-guid"]},
                {"model_id": annex_id, "guids": ["annex-guid"]},
            ],
        },
    )

    response = _client(panel_core).post(
        "/api/agents/stream",
        headers=_auth(panel_core),
        json=_stream_body(agent="recorder", prompt="Compare these selected objects."),
    )
    assert response.status_code == 200
    messages = pack.models[-1].turns[0]["messages"]
    user_text = next(message.text for message in reversed(messages) if message.role == "user")
    assert f"model_id={active_id}" in user_text and "main-guid" in user_text
    assert f"model_id={annex_id}" in user_text and "annex-guid" in user_text
