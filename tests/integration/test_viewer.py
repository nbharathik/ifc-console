"""Viewer integration: HTTP routes, WebSocket handshake, and the three tools
running against a fake browser tab."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from ifc_console.viewer.assets import require_static_dir as _static_dir
from tests.unit.test_viewer_hub import TINY_PNG, FakeWS

pytestmark = pytest.mark.asyncio

# websocket_connect ignores base_url; an absolute URL keeps the Host loopback.
WS_URL = "ws://127.0.0.1/ws"


# --------------------------------------------------------------------- helpers
def _http_client(core) -> TestClient:
    from ifc_console.mcp.server import build_http_app, build_mcp

    app = build_http_app(core, build_mcp(core))
    # base_url sets the Host header; the middleware only answers loopback.
    return TestClient(app, base_url="http://127.0.0.1")


def _auth(core) -> dict:
    return {"Authorization": f"Bearer {core.token}"}


def _attach_fake_tab(core, **kwargs) -> FakeWS:
    ws = FakeWS(hub=core.viewer_hub, **kwargs)
    ws.client = core.viewer_hub.register(ws)
    ws.client.view_model_id = core.models.active_id
    return ws


@pytest.fixture
async def viewer_core(core, work_model: Path):
    core.enable_viewer()
    await core.open_model(work_model)
    return core


# ------------------------------------------------------------------ HTTP routes
async def test_status_reports_the_console_memory(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/status", headers=_auth(viewer_core)).json()
    memory = payload["memory"]
    assert set(memory) == {"rss_bytes", "peak_rss_bytes", "total_bytes", "available_bytes"}
    for value in memory.values():
        assert value is None or (isinstance(value, int) and value >= 0)


async def test_routes_require_token(viewer_core):
    client = _http_client(viewer_core)
    assert client.get("/api/model.ifc").status_code == 401
    assert client.get("/api/status").status_code == 401
    # the retired ?t= query form must not authorize anything
    assert client.get(f"/api/status?t={viewer_core.token}").status_code == 401
    # the shell is public (nothing session-specific); the SPA holds the token
    assert client.get("/viewer").status_code == 200


async def test_cross_origin_rejected_even_with_token(viewer_core):
    """DNS rebinding / cross-site guard: a valid token never overrides Origin."""
    client = _http_client(viewer_core)
    headers = {**_auth(viewer_core), "Origin": "https://evil.example"}
    assert client.get("/api/status", headers=headers).status_code == 403
    assert client.get("/viewer", headers={"Origin": "https://evil.example"}).status_code == 403


async def test_rebound_host_rejected_even_with_token(viewer_core):
    client = _http_client(viewer_core)
    headers = {**_auth(viewer_core), "Host": "evil.example"}
    assert client.get("/api/status", headers=headers).status_code == 403


async def test_loopback_origin_and_host_accepted(viewer_core):
    client = _http_client(viewer_core)
    for origin in ("http://127.0.0.1:8383", "http://localhost:8383"):
        response = client.get(
            "/api/status", headers={**_auth(viewer_core), "Origin": origin}
        )
        assert response.status_code == 200, origin


async def test_static_assets_are_public_and_complete(viewer_core):
    client = _http_client(viewer_core)
    for asset in ("app.js", "app.css", "parser.js", "worker.js",
                  "vendor/web-ifc-api.js", "vendor/web-ifc.wasm",
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
    assert etag == (
        f"{viewer_core.session.model_id}-{viewer_core.session.fingerprint}-"
        f"{viewer_core.session.revision}"
    )

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


async def test_model_ifc_streams_the_file_while_it_matches_the_model(viewer_core):
    """A clean .ifc is served straight from disk: no re-serialization."""
    client = _http_client(viewer_core)
    assert viewer_core.session.matches_disk()
    response = client.get("/api/model.ifc", headers=_auth(viewer_core))
    assert response.status_code == 200
    assert response.content == viewer_core.session.path.read_bytes()
    # nothing was serialized, so the byte cache stayed empty
    assert viewer_core.viewer_hub.cached_model_bytes(response.headers["etag"]) is None

    # once the model diverges from the file, the fast path must switch off
    viewer_core.session.mark_dirty()
    assert not viewer_core.session.matches_disk()


async def test_disk_match_detects_content_changes_with_preserved_stat(viewer_core):
    path = viewer_core.session.path
    original = path.read_bytes()
    stat = path.stat()
    changed = original.replace(b"Duplex", b"Dupley", 1)
    assert len(changed) == len(original)
    path.write_bytes(changed)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    assert path.stat().st_size == stat.st_size
    assert path.stat().st_mtime_ns == stat.st_mtime_ns
    assert not viewer_core.session.matches_disk()


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


async def test_model_ifc_serialized_size_guard_413(viewer_core, monkeypatch):
    monkeypatch.setattr(viewer_core.settings.viewer, "max_model_mb", 1)
    monkeypatch.setattr(viewer_core.session, "matches_disk", lambda: False)

    async def oversized(_job, timeout=None):
        return b"x" * (1_048_576 + 1)

    monkeypatch.setattr(viewer_core.session, "run", oversized)
    client = _http_client(viewer_core)

    response = client.get("/api/model.ifc", headers=_auth(viewer_core))

    assert response.status_code == 413
    assert response.json()["error"] == "MODEL_TOO_LARGE"
    etag = viewer_core.viewer_hub.model_etag()
    assert etag is not None
    assert viewer_core.viewer_hub.cached_model_bytes(etag) is None


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


async def test_element_endpoint_includes_materials_and_parts(viewer_core):
    client = _http_client(viewer_core)

    def first_wall_guid() -> str:
        return viewer_core.session.ifc.by_type("IfcWall")[0].GlobalId

    guid = await viewer_core.session.run(first_wall_guid)
    detail = client.get(f"/api/elements/{guid}", headers=_auth(viewer_core)).json()
    assert "materials" in detail
    assert isinstance(detail["decomposition"], list)


# ---------------------------------------------------------------------- search
async def test_search_matches_a_class_through_the_selector(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/search?q=IfcWall", headers=_auth(viewer_core)).json()
    assert payload["mode"] == "selector"
    assert payload["total"] == 3
    assert {row["class"] for row in payload["results"]} == {"IfcWall"}
    assert all("global_id" in row for row in payload["results"])


async def test_search_falls_back_to_substring_text(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/search?q=wall", headers=_auth(viewer_core)).json()
    assert payload["mode"] == "text"
    assert payload["total"] >= 3


async def test_search_ignores_too_short_a_term(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/search?q=a", headers=_auth(viewer_core)).json()
    assert payload["results"] == [] and payload["total"] == 0


async def test_search_reports_truncation_without_dropping_the_count(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/search?q=IfcWall&limit=1", headers=_auth(viewer_core)).json()
    assert payload["total"] == 3
    assert payload["truncated"] is True
    assert len(payload["results"]) == 1


async def test_search_survives_nonsense_selector_syntax(viewer_core):
    client = _http_client(viewer_core)
    response = client.get("/api/search?q=Ifc%3D%3D%3D", headers=_auth(viewer_core))
    assert response.status_code == 200
    assert response.json()["total"] == 0


async def test_search_requires_a_token(viewer_core):
    client = _http_client(viewer_core)
    assert client.get("/api/search?q=IfcWall").status_code == 401


async def test_search_404s_while_the_viewer_is_disabled(core, work_model: Path):
    await core.open_model(work_model)
    client = _http_client(core)
    assert client.get("/api/search?q=IfcWall", headers=_auth(core)).status_code == 404


async def test_api_status_shape(viewer_core):
    client = _http_client(viewer_core)
    payload = client.get("/api/status", headers=_auth(viewer_core)).json()
    assert payload["model"] == "work.ifc"
    assert payload["schema"] == "IFC4"
    assert payload["mode"] == "ask"
    assert payload["viewer"]["enabled"] is True
    assert payload["etag"] == viewer_core.viewer_hub.model_etag()
    assert payload["project_scope"] == viewer_core.viewer_hub.project_scope()


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
    with client.websocket_connect(WS_URL) as ws:
        ws.send_text(json.dumps({"type": "hello", "token": viewer_core.token}))
        status = ws.receive_json()
        assert status["type"] == "status"
        assert status["model"] == "work.ifc"
        # the tab cannot label a measurement without the file's own unit, and
        # reading it belongs to the model worker, so it follows the handshake
        units = ws.receive_json()
        assert units["type"] == "status"
        assert units["units"]["length_unit"]
        assert units["units"]["to_si_factor"] > 0
        ws.send_text(json.dumps({"type": "selection", "guids": ["g-1"]}))
        await _wait_for(lambda: viewer_core.viewer_hub.selection == ["g-1"])
        assert viewer_core.viewer.connected == 1
    await _wait_for(lambda: viewer_core.viewer.connected == 0)  # unregistered on close


def _expect_ws_close(ws, code: int) -> None:
    from starlette.websockets import WebSocketDisconnect

    try:
        closed = ws.receive()
        assert closed["type"] == "websocket.close"
        assert closed.get("code") == code
    except WebSocketDisconnect as exc:
        assert exc.code == code


async def test_ws_rejects_non_hello_first_frame(viewer_core):
    """Without a verified hello nothing is served, whatever arrives first."""
    client = _http_client(viewer_core)
    with client.websocket_connect(WS_URL) as ws:
        ws.send_text(json.dumps({"type": "selection", "guids": ["g-1"]}))
        _expect_ws_close(ws, 4401)
    assert viewer_core.viewer_hub.selection == []
    await _wait_for(lambda: viewer_core.viewer.connected == 0)


async def test_ws_rejects_bad_hello_token(viewer_core):
    client = _http_client(viewer_core)
    with client.websocket_connect(WS_URL) as ws:
        ws.send_text(json.dumps({"type": "hello", "token": "wrong"}))
        _expect_ws_close(ws, 4401)
    await _wait_for(lambda: viewer_core.viewer.connected == 0)


async def test_ws_rejects_cross_origin_upgrade(viewer_core):
    from starlette.websockets import WebSocketDisconnect

    client = _http_client(viewer_core)
    with (
        pytest.raises((WebSocketDisconnect, Exception)),  # denied before accept
        client.websocket_connect(
            "/ws", headers={"Origin": "https://evil.example"}
        ) as ws,
    ):
        ws.receive_json()


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


async def test_get_viewer_selection_reports_what_the_viewport_is_doing(
    harness_factory, work_model: Path
):
    """Without this the agent re-isolates what the user already isolated, and
    answers 'which walls are visible' from the file rather than the screen."""
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    await h.core.viewer_hub.handle_frame(
        ws.client,
        {
            "type": "viewer_state",
            "state": {
                "camera": {"projection": "orthographic"},
                "view": "top",
                "visibility": {"hidden": 412, "total": 14400},
            },
        },
    )
    out = await h.call("get_viewer_selection")
    assert out["ok"] is True
    assert "412 of 14400 elements hidden" in out["data"]["viewport"]


async def test_get_viewer_measurements_names_the_model_they_belong_to(
    harness_factory, work_model: Path
):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    await h.core.viewer_hub.handle_frame(
        ws.client,
        {
            "type": "measurements",
            "items": [{"from": [0.0, 0.0, 0.0], "to": [2.5, 0.0, 0.0], "distance": 2.5}],
        },
    )
    out = await h.call("get_viewer_measurements")
    assert out["ok"] is True
    assert out["data"]["model_id"] == h.core.models.active_id


async def test_get_viewer_selection_empty(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    _attach_fake_tab(h.core)
    out = await h.call("get_viewer_selection")
    assert out["ok"] is True
    assert out["data"]["guids"] == []
    assert "note" in out["data"]


async def test_get_viewer_selection_uses_the_tabs_model(
    harness_factory, work_model: Path, tmp_path: Path
):
    import shutil

    import ifcopenshell

    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    attached_path = tmp_path / "annex.ifc"
    shutil.copy2(work_model, attached_path)
    annex = ifcopenshell.open(str(attached_path))
    annex.by_type("IfcWall")[0].GlobalId = ifcopenshell.guid.new()
    annex.write(str(attached_path))
    model_id = await h.core.open_model(attached_path, attach=True, alias="annex")
    attached = h.core.models.require(model_id)
    guid = await attached.run(lambda: attached.ifc.by_type("IfcWall")[0].GlobalId)

    await h.core.viewer_hub.handle_frame(
        ws.client, {"type": "selection", "guids": [guid], "model_id": model_id}
    )
    out = await h.call("get_viewer_selection")

    assert out["data"]["model_id"] == model_id
    assert out["data"]["elements"][0]["global_id"] == guid
    assert out["data"]["missing"] == []
    assert out["meta"]["read_from"] == model_id


async def test_selection_based_viewer_commands_follow_the_selected_model(
    harness_factory, work_model: Path, tmp_path: Path
):
    import shutil

    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    active_tab = _attach_fake_tab(h.core)
    annex_path = tmp_path / "selected-annex.ifc"
    shutil.copy2(work_model, annex_path)
    model_id = await h.core.open_model(annex_path, attach=True, alias="selected-annex")
    annex_tab = _attach_fake_tab(h.core)
    annex_tab.client.view_model_id = model_id
    guid = await h.core.models.require(model_id).run(
        lambda: h.core.models.require(model_id).ifc.by_type("IfcWall")[0].GlobalId
    )
    await h.core.viewer_hub.handle_frame(
        annex_tab.client,
        {"type": "selection", "guids": [guid], "model_id": model_id},
    )

    pending = asyncio.create_task(h.call("control_viewer", action="hide"))
    for _ in range(20):
        if annex_tab.frames("command"):
            break
        await asyncio.sleep(0)
    command = annex_tab.frames("command")[0]
    assert not active_tab.frames("command")
    assert command["model_id"] == model_id
    await h.core.viewer_hub.handle_frame(
        annex_tab.client,
        {
            "type": "command_result",
            "id": command["id"],
            "ok": True,
            "result": {"hidden": 1},
        },
    )

    out = await pending
    assert out["ok"] is True
    assert out["data"]["model_id"] == model_id
    assert out["meta"]["read_from"] == model_id


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


async def test_apply_color_theme_paints_and_syncs_new_tabs(
    harness_factory, work_model: Path
):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)

    def wall_guids() -> list[str]:
        return [w.GlobalId for w in h.core.session.ifc.by_type("IfcWall")]

    guids = await h.core.session.run(wall_guids)
    out = await h.call(
        "apply_color_theme",
        title="Fire rating",
        groups=[
            {"label": "F30", "global_ids": [guids[0]]},
            {"label": "unrated", "global_ids": guids[1:] + ["bogus"], "color": "#999999"},
        ],
    )
    assert out["ok"] is True
    legend = out["data"]["legend"]
    assert [entry["label"] for entry in legend] == ["F30", "unrated"]
    assert legend[0]["color"].startswith("#")  # palette-assigned
    assert legend[1]["color"] == "#999999"  # explicit color respected
    assert out["data"]["painted"] == 3
    assert out["data"]["missing"] == ["bogus"]

    frame = ws.frames("color_theme")[0]
    assert frame["title"] == "Fire rating"
    assert frame["groups"][0]["guids"] == [guids[0]]
    # late tabs get the theme from the status payload
    assert h.core.viewer_hub.status_payload()["color_theme"]["title"] == "Fire rating"

    cleared = await h.call("apply_color_theme", clear=True)
    assert cleared["ok"] is True
    assert ws.frames("color_theme")[-1]["clear"] is True
    assert h.core.viewer_hub.status_payload()["color_theme"] is None


async def test_apply_color_theme_requires_groups_or_clear(
    harness_factory, work_model: Path
):
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    _attach_fake_tab(h.core)
    out = await h.call("apply_color_theme")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"


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


# --------------------------------------------------- selection stays in sync
async def test_closed_tab_leaves_no_stale_selection(harness_factory, work_model: Path):
    """A closed or reloaded tab must not leave the LLM reading a selection
    the user cannot see."""
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    guids = await h.core.session.run(
        lambda: [w.GlobalId for w in h.core.session.ifc.by_type("IfcWall")]
    )
    await h.core.viewer_hub.handle_frame(ws.client, {"type": "selection", "guids": guids[:2]})
    assert len(h.core.viewer_hub.selection) == 2

    h.core.viewer_hub.unregister(ws.client)
    assert h.core.viewer_hub.selection == []
    assert h.core.viewer_hub.selected_at is None

    # the tab comes back (F5) and resends whatever it really has
    fresh = _attach_fake_tab(h.core)
    out = await h.call("get_viewer_selection")
    assert out["data"]["guids"] == []
    await h.core.viewer_hub.handle_frame(fresh.client, {"type": "selection", "guids": guids[:1]})
    out = await h.call("get_viewer_selection")
    assert out["data"]["guids"] == guids[:1]


async def test_connected_agent_only_page_is_not_a_hidden_viewer(
    harness_factory, work_model: Path
):
    """Closing the IFC surface must stop tools targeting an invisible scene."""
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    ws = _attach_fake_tab(h.core)
    guid = await h.core.session.run(
        lambda: h.core.session.ifc.by_type("IfcWall")[0].GlobalId
    )
    await h.core.viewer_hub.handle_frame(
        ws.client,
        {"type": "selection", "guids": [guid], "model_id": h.core.models.active_id},
    )

    await h.core.viewer_hub.handle_frame(
        ws.client, {"type": "selection", "guids": [], "model_id": None}
    )

    assert ws.client.view_model_id is None
    assert ws.client.selection_model_id is None
    assert ws.client.selection == []
    assert h.core.viewer_hub.selection == []


async def test_a_second_tab_does_not_erase_what_the_first_one_shows(
    harness_factory, work_model: Path
):
    """Opening a second viewer tab used to make the assistant answer 'nothing
    is selected' and report no measurements while the first tab still showed
    the user's picks and dimensions."""
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    hub = h.core.viewer_hub
    model_id = h.core.models.active_id
    guids = await h.core.session.run(
        lambda: [w.GlobalId for w in h.core.session.ifc.by_type("IfcWall")]
    )
    measurement = {"from": [0.0, 0.0, 0.0], "to": [2.5, 0.0, 0.0], "distance": 2.5}

    first = _attach_fake_tab(h.core)
    await hub.handle_frame(
        first.client, {"type": "selection", "guids": guids[:1], "model_id": model_id}
    )
    await hub.handle_frame(
        first.client,
        {"type": "measurements", "items": [measurement], "model_id": model_id},
    )

    # a fresh tab rebuilds its scene, which publishes both lists empty
    second = _attach_fake_tab(h.core)
    await hub.handle_frame(
        second.client, {"type": "scene_state", "state": "rebuilding", "model_id": model_id}
    )
    await hub.handle_frame(
        second.client, {"type": "measurements", "items": [], "model_id": model_id}
    )
    await hub.handle_frame(
        second.client, {"type": "selection", "guids": [], "model_id": model_id}
    )

    selection = await h.call("get_viewer_selection")
    assert selection["data"]["guids"] == guids[:1]
    assert selection["data"]["tab"] == first.client.id

    measured = await h.call("get_viewer_measurements")
    assert [item["distance"] for item in measured["data"]["measurements"]] == [2.5]
    assert measured["data"]["tab"] == first.client.id


