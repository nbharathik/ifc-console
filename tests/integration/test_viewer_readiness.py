"""Only an acknowledgement of the current parsed model proves readiness."""

from ifc_console.viewer.hub import ViewerClient


async def test_model_readiness_requires_current_model_and_revision(core, work_model):
    await core.open_model(work_model)
    hub = core.viewer_hub
    client = ViewerClient(None)
    events = []
    core.events.subscribe(events.append)
    frame = {"type": "model_ready", "model_id": core.models.active_id,
             "etag": hub.model_etag()}

    await hub.handle_frame(client, {"type": "scene_state", "state": "ready"})
    await hub.handle_frame(client, {**frame, "model_id": "missing"})
    await hub.handle_frame(client, {**frame, "etag": "stale"})
    assert not events
    assert client.ready_etag is None

    await hub.handle_frame(client, frame)
    assert events[-1]["type"] == "viewer_model_ready"
    assert events[-1]["revision"] == core.session.revision
    assert events[-1]["model_id"] == core.models.active_id
    assert client.ready_etag == hub.model_etag()
    await hub.handle_frame(client, frame)
    assert len(events) == 1

    await hub.handle_frame(client, {"type": "scene_state", "state": "rebuilding"})
    assert client.ready_etag is None
    await hub.handle_frame(client, frame)
    assert len(events) == 1
    await hub.handle_frame(client, {"type": "scene_state", "state": "ready"})
    await hub.handle_frame(client, frame)
    assert len(events) == 2


async def test_delayed_ready_acknowledgement_is_not_reported_after_an_edit(core, work_model):
    await core.open_model(work_model)
    hub = core.viewer_hub
    client = ViewerClient(None)
    etag = hub.model_etag()
    core.session.revision += 1
    events = []
    core.events.subscribe(events.append)
    await hub.handle_frame(client, {
        "type": "model_ready", "model_id": core.models.active_id, "etag": etag,
    })
    assert not events
    assert client.ready_etag is None
