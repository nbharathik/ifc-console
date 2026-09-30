"""The viewer's Undo, Redo and Changes routes."""

from __future__ import annotations

import pytest

from tests.integration.test_viewer import _auth, _http_client, viewer_core  # noqa: F401

pytestmark = pytest.mark.asyncio


async def _rename(core, name: str, description: str) -> None:
    session = core.session
    wall = session.ifc.by_type("IfcWall")[0]
    await session.run(
        lambda: session.mutate(
            lambda: setattr(wall, "Name", name), tool="test", description=description
        )
    )


async def test_undo_and_redo_routes_step_the_model(viewer_core) -> None:  # noqa: F811
    await _rename(viewer_core, "Renamed", "rename the wall")
    client = _http_client(viewer_core)
    session = viewer_core.session
    wall_guid = session.ifc.by_type("IfcWall")[0].GlobalId

    undone = client.post("/api/model/undo", headers=_auth(viewer_core))

    assert undone.status_code == 200
    assert undone.json()["step"]["description"] == "rename the wall"
    assert undone.json()["dirty"] is False
    assert session.ifc.by_guid(wall_guid).Name != "Renamed"

    redone = client.post("/api/model/redo", headers=_auth(viewer_core))

    assert redone.status_code == 200
    assert redone.json()["dirty"] is True
    assert session.ifc.by_guid(wall_guid).Name == "Renamed"


async def test_undo_with_nothing_to_undo_is_a_conflict_with_a_hint(viewer_core) -> None:  # noqa: F811
    client = _http_client(viewer_core)

    response = client.post("/api/model/undo", headers=_auth(viewer_core))

    assert response.status_code == 409
    assert response.json()["error"] == "NOTHING_TO_UNDO"
    assert response.json()["hint"]


async def test_the_changes_route_lists_steps_and_the_saved_position(viewer_core) -> None:  # noqa: F811
    await _rename(viewer_core, "One", "first")
    viewer_core.session.history.mark_saved()
    await _rename(viewer_core, "Two", "second")
    client = _http_client(viewer_core)

    body = client.get("/api/model/changes", headers=_auth(viewer_core)).json()

    assert [step["description"] for step in body["applied"]] == ["first", "second"]
    assert body["saved_after"] == 1
    assert body["can_undo"] is True
    assert body["can_redo"] is False


async def test_the_routes_need_the_token(viewer_core) -> None:  # noqa: F811
    client = _http_client(viewer_core)

    assert client.post("/api/model/undo").status_code == 401
    assert client.post("/api/model/redo").status_code == 401
    assert client.get("/api/model/changes").status_code == 401
