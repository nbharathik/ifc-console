"""The recorded-skill builder: intents from viewer measurements."""

from __future__ import annotations

from ifc_console_agents.recording import build_recorded_skill, measured_guids
from ifc_console_agents.skills import parse_measurement_spec

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
        anchors = [
            {
                "guid": WALL,
                "world": [0, 0, 0],
                "local": [0.25, 0.0, 0.0],
                "reach": 4.1,
            },
            {
                "guid": SLAB,
                "world": [1, 0, 0],
                "local": [-0.25, 0.0, 0.0],
                "reach": 6.2,
            },
        ]
        skill = _build([_distance(1.0, anchors)])
        assert "clear distance between" in skill.content
        assert "clear_distance" in skill.intents
        spec = parse_measurement_spec(skill.content, required=True)
        assert spec is not None
        rule = spec.measurements[0]
        assert rule.rule_type == "relationship"
        assert rule.unresolved is True
        assert [role.role for role in rule.intent.object_roles] == ["from", "to"]
        assert [role.global_id for role in rule.intent.object_roles] == [WALL, SLAB]
        assert "ordered_object_roles" in rule.intent.matched_by
        assert rule.intent.object_roles[0].local_point == (0.25, 0.0, 0.0)
        assert rule.intent.object_roles[1].reach_si == 6.2
        assert all(
            "world" not in role.model_dump(exclude_none=True) for role in rule.intent.object_roles
        )

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
            "elements": [{"global_id": WALL, "class": "IfcWall", "name": "A|B", "dimensions": {}}]
        }
        skill = _build([_distance(1.0, [{"guid": WALL, "world": [0, 0, 0]}])], analysis=analysis)
        assert "A\\|B" in skill.content

    def test_v2_recording_keeps_stable_id_local_intent_signature_and_revision(self):
        signature = {
            "version": "1.0",
            "class_family": "linear_member",
            "ifc_class": "IfcMember",
            "type_key": "ifcmembertype:ipe200",
            "geometry_family": "constant_profile_extrusion",
            "profile_family": "i_shape",
            "normalized_extents": [1.0, 0.1, 0.05],
            "fingerprint": "sha256:example",
        }
        analysis = {
            "analysis_version": "2.0",
            "model_revision": {
                "model_id": "viewer-model",
                "fingerprint": "model-fingerprint",
                "revision": 7,
            },
            "elements": [
                {
                    "object": {
                        "global_id": WALL,
                        "class": "IfcMember",
                        "name": "M-01",
                        "type": {"class": "IfcMemberType", "name": "IPE200"},
                    },
                    "geometry_family": "constant_profile_extrusion",
                    "geometry_signature": signature,
                    "frames": {
                        "semantic": {
                            "longitudinal": [1.0, 0.0, 0.0],
                            "transverse": [0.0, 1.0, 0.0],
                            "vertical": [0.0, 0.0, 1.0],
                        }
                    },
                    "tolerance": {"absolute_si": 0.00001, "relative": 0.01},
                    "measurements": [
                        {
                            "id": "profile.web_thickness",
                            "value_si": 0.012,
                            "source": "profile_parameter",
                            "frame": "semantic",
                            "direction": "transverse",
                            "confidence": "high",
                        },
                        {
                            "id": "envelope.overall_height",
                            "value_si": 0.012,
                            "source": "mesh_extent",
                            "frame": "semantic",
                            "direction": "vertical",
                            "confidence": "high",
                        },
                    ],
                }
            ],
        }
        item = _distance(
            0.012,
            [
                {"guid": WALL, "local": [0.0, 0.0, 0.0], "kind": "face"},
                {"guid": WALL, "local": [0.0, 0.012, 0.0], "kind": "face"},
            ],
            delta=[0.0, 0.012, 0.0],
            label="web thickness",
        )
        skill = _build([item], analysis=analysis)
        parsed = parse_measurement_spec(skill.content, required=True)
        assert parsed is not None
        assert parsed.measurement_ids == ("profile.web_thickness",)
        assert parsed.exemplar.model_revision.model_id == "viewer-model"
        assert parsed.exemplar.model_revision.revision == 7
        assert parsed.exemplar.objects[0].geometry_signature == signature
        rule = parsed.measurements[0]
        assert rule.intent.semantic_direction == "transverse"
        assert rule.intent.local_direction == (0.0, 1.0, 0.0)
        assert rule.intent.snap_kinds == ("face",)
        assert "world" not in rule.intent.model_dump(exclude_none=True)
        assert skill.unresolved_intents == ()

    def test_scale_aware_matching_does_not_use_the_old_five_millimetre_floor(self):
        analysis = {
            "elements": [
                {
                    "global_id": WALL,
                    "class": "IfcMember",
                    "box": {"local_extents": {"x": 1.0, "y": 0.1, "z": 0.1}},
                    "measurements": [
                        {
                            "id": "profile.web_thickness",
                            "value_si": 0.01,
                            "source": "profile_parameter",
                            "confidence": "high",
                        }
                    ],
                }
            ]
        }
        # A 4 mm gap used to pass solely because of the global 5 mm floor. At
        # this object scale it is far outside the derived absolute + relative bound.
        skill = _build(
            [_distance(0.014, [{"guid": WALL, "local": [0.0, 0.0, 0.0]}])],
            analysis=analysis,
        )
        assert skill.spec.executable is False
        assert skill.spec.measurements[0].unresolved is True
        assert skill.spec.measurements[0].output is None
