"""ViewerHub unit tests: registry, selection, screenshot correlation, events."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest

from ifc_console.mcp.envelope import ToolError

# 1x1 red pixel, a real PNG (magic bytes included)
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQ"
    "DwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeWS:
    """Collects frames; optionally auto-answers screenshot requests."""

    def __init__(self, hub=None, respond_shots: dict | None = None) -> None:
        self.sent: list[dict] = []
        self.hub = hub
        self.respond_shots = respond_shots
        self.client = None

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        if frame.get("type") == "screenshot_request" and self.respond_shots is not None:
            reply = {"type": "screenshot_response", "id": frame["id"], **self.respond_shots}
            asyncio.get_running_loop().create_task(
                self.hub.handle_frame(self.client, reply)
            )

    def frames(self, ftype: str) -> list[dict]:
        return [f for f in self.sent if f.get("type") == ftype]


@pytest.fixture
def hub(core):
    return core.viewer_hub


def _attach(hub, **kwargs) -> FakeWS:
    ws = FakeWS(hub=hub, **kwargs)
    ws.client = hub.register(ws)
    return ws


async def test_require_connected_raises_with_hint(hub):
    with pytest.raises(ToolError) as err:
        hub.require_connected()
    assert err.value.code == "VIEWER_NOT_CONNECTED"
    assert "query_elements" in err.value.hint


async def test_register_unregister_updates_core_state(core, hub):
    ws = _attach(hub)
    assert core.viewer.connected == 1
    hub.unregister(ws.client)
    assert core.viewer.connected == 0
    # idempotent: unregistering twice must not go negative or raise
    hub.unregister(ws.client)
    assert core.viewer.connected == 0


async def test_selection_frame_updates_hub_and_emits(core, hub):
    seen = []
    core.events.subscribe(lambda e: seen.append(e))
    ws = _attach(hub)
    await hub.handle_frame(ws.client, {"type": "selection", "guids": ["abc", "def", 42]})
    assert hub.selection == ["abc", "def"]  # non-strings dropped
    assert core.viewer.selection == ["abc", "def"]
    assert hub.selected_at is not None
    assert any(e["type"] == "viewer_selection" and e["count"] == 2 for e in seen)


async def test_closing_latest_tab_restores_other_tabs_selection(core, hub, work_model: Path):
    await core.open_model(work_model)
    ws1, ws2 = _attach(hub), _attach(hub)
    model_id = core.models.active_id
    await hub.handle_frame(
        ws1.client, {"type": "selection", "guids": ["first"], "model_id": model_id}
    )
    await hub.handle_frame(
        ws2.client, {"type": "selection", "guids": ["second"], "model_id": model_id}
    )

    hub.unregister(ws2.client)
    assert hub.selection == ["first"]
    assert hub.selection_model_id == model_id

    hub.unregister(ws1.client)
    assert hub.selection == []
    assert hub.selection_model_id is None


async def test_selection_for_an_unknown_model_is_discarded(core, hub, work_model: Path):
    await core.open_model(work_model)
    ws = _attach(hub)
    await hub.handle_frame(
        ws.client, {"type": "selection", "guids": ["stale"], "model_id": "gone"}
    )
    assert hub.selection == []
    assert hub.selection_model_id is None


async def test_broadcast_reaches_all_tabs(hub):
    ws1, ws2 = _attach(hub), _attach(hub)
    await hub.broadcast({"type": "ping"})
    assert ws1.frames("ping") and ws2.frames("ping")


async def test_highlight_state_tracked_for_resync(hub):
    ws = _attach(hub)
    await hub.send_highlight(["g1"], color="#00ff00", isolate=True, fit=True, clear=False)
    assert hub.last_highlight["guids"] == ["g1"]
    assert ws.frames("highlight")[0]["isolate"] is True
    await hub.send_highlight([], color="#00ff00", isolate=False, fit=False, clear=True)
    assert hub.last_highlight is None


async def test_screenshot_roundtrip(hub):
    payload = {
        "data_b64": base64.b64encode(TINY_PNG).decode(),
        "width": 1,
        "height": 1,
    }
    ws = _attach(hub, respond_shots=payload)
    data, width, height = await hub.request_screenshot(
        view="iso", fit=None, max_size=800, format="png", quality=85
    )
    assert data == TINY_PNG
    assert (width, height) == (1, 1)
    request = ws.frames("screenshot_request")[0]
    assert request["view"] == "iso"
    assert request["max_size"] == 800


async def test_screenshot_goes_to_most_recent_tab(hub):
    payload = {"data_b64": base64.b64encode(TINY_PNG).decode(), "width": 1, "height": 1}
    ws1 = _attach(hub, respond_shots=payload)
    await asyncio.sleep(0.01)
    ws2 = _attach(hub, respond_shots=payload)
    await hub.request_screenshot(view="top", fit=None, max_size=64, format="png", quality=1)
    assert not ws1.frames("screenshot_request")
    assert ws2.frames("screenshot_request")


async def test_screenshot_client_error_becomes_viewer_error(hub):
    _attach(hub, respond_shots={"error": "webgl context lost"})
    with pytest.raises(ToolError) as err:
        await hub.request_screenshot(
            view="current", fit=None, max_size=800, format="jpeg", quality=85
        )
    assert err.value.code == "VIEWER_ERROR"
    assert "webgl context lost" in err.value.message


async def test_screenshot_rejects_non_image_bytes(hub):
    _attach(
        hub,
        respond_shots={
            "data_b64": base64.b64encode(b"definitely not an image").decode(),
            "width": 1,
            "height": 1,
        },
    )
    with pytest.raises(ToolError) as err:
        await hub.request_screenshot(
            view="current", fit=None, max_size=800, format="png", quality=85
        )
    assert err.value.code == "VIEWER_ERROR"


async def test_screenshot_timeout(hub, monkeypatch):
    monkeypatch.setattr("ifc_console.viewer.hub._SCREENSHOT_TIMEOUT", 0.05)
    _attach(hub)  # never answers
    with pytest.raises(ToolError) as err:
        await hub.request_screenshot(
            view="current", fit=None, max_size=800, format="png", quality=85
        )
    assert err.value.code == "VIEWER_TIMEOUT"
    assert not hub._shots  # correlation table must not leak


async def test_model_events_translate_to_frames(core, hub, work_model: Path):
    ws = _attach(hub)
    await core.open_model(work_model)
    await asyncio.sleep(0)  # let the scheduled broadcast task run
    frames = ws.frames("model_updated")
    assert frames and frames[-1]["reason"] == "loaded"
    assert frames[-1]["etag"] == f"{core.session.fingerprint}-{core.session.revision}"
    assert ws.frames("status")[-1]["model"] == work_model.name

    core.events.emit("model_mutated", tool="test")
    await asyncio.sleep(0)
    assert ws.frames("model_updated")[-1]["reason"] == "edited"

    core.set_mode(core.policy.mode, by="test")  # no-op: same mode emits nothing
    core.events.emit("mode_changed", mode="edit")
    await asyncio.sleep(0)
    assert ws.frames("mode_changed")[-1]["mode"] == "edit"


async def test_model_load_clears_selection_and_highlight(core, hub, work_model: Path):
    ws = _attach(hub)
    await hub.handle_frame(ws.client, {"type": "selection", "guids": ["stale"]})
    await hub.send_highlight(["stale"], color="#ff0000", isolate=False, fit=False, clear=False)
    await core.open_model(work_model)
    assert hub.selection == []
    assert hub.last_highlight is None


async def test_eviction_reaches_tabs_and_prunes_selection(core, hub, work_model: Path):
    await core.open_model(work_model)
    model_id = core.models.active_id
    ws = _attach(hub)
    await hub.handle_frame(
        ws.client, {"type": "selection", "guids": ["x"], "model_id": model_id}
    )
    core.models.drop(model_id, force=True)
    core.events.emit("model_evicted", model_id=model_id, name=work_model.name)
    await asyncio.sleep(0)
    assert hub.selection == []
    assert hub.selection_model_id is None
    assert ws.frames("status")


async def test_status_payload_shape(core, hub, work_model: Path):
    await core.open_model(work_model)
    status = hub.status_payload()
    assert status["type"] == "status"
    assert status["model"] == "work.ifc"
    assert status["schema"] == "IFC4"
    assert status["mode"] == "ask"
    assert status["etag"] == hub.model_etag()
    assert status["selection"] == []


async def test_model_bytes_cache_by_etag(hub):
    hub.cache_model_bytes("etag-1", b"data-1")
    assert hub.cached_model_bytes("etag-1") == b"data-1"
    assert hub.cached_model_bytes("etag-2") is None
