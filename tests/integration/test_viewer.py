"""Viewer integration: HTTP routes, WebSocket handshake, and the three tools
running against a fake browser tab."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from tests.unit.test_viewer_hub import TINY_PNG, FakeWS

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------- helpers
def _http_client(core) -> TestClient:
    from ifc_console.mcp.server import build_http_app, build_mcp

    app = build_http_app(core, build_mcp(core))
    return TestClient(app)


def _auth(core) -> dict:
    return {"Authorization": f"Bearer {core.token}"}


def _attach_fake_tab(core, **kwargs) -> FakeWS:
    ws = FakeWS(hub=core.viewer_hub, **kwargs)
    ws.client = core.viewer_hub.register(ws)
    return ws


@pytest.fixture
async def viewer_core(core, work_model: Path):
    core.enable_viewer()
    await core.open_model(work_model)
    return core


# ------------------------------------------------------------------ HTTP routes
async def test_routes_require_token(viewer_core):
    client = _http_client(viewer_core)
    assert client.get("/viewer").status_code == 401
    assert client.get("/api/model.ifc").status_code == 401
    assert client.get("/api/status").status_code == 401
    # the query form used by the viewer URL must also work
    assert client.get(f"/viewer?t={viewer_core.token}").status_code == 200


async def test_static_assets_are_public_and_complete(viewer_core):
    client = _http_client(viewer_core)
    for asset in ("app.js", "app.css", "vendor/web-ifc-api.js", "vendor/web-ifc.wasm",
                  "vendor/three.module.min.js", "vendor/three.core.min.js",
                  "vendor/OrbitControls.js"):
        response = client.get(f"/viewer/static/{asset}")
        assert response.status_code == 200, asset


async def test_viewer_disabled_hides_surface(core, work_model: Path):
    await core.open_model(work_model)
    assert not core.viewer.enabled
    client = _http_client(core)
    assert client.get("/viewer", headers=_auth(core)).status_code == 404
    assert client.get("/api/model.ifc", headers=_auth(core)).status_code == 404
    body = client.get("/viewer", headers=_auth(core)).json()
    assert body["error"] == "viewer_disabled"


async def test_shell_served_with_csp(viewer_core):
    client = _http_client(viewer_core)
    response = client.get("/viewer", headers=_auth(viewer_core))
    assert response.status_code == 200
    assert "ifc-console viewer" in response.text
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "wasm-unsafe-eval" in csp


async def test_model_ifc_etag_and_304(viewer_core):
    client = _http_client(viewer_core)
    first = client.get("/api/model.ifc", headers=_auth(viewer_core))
    assert first.status_code == 200
    assert first.text.startswith("ISO-10303-21")
    etag = first.headers["etag"]
    assert etag == f"{viewer_core.session.fingerprint}-{viewer_core.session.revision}"

    again = client.get(
        "/api/model.ifc", headers={**_auth(viewer_core), "If-None-Match": etag}
    )
    assert again.status_code == 304

    # a mutation must change the ETag so tabs refetch
    viewer_core.session.mark_dirty()
    third = client.get(
        "/api/model.ifc", headers={**_auth(viewer_core), "If-None-Match": etag}
    )
    assert third.status_code == 200
    assert third.headers["etag"] != etag


async def test_model_ifc_serves_unsaved_edits(viewer_core):
    """The viewer must show the live in-memory model, not the on-disk file."""
    client = _http_client(viewer_core)

    def rename() -> None:
        viewer_core.session.ifc.by_type("IfcProject")[0].Name = "RenamedLive"

    await viewer_core.session.run(rename)
    viewer_core.session.mark_dirty()
    response = client.get("/api/model.ifc", headers=_auth(viewer_core))
    assert "RenamedLive" in response.text


async def test_model_ifc_no_model_404(core):
    core.enable_viewer()
    client = _http_client(core)
    response = client.get("/api/model.ifc", headers=_auth(core))
    assert response.status_code == 404
    assert response.json()["error"] == "NO_MODEL_LOADED"


async def test_model_ifc_size_guard_413(viewer_core, monkeypatch):
    monkeypatch.setattr(viewer_core.settings.viewer, "max_model_mb", 1)
    viewer_core.session.size_bytes = 2 * 1_048_576  # pretend the model is 2 MB
    client = _http_client(viewer_core)
    response = client.get("/api/model.ifc", headers=_auth(viewer_core))
    assert response.status_code == 413
    assert response.json()["error"] == "MODEL_TOO_LARGE"


async def test_element_endpoint(viewer_core):
    client = _http_client(viewer_core)

    def first_wall_guid() -> str:
        return viewer_core.session.ifc.by_type("IfcWall")[0].GlobalId

    guid = await viewer_core.session.run(first_wall_guid)
    response = client.get(f"/api/elements/{guid}", headers=_auth(viewer_core))
    assert response.status_code == 200
    detail = response.json()
    assert detail["global_id"] == guid
    assert detail["class"] == "IfcWall"
    assert "psets" in detail and "container" in detail

    missing = client.get("/api/elements/notaguid1234", headers=_auth(viewer_core))
    assert missing.status_code == 404


async def test_api_status_shape(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/status", headers=_auth(viewer_core)).json()
    assert payload["model"] == "work.ifc"
    assert payload["schema"] == "IFC4"
    assert payload["mode"] == "ask"
    assert payload["viewer"]["enabled"] is True
    assert payload["etag"] == viewer_core.viewer_hub.model_etag()


# ------------------------------------------------------------------- WebSocket
async def _wait_for(condition, timeout: float = 2.0) -> None:
    """The TestClient app runs in a portal thread; poll for its side effects."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def test_ws_handshake_status_and_selection(viewer_core):
    client = _http_client(viewer_core)
    with client.websocket_connect(f"/ws?t={viewer_core.token}") as ws:
        ws.send_text(json.dumps({"type": "hello", "token": viewer_core.token}))
        status = ws.receive_json()
        assert status["type"] == "status"
        assert status["model"] == "work.ifc"
        ws.send_text(json.dumps({"type": "selection", "guids": ["g-1"]}))
        await _wait_for(lambda: viewer_core.viewer_hub.selection == ["g-1"])
        assert viewer_core.viewer.connected == 1
    await _wait_for(lambda: viewer_core.viewer.connected == 0)  # unregistered on close


