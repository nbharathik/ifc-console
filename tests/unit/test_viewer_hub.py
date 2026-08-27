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
            reply = {
                "type": "screenshot_response",
                "id": frame["id"],
                "model_id": frame.get("model_id"),
                **self.respond_shots,
            }
            asyncio.get_running_loop().create_task(self.hub.handle_frame(self.client, reply))

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


async def test_selection_rejects_non_lists_and_oversized_identifiers(hub):
    ws = _attach(hub)
    await hub.handle_frame(ws.client, {"type": "selection", "guids": "not-a-list"})
    assert hub.selection == []

    await hub.handle_frame(
        ws.client,
        {"type": "selection", "guids": ["valid", "x" * 129, 42]},
    )
    assert hub.selection == ["valid"]


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
    await hub.handle_frame(ws.client, {"type": "selection", "guids": ["stale"], "model_id": "gone"})
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


async def test_screenshot_targets_a_tab_showing_the_active_model(core, hub, work_model: Path):
    await core.open_model(work_model)
    payload = {"data_b64": base64.b64encode(TINY_PNG).decode(), "width": 1, "height": 1}
    active = _attach(hub, respond_shots=payload)
    pinned = _attach(hub, respond_shots=payload)
    await hub.handle_frame(
        active.client,
        {"type": "selection", "guids": [], "model_id": core.models.active_id},
    )
    pinned.client.view_model_id = "attached-model"
    pinned.client.touch()

    await hub.request_screenshot(view="top", fit=None, max_size=64, format="png", quality=1)

    assert active.frames("screenshot_request")
    assert not pinned.frames("screenshot_request")
    assert active.frames("screenshot_request")[0]["model_id"] == core.models.active_id


async def test_screenshot_ignores_a_response_from_another_tab(hub, monkeypatch):
    monkeypatch.setattr("ifc_console.viewer.hub._SCREENSHOT_TIMEOUT", 0.05)
    target = _attach(hub)
    other = _attach(hub)
    target.client.touch()

    request = asyncio.create_task(
        hub.request_screenshot(view="top", fit=None, max_size=64, format="png", quality=1)
    )
    await asyncio.sleep(0)
    frame = target.frames("screenshot_request")[0]
    await hub.handle_frame(
        other.client,
        {
            "type": "screenshot_response",
            "id": frame["id"],
            "model_id": frame.get("model_id"),
            "data_b64": base64.b64encode(TINY_PNG).decode(),
            "width": 1,
            "height": 1,
        },
    )

    with pytest.raises(ToolError) as err:
        await request
    assert err.value.code == "VIEWER_TIMEOUT"


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


@pytest.mark.parametrize(
    "payload",
    [
        {"data_b64": {"not": "text"}, "width": 1, "height": 1},
        {
            "data_b64": base64.b64encode(TINY_PNG).decode(),
            "width": "not-a-number",
            "height": 1,
        },
        {
            "data_b64": base64.b64encode(TINY_PNG).decode(),
            "width": 9000,
            "height": 1,
        },
    ],
)
async def test_screenshot_rejects_malformed_metadata(hub, payload):
    _attach(hub, respond_shots=payload)
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
    assert frames[-1]["etag"] == (
        f"{core.session.model_id}-{core.session.fingerprint}-{core.session.revision}"
    )
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
    await hub.handle_frame(ws.client, {"type": "selection", "guids": ["x"], "model_id": model_id})
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
    assert len(status["project_scope"]) == 16
    assert str(core.store.project_dir) not in status["project_scope"]


async def test_model_bytes_cache_by_etag(hub):
    hub.cache_model_bytes("etag-1", b"data-1")
    assert hub.cached_model_bytes("etag-1") == b"data-1"
    assert hub.cached_model_bytes("etag-2") is None


# ------------------------------------------- a rebuild only when geometry moved
async def test_a_mutation_asks_for_a_rebuild_unless_it_says_it_touched_no_shape(
    core, hub, work_model: Path
):
    """A full rebuild re-downloads and re-parses the model, which is seconds to
    minutes on a large file; an edit that touched no representation must not."""
    ws = _attach(hub)
    await core.open_model(work_model)
    await asyncio.sleep(0)
    assert ws.frames("model_updated")[-1]["geometry"] is True

    core.events.emit("model_mutated", tool="execute_ifc_code")
    await asyncio.sleep(0)
    assert ws.frames("model_updated")[-1]["geometry"] is True

    core.events.emit("model_mutated", tool="preview_property_change", geometry=False)
    await asyncio.sleep(0)
    frame = ws.frames("model_updated")[-1]
    assert frame["geometry"] is False
    assert frame["reason"] == "edited"


