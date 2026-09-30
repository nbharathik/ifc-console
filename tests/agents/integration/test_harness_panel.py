"""An ACP engine behind the agent panel: the scripted engine as a subprocess."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ifc_console.agents.harness.testing import ENGINE_NAME
from ifc_console.agents.harness.workspace import runs_root

pytestmark = pytest.mark.asyncio

PROVIDER = f"harness:{ENGINE_NAME}"


def _engine_row(**extra) -> dict:
    return {
        "name": ENGINE_NAME,
        "command": sys.executable,
        "args": ["-m", "ifc_console.agents.harness.testing"],
        "mode": "ifc",
        **extra,
    }


def _write_settings(home: Path, engines: list[dict]) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.json").write_text(
        json.dumps({"harness": {"enabled": True, "engines": engines}}), encoding="utf-8"
    )


@pytest.fixture
def engine_home(home: Path) -> Path:
    _write_settings(home, [_engine_row()])
    return home


@pytest.fixture
async def engine_core(engine_home: Path, request, work_model: Path):
    core = request.getfixturevalue("core")
    core.start_audit()
    await core.open_model(work_model)
    core.enable_chat()
    return core


def _client(core) -> TestClient:
    from ifc_console.mcp.server import build_http_app, build_mcp

    return TestClient(build_http_app(core, build_mcp(core)), base_url="http://127.0.0.1")


def _auth(core) -> dict:
    return {"Authorization": f"Bearer {core.token}"}


def _events(response) -> list[dict]:
    return [
        json.loads(line[6:]) for line in response.text.split("\n\n") if line.startswith("data: ")
    ]


def _body(prompt: str, **extra) -> dict:
    return {"agent": "general", "prompt": prompt, "provider": PROVIDER, "model": "default", **extra}


def _wait_for_approval(core, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = getattr(core, "agent_panel", None)
        if state is not None and state.pending_approvals:
            return next(iter(state.pending_approvals))
        time.sleep(0.02)
    raise AssertionError("the engine run never asked for permission")


def _stream_with_approval(core, client: TestClient, body: dict, approved: bool) -> list[dict]:
    outcome: dict = {}

    def ask() -> None:
        try:
            outcome["response"] = client.post("/api/agents/stream", headers=_auth(core), json=body)
        except BaseException as exc:  # reported by the caller
            outcome["error"] = exc

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    try:
        request_id = _wait_for_approval(core)
        decided = client.post(
            "/api/agents/approve",
            headers=_auth(core),
            json={"request_id": request_id, "approved": approved},
        )
        assert decided.status_code == 200
    finally:
        worker.join(timeout=30)
    assert not worker.is_alive(), "the engine run did not finish"
    if "error" in outcome:
        raise outcome["error"]
    assert outcome["response"].status_code == 200
    return _events(outcome["response"])


async def test_engine_is_listed_as_a_provider_with_one_model(engine_core):
    client = _client(engine_core)
    providers = client.get("/api/chat/providers", headers=_auth(engine_core)).json()["providers"]
    row = next(p for p in providers if p["id"] == PROVIDER)
    assert row["family"] == "harness" and row["needs_key"] is False and row["installed"] is True
    assert row["suggested_model"] == "default"

    models = client.post(
        "/api/chat/models", headers=_auth(engine_core), json={"provider": PROVIDER}
    )
    assert models.status_code == 200 and models.json()["models"] == ["default"]

    chosen = client.post(
        "/api/chat/select",
        headers=_auth(engine_core),
        json={"provider": PROVIDER, "model": "default"},
    )
    assert chosen.status_code == 200 and engine_core.chat.provider == PROVIDER


async def test_engine_run_streams_tool_call_approval_and_answer(engine_core):
    # one client for the whole test: its event loop is where the engine lives
    with _client(engine_core) as client:
        events = _stream_with_approval(engine_core, client, _body("how many walls"), True)
        kinds = [event["type"] for event in events]
        assert kinds[0] == "thread" and kinds[-1] == "done"
        assert "reasoning" in kinds and "usage" in kinds
        call = next(e for e in events if e["type"] == "tool_call")
        assert call["name"] == "get_session_status"
        approval = next(e for e in events if e["type"] == "approval")
        assert approval["name"] == "get_session_status" and approval["request_id"]
        decided = next(e for e in events if e["type"] == "approval_decided")
        assert decided["approved"] is True and decided["decided_by"] == "chat-panel"
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["ok"] is True and result["summary"] == "ok"
        answer = "".join(e["text"] for e in events if e["type"] == "content")
        assert answer.startswith(
            "checking the session done. echo: [ifc-console session instructions]"
        )
        usage = next(e for e in events if e["type"] == "usage")
        assert usage == {"type": "usage", "in": 2, "out": 1}

        state = engine_core.agent_panel
        thread_id = events[0]["id"]
        assert thread_id.startswith("panel-")
        [session] = state.harness.sessions.values()
        assert session.alive and session.turns == 1
        log_path = runs_root(engine_core.store.home) / session.cwd.name / "engine.stderr.log"
        log = log_path.read_text()
        assert "servers=1" in log and "mode=ifc" in log

        # the same thread reuses the session: no instructions block the second time
        second = _stream_with_approval(
            engine_core, client, _body("and doors", thread_id=thread_id), True
        )
        assert second[0]["id"] == thread_id
        answer = "".join(e["text"] for e in second if e["type"] == "content")
        assert answer.endswith("echo: and doors")
        assert session.turns == 2

        cleared = client.post("/api/agents/threads/clear", headers=_auth(engine_core))
        assert cleared.status_code == 200
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (
            state.harness.sessions or session.alive or log_path.parent.exists()
        ):
            time.sleep(0.05)
        assert not state.harness.sessions and not session.alive
        assert not log_path.parent.exists(), "the run folder outlived its session"


async def test_denied_permission_reaches_the_engine(engine_core):
    with _client(engine_core) as client:
        events = _stream_with_approval(engine_core, client, _body("run bash please"), False)
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["name"] == "bash" and "ls" in call["arguments"]
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["ok"] is False and result["summary"] == "TOOL_FAILED"
    assert result["detail"] == "permission denied"
    answer = "".join(e["text"] for e in events if e["type"] == "content")
    assert answer.endswith("the tool was denied.")


async def test_refusal_and_missing_engine_are_reported(engine_core):
    client = _client(engine_core)
    events = _events(
        client.post("/api/agents/stream", headers=_auth(engine_core), json=_body("refuse noperm"))
    )
    assert events[-1]["type"] == "done"
    error = next(e for e in events if e["type"] == "error")
    assert "refused" in error["text"]

    missing = client.post(
        "/api/agents/stream",
        headers=_auth(engine_core),
        json=_body("hi", provider="harness:nope"),
    )
    assert missing.status_code == 400 and "unknown engine" in missing.json()["error"]


async def test_engines_off_by_default(core, work_model: Path):
    core.start_audit()
    await core.open_model(work_model)
    core.enable_chat()
    client = _client(core)
    providers = client.get("/api/chat/providers", headers=_auth(core)).json()["providers"]
    assert not [p for p in providers if p["family"] == "harness"]
    refused = client.post("/api/agents/stream", headers=_auth(core), json=_body("hi"))
    assert refused.status_code == 400
    assert refused.json()["error"] == "agent engines are off"
    assert "engines enable" in refused.json()["hint"]


async def test_interrupt_stops_the_engine_and_keeps_the_thread(engine_core):
    outcome: dict = {}
    with _client(engine_core) as client:

        def ask() -> None:
            try:
                outcome["response"] = client.post(
                    "/api/agents/stream", headers=_auth(engine_core), json=_body("sleep noperm")
                )
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        state = engine_core.agent_panel
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            state is not None and state.active_streams and getattr(state, "harness", None)
        ):
            time.sleep(0.02)
            state = getattr(engine_core, "agent_panel", None)
        while time.monotonic() < deadline and not any(
            s.busy for s in state.harness.sessions.values()
        ):
            time.sleep(0.02)
        thread_id = next(iter(state.active_streams))
        stopped = client.post(
            "/api/agents/interrupt", headers=_auth(engine_core), json={"thread_id": thread_id}
        )
        assert stopped.status_code == 200 and stopped.json()["cancelled_runs"] == 1
        worker.join(timeout=30)
        assert not worker.is_alive()
        [session] = state.harness.sessions.values()
        assert session.alive and not session.busy

        # the next turn on the thread carries the stop note and runs normally
        events = _stream_with_approval(
            engine_core, client, _body("what happened", thread_id=thread_id), True
        )
        answer = "".join(e["text"] for e in events if e["type"] == "content")
        assert "echo: [The user stopped this run" in answer


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_engine_calls_the_console_tools_through_the_injected_bridge(
    home: Path, work_model: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """A real loopback server: the fake engine really calls get_session_status."""
    _write_settings(home, [_engine_row(env={"IFC_CONSOLE_FAKE_ACP_MCP": "1"})])
    monkeypatch.chdir(tmp_path)
    from ifc_console.agents.devkit.serve import start
    from ifc_console.app import AppCore
    from ifc_console.settings import SettingsStore

    port = _free_port()
    store = SettingsStore(
        home=home, project_dir=tmp_path, env={}, flag_overrides={"server.port": port}
    )
    store.ensure_dirs()
    core = AppCore(store, port=port, transport="http", chat=True)
    core.start_audit()
    await core.open_model(work_model)
    server = start(core)
    try:
        import urllib.request

        def post(path: str, body: dict) -> tuple[int, str]:
            request = urllib.request.Request(
                f"{server.base_url}{path}",
                data=json.dumps(body).encode("utf-8"),
                headers={**_auth(core), "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                return response.status, response.read().decode("utf-8")

        outcome: dict = {}

        def ask() -> None:
            try:
                outcome["result"] = post("/api/agents/stream", _body("status through the bridge"))
            except BaseException as exc:
                outcome["error"] = exc

        worker = threading.Thread(target=ask, daemon=True)
        worker.start()
        request_id = _wait_for_approval(core, timeout=30)
        status, _ = post("/api/agents/approve", {"request_id": request_id, "approved": True})
        assert status == 200
        worker.join(timeout=120)
        assert not worker.is_alive()
        if "error" in outcome:
            raise outcome["error"]
        status, text = outcome["result"]
        assert status == 200
        events = [json.loads(line[6:]) for line in text.split("\n\n") if line.startswith("data: ")]
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["ok"] is True, result
        session_state = result["output"]["data"]
        assert session_state["model"]["path"].endswith("work.ifc") or "model" in session_state
        [session] = core.agent_panel.harness.sessions.values()
        log = (session.cwd / "engine.stderr.log").read_text()
        assert "mcp call failed" not in log
    finally:
        server.stop()


async def test_workflow_agent_step_runs_on_the_engine(engine_core):
    """A YAML step naming the engine runs there, approvals answered by autonomy."""
    from ifc_console.agents.workflow_runner import WorkflowRunner
    from ifc_console.agents.workflows import WorkflowSpec

    spec = WorkflowSpec.model_validate(
        {
            "version": "1",
            "name": "engine-check",
            "title": "Engine check",
            "steps": [
                {
                    "kind": "agent",
                    "id": "ask",
                    "engine": ENGINE_NAME,
                    "preset": "review",
                    "prompt": "Report the session status.",
                }
            ],
        }
    )
    assert spec.steps[0].engine == ENGINE_NAME
    runner = WorkflowRunner(engine_core, model=None, auto_approve=True)
    events = [event async for event in runner.stream(spec)]
    kinds = [event["type"] for event in events]
    assert "agent_started" in kinds and "agent_finished" in kinds
    finished = next(e for e in events if e["type"] == "agent_finished")
    assert finished["text"].startswith("checking the session done. echo:")
    assert finished["usage"] == {"in": 2, "out": 1}
    decided = next(e for e in events if e["type"] == "approval_decided")
    assert decided["decided_by"] == "session-autonomy"
    completed = events[-1]
    assert completed["type"] == "workflow_completed" and completed["state"] == "succeeded"
    # the step's engine is released with the step, not left for the idle reaper
    state = engine_core.agent_panel
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and state.harness.sessions:
        await asyncio.sleep(0.05)
    assert not state.harness.sessions


async def test_workflow_without_an_engine_still_needs_a_model(engine_core):
    from ifc_console.agents.workflow_runner import WorkflowRunner
    from ifc_console.agents.workflows import WorkflowSpec

    spec = WorkflowSpec.model_validate(
        {
            "version": "1",
            "name": "needs-model",
            "title": "Needs a model",
            "steps": [{"kind": "agent", "id": "ask", "preset": "review", "prompt": "hi"}],
        }
    )
    events = [e async for e in WorkflowRunner(engine_core, model=None).stream(spec)]
    assert events[-1]["state"] == "failed"
    step = next(e for e in events if e["type"] == "step_finished")
    assert "needs a language model" in str(step)
