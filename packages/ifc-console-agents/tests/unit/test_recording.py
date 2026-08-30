"""The recorded-skill builder: intents from viewer measurements."""

from __future__ import annotations

from ifc_console_agents.recording import build_recorded_skill, measured_guids

WALL = "2O2Fr$t4X7Zf8NOew3FL9r"
SLAB = "1xS3BCk291UvhgP2dvNsgp"

ANALYSIS = {
    "elements": [
        {
            "global_id": WALL,
            "class": "IfcWall",
            "name": "W-01",
            "type": {"class": "IfcWallType", "name": "Basic 300"},
            "dimensions": {
                "length": {"si": 4.0, "file": 4000.0, "source": "extrusion_depth"},
                "wall_thickness": {"si": 0.3, "file": 300.0, "source": "profile_curve"},
                "height": {"si": 2.7, "file": 2700.0, "source": "mesh_section"},
            },
        }
    ]
}


def _distance(value: float, anchors: list, **extra) -> dict:
    return {
        "kind": "distance",
        "from": [0.0, 0.0, 0.0],
        "to": [value, 0.0, 0.0],
        "distance": value,
        "anchors": anchors,
        **extra,
    }


def _build(items: list[dict], analysis: dict | None = ANALYSIS, notes: str = ""):
    return build_recorded_skill(
        items=items,
        measured_at="2026-08-29T10:00:00Z",
        model_name="office.ifc",
        analysis=analysis,
        notes=notes,
    )


class TestGuidCollection:
    def test_guids_come_from_anchors_dimensions_and_laser_hits(self):
        items = [
            _distance(0.3, [{"guid": WALL, "world": [0, 0, 0]}, {"world": [0.3, 0, 0]}]),
            {"kind": "dimensions", "guid": SLAB, "length": 4.0},
            {
                "kind": "laser",
                "axes": {"x": {"span": 1.0, "negative": {"distance": 0.5, "guid": WALL}}},
            },
        ]
        assert measured_guids(items) == [WALL, SLAB]

    def test_no_identity_is_no_guids(self):
        assert measured_guids([_distance(1.0, [])]) == []


class TestSkillContent:
    def test_a_matched_value_is_named_after_the_dimension(self):
        skill = _build([_distance(0.301, [{"guid": WALL, "world": [0, 0, 0]}])])
        assert "equals the element's wall_thickness (profile_curve)" in skill.content
        assert "IfcWall 'W-01'" in skill.content
        assert "type 'Basic 300'" in skill.content
        assert skill.intents == ("wall_thickness",)
        assert skill.applies_to == "IfcWall"
        assert "IfcWall" in skill.description

    def test_two_elements_read_as_a_clearance(self):
        anchors = [{"guid": WALL, "world": [0, 0, 0]}, {"guid": SLAB, "world": [1, 0, 0]}]
        skill = _build([_distance(1.0, anchors)])
        assert "clear distance between" in skill.content
        assert "clear_distance" in skill.intents

    def test_axis_label_and_notes_travel(self):
        item = _distance(
            3.0,
            [{"guid": WALL, "world": [0, 0, 0]}],
            axis="z",
            ends=["corner", "corner"],
            label="storey height",
        )
        skill = _build([item], notes="Use the top face, not the parapet.")
        assert "along model Z" in skill.content
        assert "corner to corner snap" in skill.content
        assert "storey height;" in skill.content
        assert "Use the top face, not the parapet." in skill.content

    def test_dominant_delta_names_the_axis_without_a_lock(self):
        item = _distance(3.0, [], delta=[0.0, 0.0, 2.995])
        skill = _build([item], analysis=None)
        assert "along model Z" in skill.content

    def test_without_analysis_the_pattern_still_records(self):
        skill = _build([_distance(1.234, [{"guid": WALL, "world": [0, 0, 0]}])], analysis=None)
        assert WALL in skill.content
        assert "| 1 | distance | 1.234 m |" in skill.content
        assert "analyze_element_geometry" in skill.content
        assert "measure__propose_measured_value" in skill.content
        assert skill.applies_to is None

    def test_every_measure_kind_renders_a_row(self):
        items = [
            _distance(0.3, []),
            {"kind": "dimensions", "guid": WALL, "length": 4.0, "width": 0.3, "thickness": 2.7},
            {"kind": "area", "area": 12.0, "perimeter": 14.0, "points": [[0, 0, 0]] * 3},
            {"kind": "path", "distance": 7.0, "points": [[0, 0, 0]] * 3},
            {"kind": "angle", "degrees": 90.0, "at": [0, 0, 0]},
            {"kind": "laser", "axes": {"x": {"span": 1.0}}},
        ]
        skill = _build(items)
        for kind in ("distance", "dimensions", "area", "path", "angle", "laser"):
            assert f"| {kind} |" in skill.content

    def test_rows_are_capped_but_counted(self):
        skill = _build([_distance(1.0, []) for _ in range(45)], analysis=None)
        assert "| 40 |" in skill.content
        assert "| 41 |" not in skill.content
        assert "(5 further measurements not listed.)" in skill.content

    def test_pipes_in_names_cannot_break_the_table(self):
        analysis = {
            "elements": [
                {"global_id": WALL, "class": "IfcWall", "name": "A|B", "dimensions": {}}
            ]
        }
        skill = _build(
            [_distance(1.0, [{"guid": WALL, "world": [0, 0, 0]}])], analysis=analysis
        )
        assert "A\\|B" in skill.content