async def test_saving_never_asks_the_tab_to_rebuild(core, hub, work_model: Path):
    """Saving writes the in-memory model out; nothing in it changed, so the
    ETag bump must not cost a full browser rebuild."""
    ws = _attach(hub)
    await core.open_model(work_model)
    core.events.emit("model_saved", path=str(work_model))
    await asyncio.sleep(0)
    frame = ws.frames("model_updated")[-1]
    assert frame["reason"] == "saved"
    assert frame["geometry"] is False


async def test_a_mutation_names_the_elements_it_touched(core, hub, work_model: Path):
    ws = _attach(hub)
    await core.open_model(work_model)
    core.events.emit("model_mutated", tool="t", geometry=False, guids=["g-1", 7, "x" * 200])
    await asyncio.sleep(0)
    assert ws.frames("model_updated")[-1]["elements"] == ["g-1"]


# --------------------------------------------------- the viewport on the wire
async def test_status_carries_the_files_length_unit(core, hub, work_model: Path):
    await core.open_model(work_model)
    assert hub.status_payload()["units"] is None  # not read off the worker yet

    await hub.refresh_units()
    units = hub.status_payload()["units"]
    assert units["length_unit"]
    assert units["to_si_factor"] > 0
    assert hub.status_payload()["models"][0]["units"] == units


async def test_a_viewer_state_frame_becomes_a_one_line_summary(core, hub, work_model: Path):
    await core.open_model(work_model)
    ws = _attach(hub)
    await hub.handle_frame(
        ws.client,
        {
            "type": "viewer_state",
            "state": {
                "model_id": core.models.active_id,
                "camera": {
                    "position": [1.0, 2.0, 3.0],
                    "target": [0.0, 0.0, 0.0],
                    "projection": "orthographic",
                    "world_per_pixel": 0.004,
                    "junk": "dropped",
                },
                "viewport": {"width": 1280, "height": 720},
                "visibility": {"hidden": 412, "total": 14400},
                "section": {"z": {"at": 1.2, "keep": "below"}},
                "view": "top",
            },
        },
    )
    state = hub.viewport_state()
    assert state["camera"]["position"] == [1.0, 2.0, 3.0]
    assert "junk" not in state["camera"]
    assert state["camera"]["world_per_pixel"] == 0.004
    assert state["viewport"] == {"width": 1280, "height": 720}

    summary = hub.viewport_summary()
    assert "orthographic top view" in summary
    assert "section cut on z at 1.2 m keeping below" in summary
    assert "412 of 14400 elements hidden" in summary


async def test_a_malformed_viewer_state_leaves_the_last_good_one(core, hub):
    ws = _attach(hub)
    await hub.handle_frame(
        ws.client, {"type": "viewer_state", "state": {"visibility": {"isolated": 12}}}
    )
    await hub.handle_frame(ws.client, {"type": "viewer_state", "state": "not-a-dict"})
    assert hub.viewport_summary() == "isolated to 12 elements"


# ----------------------------------------- provenance survives the whitelists
async def test_the_measurement_whitelist_keeps_what_makes_a_number_usable(hub):
    ws = _attach(hub)
    await hub.handle_frame(
        ws.client,
        {
            "type": "measurements",
            "items": [
                {
                    "id": "m-1",
                    "kind": "dimensions",
                    "length": 2.0,
                    "area": 4.0,
                    "approximate": True,
                },
                {
                    "kind": "angle",
                    "degrees": 90.0,
                    "at": [0.0, 0.0, 0.0],
                    "from": [1.0, 0.0, 0.0],
                    "to": [0.0, 1.0, 0.0],
                },
                {
                    "kind": "area",
                    "area": 6.0,
                    "points": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [1.0, 1.0, 0.0]],
                    "normal": [0.0, 0.0, 1.0],
                    "centre": [0.5, 0.5, 0.0],
                },
            ],
        },
    )
    dimensions, angle, area = ws.client.measurements
    # without this flag the caller gets a suspect area with nothing saying so
    assert dimensions["approximate"] is True
    assert dimensions["id"] == "m-1"
    assert angle["from"] == [1.0, 0.0, 0.0] and angle["to"] == [0.0, 1.0, 0.0]
    assert area["normal"] == [0.0, 0.0, 1.0]
    assert area["centre"] == [0.5, 0.5, 0.0]


