"""The server driving the viewer, and hearing back."""

from __future__ import annotations

import asyncio
import json

import pytest

from ifc_console.core.results import ToolError


class RecordingWs:
    """A viewer tab that answers whatever the hub asks it."""

    def __init__(self, reply=None) -> None:
        self.sent: list[dict] = []
        self.reply = reply

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        if frame.get("type") != "command" or self.reply is None:
            return
        # Answer on the loop, the way a real socket would.
        asyncio.get_running_loop().create_task(self.reply(frame))


async def _connected(core, reply=None):
    hub = core.viewer_hub
    ws = RecordingWs(reply)
    client = hub.register(ws)
    client.view_model_id = core.models.active_id
    return hub, client, ws


async def _register_viewer_tools(core) -> None:
    from ifc_console.application.operations import (
        build_operations,
        register_viewer_operations,
    )

    build_operations(core)
    core.viewer.enabled = True
    register_viewer_operations(core)


class TestRunCommand:
    async def test_a_command_reaches_the_tab_and_its_result_comes_back(self, core):
        async def reply(frame):
            await hub.handle_frame(
                client,
                {
                    "type": "command_result",
                    "id": frame["id"],
                    "ok": True,
                    "result": {"projection": "orthographic"},
                },
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        result = await hub.run_command("set-projection", {"projection": "orthographic"})
        assert result == {"projection": "orthographic"}
        sent = ws.sent[-1]
        assert sent["type"] == "command"
        assert sent["action"] == "set-projection"
        assert sent["projection"] == "orthographic"
        assert sent["id"]

    async def test_the_viewer_s_own_refusal_is_what_the_caller_sees(self, core):
        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": False,
                 "error": "Nothing is selected"},
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        with pytest.raises(ToolError) as caught:
            await hub.run_command("focus-selection")
        assert caught.value.code == "VIEWER_ERROR"
        assert "Nothing is selected" in str(caught.value)

    async def test_a_tab_that_never_answers_times_out(self, core, monkeypatch):
        monkeypatch.setattr("ifc_console.viewer.hub._COMMAND_TIMEOUT", 0.05)
        hub, client, ws = await _connected(core)
        with pytest.raises(ToolError) as caught:
            await hub.run_command("get-context")
        assert caught.value.code == "VIEWER_TIMEOUT"

    async def test_an_oversized_reply_is_refused_not_returned(self, core, monkeypatch):
        monkeypatch.setattr("ifc_console.viewer.hub._MAX_COMMAND_RESULT", 100)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True,
                 "result": {"blob": "x" * 5000}},
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        with pytest.raises(ToolError) as caught:
            await hub.run_command("get-context")
        assert caught.value.code == "RESULT_TOO_LARGE"

    async def test_no_tab_showing_the_model_is_the_usual_clear_error(self, core):
        with pytest.raises(ToolError) as caught:
            await core.viewer_hub.run_command("get-context")
        assert caught.value.code == "VIEWER_NOT_CONNECTED"


class TestOpenViewer:
    async def test_an_mcp_client_can_turn_the_viewer_on(self, core, no_browser):
        from ifc_console.application.operations import build_operations
        from ifc_console.mcp import tools_viewer

        build_operations(core)
        assert not core.viewer.enabled
        assert all(name in core.operations for name in tools_viewer.TOOL_NAMES)

        result = await core.operation_service.call(
            "open_viewer", {"wait_for_connection_s": 0}
        )
        assert result.ok is True
        assert result.data["enabled"] is True
        assert result.data["connected"] is False
        assert result.data["ready"] is False
        assert result.data["next_action"]
        assert core.viewer.enabled
        # The stable viewport tools remain on the same operation surface.
        for name in tools_viewer.TOOL_NAMES:
            assert name in core.operations
        # the tokenized link went to the local browser, never into the result
        assert no_browser and "#t=" in no_browser[0]
        assert "#t=" not in result.data["url"]

        again = await core.operation_service.call(
            "open_viewer", {"wait_for_connection_s": 0}
        )
        assert again.ok is True

    async def test_launcher_waits_until_the_tab_is_ready(self, core, work_model):
        from ifc_console.application.operations import build_operations

        build_operations(core)
        await core.open_model(work_model)
        pending = asyncio.create_task(
            core.operation_service.call(
                "open_viewer", {"open_browser": True, "wait_for_connection_s": 1}
            )
        )
        await asyncio.sleep(0.02)
        hub, client, _ws = await _connected(core)

        result = await pending

        assert result.ok is True
        assert result.data["connected"] is True
        assert result.data["ready"] is True
        assert result.data["next_action"] == "call control_viewer(action='context')"
        hub.unregister(client)

    async def test_stdio_sessions_say_why_there_is_no_viewer(self, home, tmp_path):
        from ifc_console.app import AppCore
        from ifc_console.application.operations import build_operations
        from ifc_console.settings import SettingsStore

        store = SettingsStore(home=home, project_dir=tmp_path, env={})
        core = AppCore(store, transport="stdio")
        build_operations(core)
        result = await core.operation_service.call("open_viewer", {})
        assert result.ok is False
        assert result.error.code == "VIEWER_UNAVAILABLE"
        assert "serve --http" in result.error.hint