async def test_serializing_for_the_viewer_frees_the_model_lifecycle_lock(
    viewer_core, monkeypatch
):
    """The refetch after an edit must not freeze every tool call, every
    element click and the search box for the length of the serialization."""
    import httpx

    from ifc_console.mcp.server import build_http_app, build_mcp

    serializing = asyncio.Event()
    lock_seen_free = asyncio.Event()
    original_run = viewer_core.session.run
    monkeypatch.setattr(viewer_core.session, "matches_disk", lambda: False)

    async def gated_serialize(fn, **kwargs):
        serializing.set()
        await asyncio.wait_for(lock_seen_free.wait(), timeout=5)
        return await original_run(fn, **kwargs)

    monkeypatch.setattr(viewer_core.session, "run", gated_serialize)
    app = build_http_app(viewer_core, build_mcp(viewer_core))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as ac:
        fetch = asyncio.create_task(ac.get("/api/model.ifc", headers=_auth(viewer_core)))
        await asyncio.wait_for(serializing.wait(), timeout=5)
        async with viewer_core.active_session():  # blocked for the whole serialize before
            lock_seen_free.set()
        response = await asyncio.wait_for(fetch, timeout=10)

    assert response.status_code == 200
    assert response.content.startswith(b"ISO-10303-21")


async def test_every_status_frame_asks_the_tab_to_resend_its_selection(
    viewer_core,
):
    """The browser owns the selection, so the connect handshake must carry it.

    Guards the two halves of that contract that live in app.js: sending the
    selection on a status frame, and again after a model rebuild.
    """

    source = (
        _static_dir() / "app.js"
    ).read_text(encoding="utf-8")
    status_case = source.split('case "status":', 1)[1].split("case ", 1)[0]
    assert "sendSelection()" in status_case, "app.js must resend selection on (re)connect"
    rebuild = source.split("for (const guid of keepSelection)", 1)[1].split("function ", 1)[0]
    assert "sendSelection()" in rebuild, "app.js must resend selection after a rebuild"


