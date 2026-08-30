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
    "horizontal": 0.16,
    "vertical": 0.12,
    "slope_percent": 75.0,
    "slope_angle": 36.869898,
}


DIMENSIONS = {
    "kind": "dimensions",
    "guid": "3vB2YO$MX4xv5uCqZZG05x",
    "length": 4.0,
    "width": 3.0,
    "thickness": 0.3,
    "area": 33.84,
    "volume": 3.6,
    "method": "oriented bounding box",
    "centre": {"x": 2.5, "y": 0.12, "z": 1.5},
}
LASER = {
    "kind": "laser",
    "method": "element bounding boxes",
    "origin": [2.5, 0.12, 1.5],
    "axes": {
        "x": {"span": 1.0, "negative": {"distance": 0.5, "guid": "a"},
              "positive": {"distance": 0.5, "guid": "b"}},
        "y": {"span": None},
        "z": {"span": None},
    },
}
ANGLE = {"kind": "angle", "degrees": 90.0, "at": [0.0, 0.0, 0.0], "legs": [1.0, 1.0]}
AREA = {
    "kind": "area",
    "area": 3.0,
    "perimeter": 8.0,
    "flatness": 0.0,
    "centre": {"x": 1.5, "y": 0.5, "z": 0.0},
    "points": [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [3.0, 1.0, 0.0], [0.0, 1.0, 0.0]],
}
PATH = {
    "kind": "path",
    "distance": 7.0,
    "points": [[0.0, 0.0, 0.0], [3.0, 0.0, 0.0], [3.0, 4.0, 0.0]],
    "segments": [3.0, 4.0],
}


class TestCleaning:
    def test_valid_items_survive_rounding(self):
        cleaned = _clean_measurements([MEASUREMENT])
        assert cleaned == [MEASUREMENT]

    def test_every_kind_the_viewer_can_measure_arrives(self):
        """A size measured in the viewer used to be sent and then dropped
        here, so the assistant that asked for it was told nothing had been
        measured."""
        cleaned = _clean_measurements([DIMENSIONS, LASER, ANGLE, AREA, PATH])
        assert [item["kind"] for item in cleaned] == [
            "dimensions",
            "laser",
            "angle",
            "area",
            "path",
        ]
        assert cleaned[0]["thickness"] == 0.3
        assert cleaned[0]["volume"] == 3.6
        assert cleaned[1]["axes"]["x"]["span"] == 1.0
        assert cleaned[1]["axes"]["x"]["negative"]["guid"] == "a"
        assert cleaned[2]["degrees"] == 90.0
        assert cleaned[3]["points"][2] == [3.0, 1.0, 0.0]
        assert cleaned[3]["centre"] == {"x": 1.5, "y": 0.5, "z": 0.0}
        assert cleaned[4]["distance"] == 7.0
        assert cleaned[4]["segments"] == [3.0, 4.0]

    def test_path_requires_finite_total_and_ordered_points(self):
        cleaned = _clean_measurements(
            [
                {**PATH, "distance": float("inf")},
                {**PATH, "points": [[0, 0, 0]]},
                {**PATH, "points": [[0, 0, 0], [float("nan"), 1, 0]]},
            ]
        )
        assert cleaned == []

    def test_paths_and_areas_over_point_limit_are_rejected(self):
        points = [[index, 0, 0] for index in range(250)]
        assert _clean_measurements([{**PATH, "points": points, "segments": [1.0] * 249}]) == []
        assert _clean_measurements([{**AREA, "points": points}]) == []

    def test_only_aligned_finite_path_segments_survive(self):
        misaligned = _clean_measurements([{**PATH, "segments": [7.0]}])[0]
        assert "segments" not in misaligned
        bad_segment = _clean_measurements([{**PATH, "segments": [3.0, float("nan")]}])[0]
        assert "segments" not in bad_segment

    def test_area_rejects_an_invalid_interior_point(self):
        points = [*AREA["points"]]
        points[2] = [float("inf"), 1.0, 0.0]
        assert _clean_measurements([{**AREA, "points": points}]) == []

    def test_area_centre_accepts_current_and_legacy_wire_shapes(self):
        named = _clean_measurements([AREA])[0]
        assert named["centre"] == {"x": 1.5, "y": 0.5, "z": 0.0}
        legacy = _clean_measurements([{**AREA, "centre": [1.5, 0.5, 0.0]}])[0]
        assert legacy["centre"] == [1.5, 0.5, 0.0]

    def test_an_untagged_item_is_still_a_distance(self):
        """The only shape there used to be."""
        assert _clean_measurements([MEASUREMENT])[0]["distance"] == 0.2

    def test_a_kind_missing_its_own_fields_drops(self):
        cleaned = _clean_measurements(
            [
                {"kind": "angle", "at": [0, 0, 0]},           # no degrees
                {"kind": "area", "area": 2.0, "points": [[0, 0, 0]]},  # not a polygon
                {"kind": "laser", "axes": "sideways"},
                {"kind": "dimensions", "guid": "x"},          # no numbers at all
                ANGLE,
            ]
        )
        assert [item["kind"] for item in cleaned] == ["angle"]

    def test_snapped_ends_travel_with_a_distance(self):
        cleaned = _clean_measurements([{**MEASUREMENT, "ends": ["corner", "surface"]}])
        assert cleaned[0]["ends"] == ["corner", "surface"]

    def test_anchors_and_label_travel_with_any_kind(self):
        """Anchors name the measured elements; without them a recorded
        measurement cannot be tied back to anything."""
        anchors = [
            {"guid": "3vB2YO$MX4xv5uCqZZG05x", "world": [0.0, 0.0, 0.0]},
            {"guid": None, "world": [0.2, 0.0, 0.0]},
        ]
        distance = _clean_measurements(
            [{**MEASUREMENT, "anchors": anchors, "label": " span "}]
        )[0]
        assert distance["anchors"] == [
            {"guid": "3vB2YO$MX4xv5uCqZZG05x", "world": [0.0, 0.0, 0.0]},
            {"world": [0.2, 0.0, 0.0]},
        ]
        assert distance["label"] == "span"
        laser = _clean_measurements([{**LASER, "anchors": anchors[:1]}])[0]
        assert laser["anchors"][0]["guid"] == "3vB2YO$MX4xv5uCqZZG05x"

    def test_bad_anchors_drop_without_taking_the_measurement(self):
        cleaned = _clean_measurements(
            [
                {
                    **MEASUREMENT,
                    "anchors": [
                        "nonsense",
                        {"guid": "", "world": [1, 2]},
                        {"guid": "a" * 200, "world": None},
                        {"guid": "ok", "world": [1.0, 2.0, 3.0]},
                    ],
                    "label": 7,
                }
            ]
        )[0]
        assert cleaned["anchors"] == [{"guid": "ok", "world": [1.0, 2.0, 3.0]}]
        assert "label" not in cleaned

    def test_anchor_and_label_caps(self):
        many = [{"guid": f"g{i}", "world": [0.0, 0.0, 0.0]} for i in range(30)]
        cleaned = _clean_measurements(
            [{**MEASUREMENT, "anchors": many, "label": "x" * 300}]
        )[0]
        assert len(cleaned["anchors"]) == 16
        assert len(cleaned["label"]) == 120

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
