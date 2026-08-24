"""The agent panel over HTTP: listing, streaming, threads, and uploads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ifc_console.agents.packs import AgentPackInfo
from ifc_console.testing import ScriptedAgentModel, text_round, tool_call_round

pytestmark = pytest.mark.asyncio


class ScriptedPack:
    """A pack that ignores the provider model and plays a script instead."""

    def __init__(self, name="scripted", features=(), rounds=None):
        self.info = AgentPackInfo(
            name=name,
            title="Scripted",
            description="offline test pack",
            features=tuple(features),
            starters=("say hello",),
        )
        self.rounds = rounds or [text_round("hello from the pack")]
        self.built = 0

    async def build(self, runtime, *, model, viewer: bool = False):
        from ifc_console import Agent

        self.built += 1
        tools = await runtime.tools("get_ifc_project_info", "query_elements")
        return Agent(
            name=self.info.name,
            model=ScriptedAgentModel(list(self.rounds)),
            tools=tools,
            instructions="test",
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


async def test_upload_ingests_into_the_project_corpus(panel_core):
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
    assert payload["files"][0]["indexed"] is True
    assert ".ifc-console/agents/references/" in payload["files"][0]["path"]
    hits = panel_core.project_knowledge.search("wall thickness layers")
    assert hits and hits[0]["meta"]["path"].endswith("manual.md")


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