async def test_picker_follows_a_pinned_model_after_it_becomes_active(viewer_core):

    source = (
        _static_dir() / "app.js"
    ).read_text(encoding="utf-8")
    picker = source.split("function renderModelPicker", 1)[1].split("function ", 1)[0]
    assert "viewModelId === activeId" in picker
    assert "viewModelId = null" in picker


async def test_viewer_commits_complete_model_with_one_loading_state(viewer_core):

    static = _static_dir()
    source = (static / "app.js").read_text(encoding="utf-8")
    shell = (static / "index.html").read_text(encoding="utf-8")

    assert "progress-toast" not in source
    assert "progress-toast" not in shell
    assert shell.count('id="overlay"') == 1
    assert "progressiveTick" not in source
    assert "onChunk: (msg) => chunks.push(msg)" in source
    assert "decideOrigin(parsed.chunks)" in source
    # the origin must be fixed before any chunk is ingested against it
    # decideOrigin also yields the whole model's bounds and each placement's
    # world box, so batching sees the finished box from the first chunk
    assert source.index("decideOrigin(parsed.chunks)") < source.index(
        "\n    ingestChunk(chunk, placed.layout.get(chunk)"
    )


async def test_static_assets_revalidate_so_upgrades_take_effect(viewer_core):
    """Asset URLs never change, so a cached viewer would survive an upgrade."""
    client = _http_client(viewer_core)
    response = client.get("/viewer/static/app.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"


async def test_section_planes_are_wired_to_every_patched_material(viewer_core):
    """Clipping only works if the shared plane array reaches every material and
    a plane-count change forces the recompile three.js needs."""

    static = _static_dir()
    source = (static / "app.js").read_text(encoding="utf-8")
    shell = (static / "index.html").read_text(encoding="utf-8")

    assert "patchedMaterials.add(mat)" in source
    assert "mat.clippingPlanes = activeClipPlanes" in source
    update = source.split("function updateClipping", 1)[1].split("\nfunction ", 1)[0]
    assert "renderer.localClippingEnabled" in update
    assert "mat.needsUpdate = true" in update
    # the patched shader must keep the clipping include it replaces
    assert "#include <clipping_planes_fragment>" in source
    for axis in ("x", "y", "z"):
        assert f'class="switch section-on" data-axis="{axis}"' in shell


async def test_measurement_reads_depth_on_the_gpu_not_by_raycast(viewer_core):
    """Merged chunks free their CPU arrays on upload (`freeUploadedArray`), so
    THREE.Raycaster has no vertex data and throws. The surface point has to come
    from the same 1x1 GPU pass the id picker uses."""

    static = _static_dir()
    source = (static / "app.js").read_text(encoding="utf-8")
    shell = (static / "index.html").read_text(encoding="utf-8")

    assert "attr.onUpload(freeUploadedArray)" in source, "the premise of this test"
    surface = source.split("function surfacePointAt", 1)[1].split("\nfunction ", 1)[0]
    assert "intersectObjects" not in surface, "raycasting cannot work on freed arrays"
    assert "readRenderTargetPixels" in surface
    # the depth pass itself sits in the probe every depth reader shares
    probe = source.split("function beginDepthProbe", 1)[1].split("\nfunction ", 1)[0]
    assert "scene.overrideMaterial = depthMaterial" in probe
    assert "beginDepthProbe(" in surface
    # both 1x1 passes clip, so a sectioned-away face is neither pickable nor
    # measurable and the surface behind it answers instead
    for material in ("pickMaterial", "depthMaterial"):
        block = source.split(f"const {material} = new THREE.ShaderMaterial", 1)[1]
        block = block.split("});", 1)[0]
        assert "clipping: true" in block, material
        assert "#include <clipping_planes_fragment>" in block, material

    # a model rebuild invalidates every placed measurement
    build = source.split("async function buildScene", 1)[1].split("\nfunction ", 1)[0]
    # A rebuild must not throw the user's dimensions away: they are carried as
    # GlobalId anchors and replayed onto the rebuilt scene.
    assert "measurementCarry()" in build
    assert "restoreMeasurements(keepMeasurements)" in build
    assert "clearMeasurements()" not in build
    assert "updateClipping()" in build
    assert 'id="tool-measure"' in shell
    assert 'id="measure-card"' in shell


async def test_escape_exits_the_active_tool_before_closing_popovers(viewer_core):

    source = (
        _static_dir() / "app.js"
    ).read_text(encoding="utf-8")
    # Anchor on the comment that names this handler's whole job: the file has
    # several keydown listeners now, including the measurement axis lock, and
    # positional slicing picked whichever happened to be declared first.
    escape = source.split(
        "// an active tool owns Escape first, then the popovers", 1
    )[1].split("return;", 1)[0]
    assert "setMeasureMode(false)" in escape
    assert escape.index("setMeasureMode(false)") < escape.index("closePopovers()")


async def test_wheel_zoom_always_invalidates_the_viewer_frame(viewer_core):

    static = _static_dir()
    source = (static / "app.js").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")

    assert 'controls.addEventListener("change", invalidate)' in source
    canvas_rule = css.split("#canvas {", 1)[1].split("}", 1)[0]
    assert "overscroll-behavior: contain" in canvas_rule
    assert "touch-action: none" in canvas_rule


# ------------------------------------------------- more than one model in view
async def test_status_lists_every_resident_model(viewer_core, tmp_path: Path):
    """The viewer picker is driven by this list; active leads."""
    import shutil

    second = tmp_path / "annex.ifc"
    shutil.copy2(viewer_core.session.path, second)
    await viewer_core.open_model(second, attach=True)

    rows = viewer_core.viewer_hub.model_rows()
    assert [r["active"] for r in rows] == [True, False]
    assert rows[0]["name"] == "work.ifc"
    assert rows[1]["id"] == "annex"
    assert rows[0]["etag"] and rows[1]["etag"] != rows[0]["etag"]

    payload = viewer_core.viewer_hub.status_payload()
    assert [r["id"] for r in payload["models"]] == [r["id"] for r in rows]


async def test_model_route_serves_an_attached_model(viewer_core, tmp_path: Path):
    import shutil

    second = tmp_path / "annex.ifc"
    shutil.copy2(viewer_core.session.path, second)
    await viewer_core.open_model(second, attach=True)
    client = _http_client(viewer_core)

    active = client.get("/api/model.ifc", headers=_auth(viewer_core))
    attached = client.get("/api/model.ifc?model=annex", headers=_auth(viewer_core))
    assert active.status_code == attached.status_code == 200
    assert attached.headers["ETag"] != active.headers["ETag"]
    assert attached.content.startswith(b"ISO-10303-21")

    missing = client.get("/api/model.ifc?model=ghost", headers=_auth(viewer_core))
    assert missing.status_code == 404
    assert missing.json()["error"] == "MODEL_NOT_FOUND"


async def test_element_route_reads_the_named_model(viewer_core, tmp_path: Path):
    import shutil

    second = tmp_path / "annex.ifc"
    shutil.copy2(viewer_core.session.path, second)
    await viewer_core.open_model(second, attach=True)
    guid = (
        await viewer_core.session.run(
            lambda: viewer_core.session.ifc.by_type("IfcWall")[0].GlobalId
        )
    )
    client = _http_client(viewer_core)
    response = client.get(f"/api/elements/{guid}?model=annex", headers=_auth(viewer_core))
    assert response.status_code == 200
    assert response.json()["class"] == "IfcWall"


async def test_attaching_a_model_refreshes_connected_tabs(viewer_core, tmp_path: Path):
    import shutil

    ws = _attach_fake_tab(viewer_core)
    second = tmp_path / "annex.ifc"
    shutil.copy2(viewer_core.session.path, second)
    await viewer_core.open_model(second, attach=True)
    await asyncio.sleep(0)  # the broadcast is scheduled on the loop

    status = ws.frames("status")[-1]
    assert [r["id"] for r in status["models"]] == ["work", "annex"]


async def test_model_bytes_cache_survives_switching(viewer_core, tmp_path: Path):
    """Two models must not evict each other's serialized bytes."""
    import shutil

    second = tmp_path / "annex.ifc"
    shutil.copy2(viewer_core.session.path, second)
    await viewer_core.open_model(second, attach=True)
    hub = viewer_core.viewer_hub
    active_etag = hub.model_etag(viewer_core.session)
    annex_etag = hub.model_etag(viewer_core.models.require("annex"))

    hub.cache_model_bytes(active_etag, b"active")
    hub.cache_model_bytes(annex_etag, b"annex")
    assert hub.cached_model_bytes(active_etag) == b"active"
    assert hub.cached_model_bytes(annex_etag) == b"annex"