async def test_ws_rejects_without_token(viewer_core):
    from starlette.websockets import WebSocketDisconnect

    client = _http_client(viewer_core)
    with (
        pytest.raises((WebSocketDisconnect, Exception)),  # denied before accept
        client.websocket_connect("/ws") as ws,
    ):
        ws.receive_json()


async def test_ws_rejects_bad_hello_token(viewer_core):
    from starlette.websockets import WebSocketDisconnect

    client = _http_client(viewer_core)
    with client.websocket_connect(f"/ws?t={viewer_core.token}") as ws:
        ws.send_text(json.dumps({"type": "hello", "token": "wrong"}))
        try:
            closed = ws.receive()
            assert closed["type"] == "websocket.close"
            assert closed.get("code") == 4401
        except WebSocketDisconnect as exc:
            assert exc.code == 4401
    await _wait_for(lambda: viewer_core.viewer.connected == 0)


# ------------------------------------------------------------------- MCP tools
async def test_get_viewer_selection_roundtrip(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)

    def wall_guids() -> list[str]:
        return [w.GlobalId for w in h.core.session.ifc.by_type("IfcWall")]

    guids = await h.core.session.run(wall_guids)
    await h.core.viewer_hub.handle_frame(
        ws.client, {"type": "selection", "guids": [guids[0], "bogus-guid"]}
    )
    out = await h.call("get_viewer_selection")
    assert out["ok"] is True
    assert out["data"]["guids"] == [guids[0], "bogus-guid"]
    assert out["data"]["elements"][0]["class"] == "IfcWall"
    assert out["data"]["missing"] == ["bogus-guid"]
    assert out["data"]["selected_at"] is not None


async def test_get_viewer_selection_empty(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    _attach_fake_tab(h.core)
    out = await h.call("get_viewer_selection")
    assert out["ok"] is True
    assert out["data"]["guids"] == []
    assert "note" in out["data"]


async def test_highlight_elements_roundtrip(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)

    def wall_guids() -> list[str]:
        return [w.GlobalId for w in h.core.session.ifc.by_type("IfcWall")]

    guids = await h.core.session.run(wall_guids)
    out = await h.call(
        "highlight_elements",
        global_ids=[guids[0], guids[1], "missing-one"],
        color="#00ff00",
        isolate=True,
    )
    assert out["ok"] is True
    assert out["data"]["highlighted"] == 2
    assert out["data"]["missing"] == ["missing-one"]
    frame = ws.frames("highlight")[0]
    assert frame["guids"] == [guids[0], guids[1]]
    assert frame["color"] == "#00ff00"
    assert frame["isolate"] is True

    cleared = await h.call("highlight_elements", clear=True)
    assert cleared["ok"] is True
    assert cleared["data"]["cleared"] is True
    assert ws.frames("highlight")[-1]["clear"] is True


async def test_highlight_works_in_ask_mode(harness_factory, work_model: Path):
    """VIEW-class tools are visual only and must not be blocked in ask mode."""
    h = await harness_factory(model=work_model)  # ask-mode default
    h.core.enable_viewer()
    _attach_fake_tab(h.core)
    out = await h.call("highlight_elements", clear=True)
    assert out["ok"] is True


async def test_screenshot_tool_returns_image(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    payload = {"data_b64": base64.b64encode(TINY_PNG).decode(), "width": 1, "height": 1}
    _attach_fake_tab(h.core, respond_shots=payload)

    result = await h.session.call_tool(
        "get_viewer_screenshot", {"view": "iso", "format": "png"}
    )
    kinds = [getattr(c, "type", None) for c in result.content]
    assert "image" in kinds
    image = next(c for c in result.content if getattr(c, "type", None) == "image")
    assert base64.b64decode(image.data) == TINY_PNG
    assert image.mimeType == "image/png"
    note = next(c.text for c in result.content if getattr(c, "type", None) == "text")
    assert "1x1" in note


async def test_screenshot_timeout_surfaces_hint(
    harness_factory, work_model: Path, monkeypatch
):
    monkeypatch.setattr("ifc_console.viewer.hub._SCREENSHOT_TIMEOUT", 0.05)
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    _attach_fake_tab(h.core)  # never answers
    out = await h.call("get_viewer_screenshot")
    assert out["ok"] is False
    assert out["error"]["code"] == "VIEWER_TIMEOUT"


async def test_viewer_tools_not_connected_after_tab_closes(
    harness_factory, work_model: Path
):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    h.core.viewer_hub.unregister(ws.client)
    out = await h.call("get_viewer_selection")
    assert out["ok"] is False
    assert out["error"]["code"] == "VIEWER_NOT_CONNECTED"


async def test_mutation_notifies_viewer_tabs(harness_factory, work_model: Path):
    """execute_ifc_code mutations must push model_updated to connected tabs."""
    from ifc_console.policy.modes import Mode

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    out = await h.call(
        "execute_ifc_code",
        code="ifc.by_type('IfcProject')[0].Name = 'Renamed'",
        description="rename project",
    )
    assert out["ok"] is True and out["data"]["mutated"] is True
    await asyncio.sleep(0)  # scheduled broadcast task
    frames = ws.frames("model_updated")
    assert frames and frames[-1]["reason"] == "edited"
