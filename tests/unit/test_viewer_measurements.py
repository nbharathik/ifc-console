"""User measurements crossing the websocket and the tool that reads them."""

from __future__ import annotations

from ifc_console.viewer.hub import _clean_measurements


class FakeWs:
    async def send_text(self, _text: str) -> None:
        pass


MEASUREMENT = {
    "from": [0.0, 0.0, 0.0],
    "to": [0.2, 0.0, 0.0],
    "distance": 0.2,
    "delta": [0.2, 0.0, 0.0],
}


class TestCleaning:
    def test_valid_items_survive_rounding(self):
        cleaned = _clean_measurements([MEASUREMENT])
        assert cleaned == [MEASUREMENT]

    def test_garbage_is_dropped_not_fatal(self):
        cleaned = _clean_measurements(
            [
                MEASUREMENT,
                {"from": [0, 0], "to": [1, 1, 1], "distance": 1},
                {"from": "x", "to": [1, 1, 1], "distance": 1},
                "nonsense",
                {"from": [0, 0, 0], "to": [1, 0, 0], "distance": "far"},
            ]
        )
        assert len(cleaned) == 1

    def test_not_a_list_is_empty(self):
        assert _clean_measurements({"items": []}) == []

    def test_capped_at_one_hundred(self):
        cleaned = _clean_measurements([MEASUREMENT] * 150)
        assert len(cleaned) == 100


class TestHubAndTool:
    async def test_frame_lands_and_the_tool_reads_it_back(self, core):
        from ifc_console.application.operations import build_operations, register_viewer_operations

        build_operations(core)
        core.viewer.enabled = True
        register_viewer_operations(core)

        hub = core.viewer_hub
        client = hub.register(FakeWs())
        await hub.handle_frame(
            client, {"type": "measurements", "items": [MEASUREMENT, {"bad": True}]}
        )

        result = await core.operation_service.call("get_viewer_measurements", {})
        assert result.ok is True
        assert result.data["measurements"] == [MEASUREMENT]
        assert result.data["measured_at"]
        assert "metres" in result.data["units"]

        await hub.handle_frame(client, {"type": "measurements", "items": []})
        cleared = await core.operation_service.call("get_viewer_measurements", {})
        assert cleared.data["measurements"] == []
        assert "press M" in cleared.data["note"]

    async def test_no_tab_is_the_usual_clear_error(self, core):
        from ifc_console.application.operations import build_operations, register_viewer_operations

        build_operations(core)
        core.viewer.enabled = True
        register_viewer_operations(core)
        result = await core.operation_service.call("get_viewer_measurements", {})
        assert result.ok is False
        assert result.error.code == "VIEWER_NOT_CONNECTED"

    async def test_newest_tab_wins(self, core):
        from ifc_console.application.operations import build_operations, register_viewer_operations

        build_operations(core)
        core.viewer.enabled = True
        register_viewer_operations(core)
        hub = core.viewer_hub
        first = hub.register(FakeWs())
        second = hub.register(FakeWs())
        await hub.handle_frame(first, {"type": "measurements", "items": [MEASUREMENT]})
        newer = {**MEASUREMENT, "distance": 0.4, "to": [0.4, 0.0, 0.0]}
        await hub.handle_frame(second, {"type": "measurements", "items": [newer]})
        items, _ = hub.latest_measurements()
        assert items[0]["distance"] == 0.4