# ----------------------------------------------- one tab must not erase another
DISTANCE = {"from": [0.0, 0.0, 0.0], "to": [2.5, 0.0, 0.0], "distance": 2.5}


async def test_a_second_tab_does_not_erase_the_first_tabs_selection(
    core, hub, work_model: Path
):
    """Every tab publishes an empty selection while it rebuilds its scene, so
    last-writer-wins made a newly opened tab answer 'nothing is selected'."""
    await core.open_model(work_model)
    model_id = core.models.active_id
    ws1 = _attach(hub)
    await hub.handle_frame(
        ws1.client, {"type": "selection", "guids": ["first"], "model_id": model_id}
    )

    ws2 = _attach(hub)
    await hub.handle_frame(ws2.client, {"type": "selection", "guids": [], "model_id": model_id})

    assert hub.selection == ["first"]
    assert hub.selection_client_id == ws1.client.id

    # the tab that owns the selection may still empty it
    await hub.handle_frame(ws1.client, {"type": "selection", "guids": [], "model_id": model_id})
    assert hub.selection == []


async def test_a_second_tab_does_not_erase_the_first_tabs_measurements(
    core, hub, work_model: Path
):
    await core.open_model(work_model)
    model_id = core.models.active_id
    ws1 = _attach(hub)
    await hub.handle_frame(
        ws1.client, {"type": "measurements", "items": [DISTANCE], "model_id": model_id}
    )

    ws2 = _attach(hub)
    await hub.handle_frame(ws2.client, {"type": "measurements", "items": [], "model_id": model_id})

    items, measured_at = hub.latest_measurements()
    assert [item["distance"] for item in items] == [2.5]
    assert measured_at is not None
    assert hub.measurement_source() is ws1.client


async def test_a_measurement_frame_for_another_model_is_ignored(
    core, hub, work_model: Path
):
    await core.open_model(work_model)
    ws = _attach(hub)
    await hub.handle_frame(
        ws.client, {"type": "measurements", "items": [DISTANCE], "model_id": "annex"}
    )
    assert hub.latest_measurements() == ([], None)


# --------------------------------------------- nothing runs against no scene
async def test_a_command_during_a_rebuild_is_busy_not_a_bad_argument(hub, monkeypatch):
    monkeypatch.setattr("ifc_console.viewer.hub._REBUILD_WAIT", 0.1)
    ws = _attach(hub)
    await hub.handle_frame(ws.client, {"type": "scene_state", "state": "rebuilding"})

    with pytest.raises(ToolError) as err:
        await hub.run_command("set-selection", {"guids": ["g-1"]})

    assert err.value.code == "VIEWER_BUSY"
    assert "retry" in err.value.hint.lower()
    assert not ws.frames("command")  # the tab never heard about it


async def test_a_screenshot_during_a_rebuild_is_refused_not_an_empty_viewport(
    hub, monkeypatch
):
    monkeypatch.setattr("ifc_console.viewer.hub._REBUILD_WAIT", 0.1)
    payload = {"data_b64": base64.b64encode(TINY_PNG).decode(), "width": 1, "height": 1}
    ws = _attach(hub, respond_shots=payload)
    await hub.handle_frame(ws.client, {"type": "scene_state", "state": "rebuilding"})

    with pytest.raises(ToolError) as err:
        await hub.request_screenshot(
            view="current", fit=None, max_size=800, format="png", quality=85
        )

    assert err.value.code == "VIEWER_BUSY"
    assert not ws.frames("screenshot_request")


