"""Reliability contracts for the standalone SDK agent chat example."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from ifc_console.mcp.server import TokenAuthMiddleware

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "sdk" / "agent_chat"
STATIC = EXAMPLE / "static"


@pytest.fixture(scope="module")
def example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("sdk_agent_chat_example", EXAMPLE / "app.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Event:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def model_dump(self, **_kwargs) -> dict[str, object]:
        return self.payload


class _Agent:
    name = "test reviewer"
    model = SimpleNamespace(provider_id="test", model_id="test-model")
    tools = {"inspect": object()}

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def stream(self, message: str, *, thread_id: str):
        self.calls.append((message, thread_id))
        yield _Event({"type": "run_started", "run_id": "run-1"})
        yield _Event({"type": "run_completed", "run_id": "run-1"})


class _Workspace:
    async def status(self) -> dict[str, object]:
        return {"model": {"name": "fixture.ifc"}, "viewer": {"enabled": False}}


class _Runtime:
    mode = "ask"
    workspace = _Workspace()


@pytest.fixture
def reference_surface(example: ModuleType):
    agent = _Agent()
    approvals = example.BrowserApprovalHandler()
    routes = example.build_reference_routes(
        agent=agent,
        runtime=_Runtime(),
        approvals=approvals,
    )
    app = TokenAuthMiddleware(Starlette(routes=routes), "test-token")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, agent


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer test-token"}


def test_reference_shell_is_public_but_sdk_apis_require_auth(reference_surface) -> None:
    client, _agent = reference_surface

    shell = client.get("/sdk-chat")
    assert shell.status_code == 200
    assert "default-src 'self'" in shell.headers["content-security-policy"]
    assert client.get("/api/sdk/status").status_code == 401
    assert client.get("/api/sdk/status", headers=_auth()).status_code == 200
    assert client.post("/api/sdk/approvals/unknown", json={"approved": True}).status_code == 401


def test_authorized_stream_parses_json_and_emits_sse(reference_surface) -> None:
    client, agent = reference_surface
    response = client.post(
        "/api/sdk/chat/stream",
        headers=_auth(),
        json={"message": "Review the model", "thread_id": "thread-1"},
    )

    assert response.status_code == 200
    assert '"type": "run_completed"' in response.text
    assert '"type": "stream_closed"' in response.text
    assert agent.calls == [("Review the model", "thread-1")]


def test_reference_main_treats_keyboard_interrupt_as_clean_shutdown(
    example: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        example.argparse.ArgumentParser,
        "parse_args",
        lambda _parser: SimpleNamespace(),
    )

    def interrupt(coroutine) -> None:
        coroutine.close()
        raise KeyboardInterrupt

    monkeypatch.setattr(example.asyncio, "run", interrupt)

    assert example.main() == 0


def test_chunked_route_body_over_the_local_limit_never_starts_an_agent(
    reference_surface,
) -> None:
    client, agent = reference_surface

    def chunks():
        yield b'{"message":"'
        yield b"x" * (64 * 1024)
        yield b'","thread_id":"thread-1"}'

    response = client.post(
        "/api/sdk/chat/stream",
        headers={**_auth(), "Content-Type": "application/json"},
        content=chunks(),
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid or oversized JSON body"
    assert agent.calls == []


class _ChunkedRequest:
    headers: dict[str, str] = {}

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.consumed = 0

    async def stream(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


async def test_json_limit_stops_chunk_consumption_before_parsing(
    example: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _ChunkedRequest([b"123", b"456", b'{"late":true}'])

    def unexpected_parse(_value):
        raise AssertionError("oversized JSON reached the parser")

    monkeypatch.setattr(example.json, "loads", unexpected_parse)
    assert await example._json(request, max_bytes=5) is None
    assert request.consumed == 2


@pytest.fixture(scope="module")
def script() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def styles() -> str:
    return (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html() -> str:
    return (STATIC / "index.html").read_text(encoding="utf-8")


def test_each_stream_has_one_run_owned_controller_and_event_state(script: str) -> None:
    assert "activeRun: null" in script
    assert "return state.activeRun === runState" in script
    assert "if (!clean || state.activeRun) return;" in script
    assert "state.activeRun = runState;" in script
    assert "signal: runState.controller.signal" in script
    assert "await readSse(response, runState)" in script

    events = script.split("function handleEvent(event, runState)", 1)[1].split(
        "async function readSse", 1
    )[0]
    assert "if (!ownsRun(runState)) return;" in events
    assert "runState.assistant" in events
    assert "runState.ledger" in events
    assert "state.controller" not in script
    assert "state.running" not in script


def test_approval_failures_restore_controls_and_offer_a_next_step(script: str, html: str) -> None:
    approval = script.split("async function resolveApproval", 1)[1].split(
        "function showApproval", 1
    )[0]
    assert "try {" in approval and "catch (error)" in approval
    assert "response.json().catch" in approval
    assert "button.disabled = false" in approval
    assert "if (!ownsRun(runState)) return;" in approval
    assert "Retry the decision or stop the run." in approval
    assert re.search(
        r'data-role="run-status"[^>]*role="status"[^>]*aria-live="polite"', html
    )


def _token(styles: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", styles)
    assert match is not None, f"missing CSS token {name}"
    return match.group(1)


def _luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    light, dark = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (light + 0.05) / (dark + 0.05)


def test_text_tokens_meet_contrast_on_their_light_surfaces(styles: str) -> None:
    ledger = _token(styles, "--ledger")
    pairs = {
        "muted ledger text": (_token(styles, "--muted"), ledger),
        "survey state text": (_token(styles, "--survey"), ledger),
        "moss state text": (_token(styles, "--moss"), ledger),
        "survey action": (_token(styles, "--white"), _token(styles, "--survey")),
        "moss action": (_token(styles, "--white"), _token(styles, "--moss")),
    }
    for label, (foreground, background) in pairs.items():
        assert _contrast(foreground, background) >= 4.5, label