class TestControlViewerTool:
    async def _register(self, core):
        from ifc_console.application.operations import (
            build_operations,
            register_viewer_operations,
        )

        build_operations(core)
        core.viewer.enabled = True
        register_viewer_operations(core)

    async def test_the_tool_translates_intent_into_a_viewer_command(self, core):
        await self._register(core)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True,
                 "result": {"axes": {}, "slice": 0.3}},
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        result = await core.operation_service.call(
            "control_viewer",
            {"action": "section", "section": {"z": {"at": 1.2, "keep": "below"}},
             "slice_depth": 0.3},
        )
        assert result.ok is True
        sent = ws.sent[-1]
        assert sent["action"] == "set-section"
        assert sent["axes"] == {"z": {"at": 1.2, "keep": "below"}}
        assert sent["slice"] == 0.3

    async def test_point_count_chooses_the_measurement(self, core):
        await self._register(core)

        async def reply(frame):
            await hub.handle_frame(
                client, {"type": "command_result", "id": frame["id"], "ok": True, "result": {}}
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        cases = {
            2: "measure-points",
            3: "measure-angle",
            5: "measure-area",
        }
        for count, expected in cases.items():
            await core.operation_service.call(
                "control_viewer",
                {"action": "measure_points",
                 "points": [[float(i), 0.0, 0.0] for i in range(count)]},
            )
            assert ws.sent[-1]["action"] == expected, count

    async def test_select_and_hide_close_the_ui_parity_gap(self, core):
        """Whatever the person can do in the viewer, the agent can too."""
        await self._register(core)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True, "result": {}},
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        selected = await core.operation_service.call(
            "control_viewer",
            {"action": "select", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"]},
        )
        assert selected.ok is True
        assert ws.sent[-1]["action"] == "set-selection"
        assert ws.sent[-1]["guids"] == ["2O2Fr$t4X7Zf8NOew3FL9r"]
        assert ws.sent[-1]["additive"] is False

        refused = await core.operation_service.call("control_viewer", {"action": "select"})
        assert refused.ok is False
        assert refused.error.code == "INVALID_INPUT"

        hidden = await core.operation_service.call(
            "control_viewer",
            {"action": "hide", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"]},
        )
        assert hidden.ok is True
        assert ws.sent[-1]["action"] == "hide"

    async def test_focus_carries_the_ids_without_creating_a_tab(self, core):
        await self._register(core)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True,
                 "result": {"focused": 1}},
            )

        hub, client, ws = await _connected(core)
        ws.reply = reply
        result = await core.operation_service.call(
            "control_viewer",
            {"action": "focus", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"], "name": "Pile 07"},
        )
        assert result.ok is True
        sent = ws.sent[-1]
        assert sent["action"] == "focus"
        assert sent["guids"] == ["2O2Fr$t4X7Zf8NOew3FL9r"]
        assert "name" not in sent
        assert result.data["result"] == {"focused": 1}
        await core.operation_service.call("control_viewer", {"action": "unfocus"})
        assert ws.sent[-1]["action"] == "unfocus"
        assert "name" not in ws.sent[-1]

    async def test_set_camera_carries_the_whole_camera_in_model_axes(self, core):
        await self._register(core)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True,
                 "result": {"position": [1.0, 2.0, 3.0]}},
            )

        hub, client, ws = await _connected(core, reply)
        result = await core.operation_service.call(
            "control_viewer",
            {
                "action": "set_camera",
                "camera": {
                    "position": [10.0, -20.0, 5.0],
                    "target": [0.0, 0.0, 1.5],
                    "fov": 35,
                    "projection": "perspective",
                    "transition": False,
                },
            },
        )
        assert result.ok is True
        sent = ws.sent[-1]
        assert sent["action"] == "set-camera"
        assert sent["position"] == [10.0, -20.0, 5.0]
        assert sent["target"] == [0.0, 0.0, 1.5]
        assert sent["fov"] == 35.0
        assert sent["projection"] == "perspective"
        assert sent["transition"] is False

    @pytest.mark.parametrize(
        "camera",
        [
            None,
            {},
            {"position": [1, 2]},
            {"position": [1, 2, "x"]},
            {"position": [1, 2, 3], "target": [1, 2, 3]},
            {"fov": 400},
            {"projection": "isometric"},
            {"postion": [1, 2, 3]},
            {"transition": True},
        ],
    )
    async def test_a_bad_camera_is_refused_in_the_tools_own_words(self, core, camera):
        await self._register(core)
        hub, client, ws = await _connected(core)
        payload = {"action": "set_camera"}
        if camera is not None:
            payload["camera"] = camera
        result = await core.operation_service.call("control_viewer", payload)
        assert result.ok is False
        assert result.error.code == "INVALID_INPUT"
        assert not [frame for frame in ws.sent if frame.get("type") == "command"]

    async def test_fit_frames_ids_a_selection_or_the_whole_model(self, core):
        await self._register(core)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True,
                 "result": {"framed": 1, "missing": [], "camera": {}}},
            )

        hub, client, ws = await _connected(core, reply)
        await core.operation_service.call(
            "control_viewer",
            {"action": "fit", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"], "padding": 1.5},
        )
        assert ws.sent[-1]["action"] == "fit"
        assert ws.sent[-1]["guids"] == ["2O2Fr$t4X7Zf8NOew3FL9r"]
        assert ws.sent[-1]["padding"] == 1.5

        await core.operation_service.call(
            "control_viewer", {"action": "fit", "selection": True, "view": "bottom"}
        )
        assert ws.sent[-1]["selection"] is True
        assert ws.sent[-1]["view"] == "bottom"

        # nothing named: zoom back out, which had no command at all before
        await core.operation_service.call("control_viewer", {"action": "fit"})
        assert ws.sent[-1]["action"] == "fit"
        assert "guids" not in ws.sent[-1] and "selection" not in ws.sent[-1]

    async def test_a_missing_argument_is_refused_before_the_tab_hears_about_it(self, core):
        await self._register(core)
        hub, client, ws = await _connected(core)
        result = await core.operation_service.call("control_viewer", {"action": "save_view"})
        assert result.ok is False
        assert result.error.code == "INVALID_INPUT"
        assert not [frame for frame in ws.sent if frame.get("type") == "command"]


class TestSelectorScopedVisibility:
    """'isolate the walls' has to be one call, not query-then-paste-500-ids."""

    async def _ready(self, core, work_model):
        from ifc_console.application.operations import (
            build_operations,
            register_viewer_operations,
        )

        build_operations(core)
        core.viewer.enabled = True
        register_viewer_operations(core)
        await core.open_model(work_model)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True, "result": {}},
            )

        hub, client, ws = await _connected(core, reply)
        return hub, client, ws

    async def test_a_selector_becomes_the_ids_the_viewer_needs(self, core, work_model):
        hub, client, ws = await self._ready(core, work_model)
        result = await core.operation_service.call(
            "control_viewer", {"action": "isolate", "selector": "IfcWall"}
        )
        assert result.ok is True
        assert result.data["resolved"] == 3
        assert result.data["selector"] == "IfcWall"
        sent = ws.sent[-1]
        assert sent["action"] == "isolate"
        assert sorted(sent["guids"]) == sorted(
            w.GlobalId for w in core.session.ifc.by_type("IfcWall")
        )

    async def test_the_same_selector_works_for_select_hide_and_fit(self, core, work_model):
        hub, client, ws = await self._ready(core, work_model)
        for action, command in (("select", "set-selection"), ("hide", "hide"), ("fit", "fit")):
            result = await core.operation_service.call(
                "control_viewer", {"action": action, "selector": "IfcWall"}
            )
            assert result.ok is True, action
            assert ws.sent[-1]["action"] == command
            assert len(ws.sent[-1]["guids"]) == 3

    async def test_a_selector_matching_nothing_says_so_instead_of_clearing_the_view(
        self, core, work_model
    ):
        hub, client, ws = await self._ready(core, work_model)
        result = await core.operation_service.call(
            "control_viewer", {"action": "isolate", "selector": "IfcFurniture"}
        )
        assert result.ok is False
        assert result.error.code == "NO_MATCH"
        assert not [frame for frame in ws.sent if frame.get("type") == "command"]

    async def test_a_selector_where_it_cannot_apply_is_refused(self, core, work_model):
        hub, client, ws = await self._ready(core, work_model)
        wrong_action = await core.operation_service.call(
            "control_viewer", {"action": "show_all", "selector": "IfcWall"}
        )
        assert wrong_action.ok is False
        assert wrong_action.error.code == "INVALID_INPUT"

        both = await core.operation_service.call(
            "control_viewer",
            {"action": "isolate", "selector": "IfcWall", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"]},
        )
        assert both.ok is False
        assert both.error.code == "INVALID_INPUT"
        assert not [frame for frame in ws.sent if frame.get("type") == "command"]

    async def test_a_broken_selector_is_a_query_error_not_a_viewer_error(
        self, core, work_model
    ):
        hub, client, ws = await self._ready(core, work_model)
        result = await core.operation_service.call(
            "control_viewer", {"action": "isolate", "selector": "IfcWall, ,, ="}
        )
        assert result.ok is False
        assert result.error.code in ("INVALID_QUERY", "NO_MATCH")


class TestSceneState:
    """A tab rebuilding its scene has disposed its geometry, so anything sent
    to it would come back as 'none of those elements are in this model'."""

    async def test_a_command_during_a_rebuild_is_busy_not_a_bad_guid(
        self, core, monkeypatch
    ):
        monkeypatch.setattr("ifc_console.viewer.hub._REBUILD_WAIT", 0.1)
        await _register_viewer_tools(core)
        hub, client, ws = await _connected(core)
        await hub.handle_frame(client, {"type": "scene_state", "state": "rebuilding"})

        result = await core.operation_service.call(
            "control_viewer", {"action": "select", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"]}
        )

        assert result.ok is False
        assert result.error.code == "VIEWER_BUSY"
        assert "retry" in result.error.hint.lower()
        assert not [frame for frame in ws.sent if frame.get("type") == "command"]

    async def test_the_command_runs_once_the_tab_reports_a_scene_again(self, core):
        await _register_viewer_tools(core)

        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": True, "result": {}},
            )

        hub, client, ws = await _connected(core, reply)
        await hub.handle_frame(client, {"type": "scene_state", "state": "rebuilding"})
        await hub.handle_frame(client, {"type": "scene_state", "state": "ready"})

        result = await core.operation_service.call(
            "control_viewer", {"action": "select", "guids": ["2O2Fr$t4X7Zf8NOew3FL9r"]}
        )
        assert result.ok is True
        assert ws.sent[-1]["action"] == "set-selection"

    async def test_the_tabs_own_busy_refusal_is_not_reported_as_a_bad_argument(self, core):
        async def reply(frame):
            await hub.handle_frame(
                client,
                {"type": "command_result", "id": frame["id"], "ok": False,
                 "error": "VIEWER_BUSY_REBUILDING"},
            )

        hub, client, ws = await _connected(core, reply)
        with pytest.raises(ToolError) as caught:
            await hub.run_command("set-selection", {"guids": ["g-1"]})
        assert caught.value.code == "VIEWER_BUSY"

    async def test_a_tab_that_closes_mid_command_fails_it_at_once(self, core):
        hub, client, ws = await _connected(core)  # never answers
        request = asyncio.create_task(hub.run_command("get-context"))
        await asyncio.sleep(0)
        assert [frame for frame in ws.sent if frame.get("type") == "command"]

        hub.unregister(client)

        with pytest.raises(ToolError) as caught:
            await request
        assert caught.value.code == "VIEWER_NOT_CONNECTED"
        assert "reopen the viewer" in caught.value.hint