async def test_a_short_rebuild_is_waited_out_rather_than_refused(hub):
    payload = {"data_b64": base64.b64encode(TINY_PNG).decode(), "width": 1, "height": 1}
    ws = _attach(hub, respond_shots=payload)
    await hub.handle_frame(ws.client, {"type": "scene_state", "state": "rebuilding"})

    async def ready_soon() -> None:
        await asyncio.sleep(0.1)
        await hub.handle_frame(ws.client, {"type": "scene_state", "state": "ready"})

    asyncio.get_running_loop().create_task(ready_soon())
    data, _, _ = await hub.request_screenshot(
        view="current", fit=None, max_size=800, format="png", quality=85
    )
    assert data == TINY_PNG


async def test_a_rebuilding_tab_is_passed_over_for_one_with_a_scene(hub):
    payload = {"data_b64": base64.b64encode(TINY_PNG).decode(), "width": 1, "height": 1}
    ready = _attach(hub, respond_shots=payload)
    rebuilding = _attach(hub, respond_shots=payload)
    # the rebuilding tab is the more recently active one, which used to win
    await hub.handle_frame(rebuilding.client, {"type": "scene_state", "state": "rebuilding"})

    await hub.request_screenshot(
        view="current", fit=None, max_size=800, format="png", quality=85
    )

    assert ready.frames("screenshot_request")
    assert not rebuilding.frames("screenshot_request")


async def test_a_closing_tab_fails_its_pending_screenshot_at_once(hub):
    ws = _attach(hub)  # never answers
    request = asyncio.create_task(
        hub.request_screenshot(view="current", fit=None, max_size=800, format="png", quality=85)
    )
    await asyncio.sleep(0)
    assert ws.frames("screenshot_request")
    hub.unregister(ws.client)

    with pytest.raises(ToolError) as err:
        await request
    assert err.value.code == "VIEWER_NOT_CONNECTED"
    assert not hub._shots


async def test_a_closing_tab_fails_its_pending_command_at_once(hub):
    ws = _attach(hub)  # never answers
    request = asyncio.create_task(hub.run_command("get-context"))
    await asyncio.sleep(0)
    assert ws.frames("command")
    hub.unregister(ws.client)

    with pytest.raises(ToolError) as err:
        await request
    assert err.value.code == "VIEWER_NOT_CONNECTED"
    assert not hub._commands


async def test_a_command_result_from_another_tab_is_ignored(hub, monkeypatch):
    monkeypatch.setattr("ifc_console.viewer.hub._COMMAND_TIMEOUT", 0.05)
    target = _attach(hub)
    other = _attach(hub)
    target.client.touch()

    request = asyncio.create_task(hub.run_command("get-context"))
    await asyncio.sleep(0)
    sent = target.frames("command")[0]
    await hub.handle_frame(
        other.client,
        {"type": "command_result", "id": sent["id"], "ok": True, "result": {"spoofed": True}},
    )

    with pytest.raises(ToolError) as err:
        await request
    assert err.value.code == "VIEWER_TIMEOUT"


# ------------------------------------------------------ model bytes off the lock
async def test_one_serialization_is_shared_by_every_waiting_tab(
    core, hub, work_model: Path, monkeypatch
):
    await core.open_model(work_model)
    session = core.session
    etag = hub.model_etag(session)
    calls = []

    async def serialize(_fn, **_kwargs):
        calls.append(1)
        await asyncio.sleep(0.05)
        return b"serialized"

    monkeypatch.setattr(session, "run", serialize)
    first = hub.model_bytes_job(etag, session)
    second = hub.model_bytes_job(etag, session)

    assert first is second
    assert await asyncio.gather(first, second) == [b"serialized", b"serialized"]
    assert calls == [1]
    assert hub.cached_model_bytes(etag) == b"serialized"


async def test_bytes_from_a_revision_that_moved_on_are_not_cached(
    core, hub, work_model: Path, monkeypatch
):
    """Without the re-check the next tab would be served these bytes under an
    ETag they no longer describe."""
    await core.open_model(work_model)
    session = core.session
    etag = hub.model_etag(session)

    async def serialize(_fn, **_kwargs):
        session.mark_dirty()  # an edit lands while the bytes are being produced
        return b"stale"

    monkeypatch.setattr(session, "run", serialize)
    assert await hub.model_bytes_job(etag, session) == b"stale"
    assert hub.cached_model_bytes(etag) is None
