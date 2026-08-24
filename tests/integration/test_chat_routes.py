"""Chat over HTTP: the same token gate as the viewer, and a scripted stream."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.asyncio


def _client(core) -> TestClient:
    from ifc_console.mcp.server import build_http_app, build_mcp

    app = build_http_app(core, build_mcp(core))
    return TestClient(app, base_url="http://127.0.0.1")


def _auth(core) -> dict:
    return {"Authorization": f"Bearer {core.token}"}


@pytest.fixture
async def chat_core(core, work_model: Path):
    core.start_audit()
    await core.open_model(work_model)
    core.enable_chat()
    core.chat.model = "test-model"
    core.chat.keys["openai"] = "sk-test"
    return core


async def test_routes_are_404_until_chat_is_on(core, work_model: Path):
    await core.open_model(work_model)
    client = _client(core)
    assert client.get("/chat").status_code == 404
    assert client.get("/api/chat/providers", headers=_auth(core)).status_code == 404


async def test_the_api_needs_the_token_but_the_shell_does_not(chat_core):
    client = _client(chat_core)
    assert client.get("/api/chat/providers").status_code == 401
    assert client.get("/api/chat/providers", headers=_auth(chat_core)).status_code == 200
    # the page carries the token in its fragment, like the viewer shell
    assert client.get("/chat").status_code == 200


async def test_status_adds_the_resolved_console_theme_for_standalone_chat(chat_core):
    client = _client(chat_core)
    expected = {
        "server",
        "meta",
        "model",
        "models",
        "schema",
        "mode",
        "dirty",
        "fingerprint",
        "etag",
        "selection",
        "viewer",
        "chat",
        "theme",
    }

    chat_core.set_ui_theme("light")
    light = client.get("/api/status", headers=_auth(chat_core)).json()
    assert expected <= light.keys()
    assert light["theme"] == "light"

    chat_core.set_ui_theme("auto")
    auto = client.get("/api/status", headers=_auth(chat_core)).json()
    assert auto["theme"] == "dark"


async def test_a_cross_site_origin_is_refused_even_with_the_token(chat_core):
    client = _client(chat_core)
    headers = {**_auth(chat_core), "Origin": "https://evil.example"}
    assert client.get("/api/chat/providers", headers=headers).status_code == 403


async def test_providers_list_never_leaks_a_key(chat_core, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-12345")
    client = _client(chat_core)
    body = client.get("/api/chat/providers", headers=_auth(chat_core)).text
    assert "sk-secret-value-12345" not in body
    assert "sk-test" not in body
    payload = json.loads(body)
    openai = next(p for p in payload["providers"] if p["id"] == "openai")
    assert openai["key_from_env"] == "OPENAI_API_KEY"
    assert openai["has_key"] is True


async def test_the_panel_can_remember_a_choice_for_this_run(chat_core):
    client = _client(chat_core)
    response = client.post(
        "/api/chat/select",
        headers=_auth(chat_core),
        json={"provider": "anthropic", "model": "claude-sonnet-5", "api_key": "sk-ant"},
    )
    assert response.status_code == 200
    assert chat_core.chat.provider == "anthropic"
    assert chat_core.chat.model == "claude-sonnet-5"
    assert chat_core.chat.keys["anthropic"] == "sk-ant"


async def test_keys_can_be_saved_and_deleted_without_ever_being_returned(
    chat_core, monkeypatch
):
    saved: dict[str, str] = {}
    monkeypatch.setattr("ifc_console.credentials.keyring_available", lambda: True)
    monkeypatch.setattr("ifc_console.credentials.stored_providers", lambda _home: list(saved))
    monkeypatch.setattr(
        "ifc_console.credentials.set_api_key",
        lambda _home, provider, key: saved.__setitem__(provider, key),
    )
    monkeypatch.setattr(
        "ifc_console.credentials.delete_api_key",
        lambda _home, provider: saved.pop(provider, None) is not None,
    )
    client = _client(chat_core)
    headers = _auth(chat_core)

    stored = client.post(
        "/api/chat/credentials",
        headers=headers,
        json={"provider": "anthropic", "action": "store", "api_key": "secret-value"},
    )
    assert stored.status_code == 200
    assert "secret-value" not in stored.text
    assert saved == {"anthropic": "secret-value"}
    listing = client.get("/api/chat/credentials", headers=headers).json()
    assert listing["providers"] == ["anthropic"]
    assert "secret-value" not in json.dumps(listing)

    deleted = client.post(
        "/api/chat/credentials",
        headers=headers,
        json={"provider": "anthropic", "action": "delete"},
    )
    assert deleted.status_code == 200
    assert saved == {}


async def test_local_only_blocks_model_discovery_before_network(chat_core, monkeypatch):
    chat_core.store.settings.chat.local_only = True

    def should_not_run(*_args, **_kwargs):
        raise AssertionError("network helper was called")

    monkeypatch.setattr("ifc_console.chat.routes.list_models", should_not_run)
    client = _client(chat_core)
    response = client.post(
        "/api/chat/models",
        headers=_auth(chat_core),
        json={"provider": "openai", "base_url": "https://api.openai.com/v1"},
    )
    assert response.status_code == 400
    assert "local_only" in response.json()["error"]


async def test_invalid_provider_url_is_not_remembered(chat_core):
    chat_core.chat.base_url = "http://localhost:8000/v1"
    client = _client(chat_core)
    response = client.post(
        "/api/chat/select",
        headers=_auth(chat_core),
        json={"base_url": "file:///tmp/provider"},
    )
    assert response.status_code == 400
    assert chat_core.chat.base_url == "http://localhost:8000/v1"


async def test_stream_returns_sse_events(chat_core, monkeypatch):
    def fake_stream(provider, **kwargs):
        yield {"type": "content", "text": "there are three walls"}
        yield {"type": "usage", "in": 10, "out": 4}

    monkeypatch.setattr("ifc_console.chat.agent.stream", fake_stream)
    client = _client(chat_core)
    response = client.post(
        "/api/chat/stream",
        headers=_auth(chat_core),
        json={"turns": [{"role": "user", "text": "how many walls?"}]},
    )
    assert response.status_code == 200
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    kinds = [event["type"] for event in events]
    assert kinds[0] == "content" and kinds[-1] == "done"
    assert events[0]["text"] == "there are three walls"


async def test_a_provider_failure_arrives_as_an_event_not_a_500(chat_core, monkeypatch):
    from ifc_console.chat.providers import ProviderError

    def failing(provider, **kwargs):
        raise ProviderError("HTTP 401: invalid api key")
        yield  # pragma: no cover

    monkeypatch.setattr("ifc_console.chat.agent.stream", failing)
    client = _client(chat_core)
    response = client.post(
        "/api/chat/stream",
        headers=_auth(chat_core),
        json={"turns": [{"role": "user", "text": "hi"}]},
    )
    assert response.status_code == 200
    assert "invalid api key" in response.text
    assert '"type": "error"' in response.text


async def test_an_empty_conversation_is_refused(chat_core):
    client = _client(chat_core)
    response = client.post("/api/chat/stream", headers=_auth(chat_core), json={"turns": []})
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/api/chat/models", "/api/chat/select", "/api/chat/stream"])
async def test_chat_routes_reject_invalid_json_objects(chat_core, path: str):
    client = _client(chat_core)
    malformed = client.post(
        path,
        headers={**_auth(chat_core), "Content-Type": "application/json"},
        content="{not-json",
    )
    assert malformed.status_code == 400

    array = client.post(path, headers=_auth(chat_core), json=[])
    assert array.status_code == 400


async def test_chat_stream_rejects_malformed_and_oversized_fields(chat_core):
    client = _client(chat_core)
    cases = [
        {"turns": "not-a-list"},
        {"turns": [{"role": "tool", "text": "x"}]},
        {"turns": [{"role": "user", "text": "x" * 100_001}]},
        {"turns": [{"role": "user", "text": "x"}], "tools": "false"},
        {"turns": [{"role": "user", "text": "x"}], "temperature": 99},
        {"turns": [{"role": "user", "text": "x"}], "max_tokens": 1.5},
    ]
    for payload in cases:
        response = client.post("/api/chat/stream", headers=_auth(chat_core), json=payload)
        assert response.status_code == 400


async def test_unexpected_chat_failure_does_not_echo_exception_text(chat_core, monkeypatch):
    def failing(provider, **kwargs):
        raise RuntimeError("internal-secret-detail")
        yield  # pragma: no cover

    monkeypatch.setattr("ifc_console.chat.agent.stream", failing)
    client = _client(chat_core)
    response = client.post(
        "/api/chat/stream",
        headers=_auth(chat_core),
        json={"turns": [{"role": "user", "text": "hi"}]},
    )
    assert response.status_code == 200
    assert "internal provider error" in response.text
    assert "internal-secret-detail" not in response.text


async def test_the_chat_page_and_assets_ship_with_the_viewer_extra(chat_core):
    from ifc_console.viewer import assets

    directory = assets.require_static_dir()
    for name in ("chat.html", "chat.js", "chat.css"):
        assert (directory / name).is_file(), f"{name} missing from the viewer bundle"
    client = _client(chat_core)
    assert client.get("/viewer/static/chat.js").status_code == 200
