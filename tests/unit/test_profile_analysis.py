"""Cross-section math and the full element probe, on known geometry."""

from __future__ import annotations

import json

import numpy as np
import pytest

from ifc_console.ifc import geometry, section
from ifc_console.ifc.profile import analyze_elements

MM = 0.001

# a national-grid easting/northing: the coordinates a georeferenced file
# actually carries, where the tetrahedron sum used to cancel to noise
GRID_OFFSET = np.array([2_700_000.0, 1_200_000.0, 120.0])


def _box_mesh(dx: float, dy: float, dz: float):
    """A closed cuboid centred on the origin."""
    x, y, z = dx / 2, dy / 2, dz / 2
    verts = np.array(
        [
            [-x, -y, -z], [x, -y, -z], [x, y, -z], [-x, y, -z],
            [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z],
        ]
    )
    faces = np.array(
        [
            [0, 2, 1], [0, 3, 2],
            [4, 5, 6], [4, 6, 7],
            [0, 1, 5], [0, 5, 4],
            [2, 3, 7], [2, 7, 6],
            [1, 2, 6], [1, 6, 5],
            [3, 0, 4], [3, 4, 7],
        ]
    )
    return verts, faces


class TestMeshVolume:
    def test_a_pane_keeps_its_volume_on_a_national_grid(self):
        """Absolute coordinates used to report this one 93 percent high."""
        verts, faces = _box_mesh(1.5, 0.012, 2.1)
        exact = 1.5 * 0.012 * 2.1
        assert geometry.mesh_volume(verts, faces) == pytest.approx(exact, rel=1e-12)
        # what is left is the offset coordinates' own float64 resolution
        assert geometry.mesh_volume(verts + GRID_OFFSET, faces) == pytest.approx(exact, rel=1e-6)

    def test_a_slender_bar_is_not_inflated_far_from_the_origin(self):
        verts, faces = _box_mesh(0.016, 0.016, 6.0)
        got = geometry.mesh_volume(verts + GRID_OFFSET, faces)
        assert got == pytest.approx(0.016 * 0.016 * 6.0, rel=1e-6)


class TestSurfaceAreas:
    def test_the_buckets_partition_a_box(self):
        verts, faces = _box_mesh(4.0, 0.2, 3.0)
        areas = geometry.surface_areas(verts, faces)
        assert areas["side_area_y"] == pytest.approx(2 * 4.0 * 3.0)
        assert areas["side_area_x"] == pytest.approx(2 * 0.2 * 3.0)
        assert areas["top_area"] == pytest.approx(4.0 * 0.2)
        assert areas["bottom_area"] == pytest.approx(areas["top_area"])
        buckets = sum(v for name, v in areas.items() if name != "surface_area")
        assert areas["surface_area"] == pytest.approx(buckets)

    def test_the_split_follows_the_element_frame(self):
        """A wall on a skew grid keeps its faces under the local axis."""
        verts, faces = _box_mesh(4.0, 0.2, 3.0)
        c, s = np.cos(np.radians(60.0)), np.sin(np.radians(60.0))
        rotation = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
        world = verts @ rotation.T
        # turned past 45 degrees the world split files those faces under x
        assert geometry.surface_areas(world, faces)["side_area_x"] == pytest.approx(24.0)
        areas = geometry.surface_areas(world, faces, rotation)
        assert areas["side_area_y"] == pytest.approx(24.0)
        assert areas["top_area"] == pytest.approx(4.0 * 0.2)


class TestSectionMetrics:
    def test_a_thin_plate_measures_its_own_thickness(self):
        verts, faces = _box_mesh(1.0, 0.01, 2.0)
        cut = section.section_metrics(
            verts, faces, origin=np.zeros(3), normal=np.array([0.0, 0.0, 1.0])
        )
        assert cut is not None
        assert cut["closed"] is True
        assert cut["width"] == pytest.approx(1.0, abs=1e-6)
        assert cut["height"] == pytest.approx(0.01, abs=1e-6)
        assert cut["perimeter"] == pytest.approx(2.02, abs=1e-6)
        assert cut["area"] == pytest.approx(0.01, rel=1e-6)
        assert cut["thickness"]["median"] == pytest.approx(0.01, rel=1e-6)

    def test_a_plane_that_misses_returns_none(self):
        verts, faces = _box_mesh(1.0, 1.0, 1.0)
        cut = section.section_metrics(
            verts, faces, origin=np.array([0.0, 0.0, 5.0]), normal=np.array([0.0, 0.0, 1.0])
        )
        assert cut is None

    def test_the_outline_is_returned_only_on_request(self):
        verts, faces = _box_mesh(1.0, 0.5, 1.0)
        origin = np.zeros(3)
        normal = np.array([0.0, 0.0, 1.0])
        assert "outline" not in section.section_metrics(verts, faces, origin, normal)
        cut = section.section_metrics(verts, faces, origin, normal, include_outline=True)
        assert len(cut["outline"]) == 1
        assert len(cut["outline"][0]) >= 4


def _project(ifc):
    import ifcopenshell.api.context
    import ifcopenshell.api.root
    import ifcopenshell.api.unit

    ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcProject", name="Piles")
    length = ifcopenshell.api.unit.add_si_unit(ifc, unit_type="LENGTHUNIT", prefix="MILLI")
    ifcopenshell.api.unit.assign_unit(ifc, units=[length])
    model = ifcopenshell.api.context.add_context(ifc, context_type="Model")
    return ifcopenshell.api.context.add_context(
        ifc,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model,
    )


def _extrude(ifc, body, element, profile, depth_mm: float):
    import ifcopenshell.api.geometry

    direction = ifc.createIfcDirection((0.0, 0.0, 1.0))
    solid = ifc.createIfcExtrudedAreaSolid(profile, None, direction, depth_mm)
    representation = ifc.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=element, representation=representation
    )
    ifcopenshell.api.geometry.edit_object_placement(ifc, product=element)


@pytest.fixture
def pile_model():
    """Two members in a millimetre file: a thin plate and a hollow box."""
    import ifcopenshell.api.project
    import ifcopenshell.api.root

    ifc = ifcopenshell.api.project.create_file(version="IFC4")
    body = _project(ifc)

    plate = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcMember", name="Sheet-1")
    points = [(0.0, 0.0), (500.0, 0.0), (500.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
    polyline = ifc.createIfcPolyline(
        [ifc.createIfcCartesianPoint(point) for point in points]
    )
    profile = ifc.createIfcArbitraryClosedProfileDef("AREA", "SP-500x10", polyline)
    _extrude(ifc, body, plate, profile, 6000.0)

    box = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcColumn", name="Box-1")
    hollow = ifc.createIfcRectangleHollowProfileDef("AREA", "RHS-400x300x12", None, 400.0, 300.0, 12.0, None, None)
    _extrude(ifc, body, box, hollow, 3000.0)
    return ifc, plate, box


def _profile_model(profile_builder, *, tapered: bool = False):
    import ifcopenshell.api.geometry
    import ifcopenshell.api.project
    import ifcopenshell.api.root

    ifc = ifcopenshell.api.project.create_file(version="IFC4")
    body = _project(ifc)
    element = ifcopenshell.api.root.create_entity(
        ifc, ifc_class="IfcMember", name="Profile-Test"
    )
    start, end = profile_builder(ifc)
    direction = ifc.createIfcDirection((0.0, 0.0, 1.0))
    if tapered:
        solid = ifc.create_entity(
            "IfcExtrudedAreaSolidTapered",
            SweptArea=start,
            ExtrudedDirection=direction,
            Depth=3000.0,
            EndSweptArea=end,
        )
    else:
        solid = ifc.createIfcExtrudedAreaSolid(start, None, direction, 3000.0)
    representation = ifc.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=element, representation=representation
    )
    ifcopenshell.api.geometry.edit_object_placement(ifc, product=element)
    return ifc, element


class TestAnalyzeElements:
    def test_profile_curve_and_mesh_agree_on_the_plate(self, pile_model):
        ifc, plate, _ = pile_model
        report = analyze_elements(ifc, global_ids=[plate.GlobalId])
        assert report["units"]["length_unit"] == "MILLIMETRE"
        record = report["elements"][0]

        dims = record["dimensions"]
        assert dims["width"]["file"] == pytest.approx(500.0, rel=1e-4)
        assert dims["width"]["si"] == pytest.approx(0.5, rel=1e-4)
        assert dims["width"]["source"] == "profile_curve"
        assert dims["height"]["file"] == pytest.approx(10.0, rel=1e-4)
        assert dims["length"]["file"] == pytest.approx(6000.0, rel=1e-4)
        assert dims["length"]["source"] == "extrusion_depth"
        assert dims["wall_thickness"]["si"] == pytest.approx(0.01, rel=1e-3)

        cut = record["cross_section"]
        assert cut["closed"] is True
        assert cut["width"] == pytest.approx(0.5, rel=1e-3)
        assert cut["height"] == pytest.approx(0.01, rel=1e-2)
        assert cut["area"] == pytest.approx(0.005, rel=1e-3)
        assert not [flag for flag in record["flags"] if flag.startswith("mismatch")]
        assert record["swept_solids"][0]["profile"]["name"] == "SP-500x10"

    def test_parameterized_profiles_read_exact_values(self, pile_model):
        ifc, _, box = pile_model
        report = analyze_elements(ifc, global_ids=[box.GlobalId])
        record = report["elements"][0]
        dims = record["dimensions"]
        assert dims["width"]["file"] == pytest.approx(400.0)
        assert dims["width"]["source"] == "profile_parameter"
        assert dims["height"]["file"] == pytest.approx(300.0)
        assert dims["wall_thickness"]["file"] == pytest.approx(12.0)
        params = record["swept_solids"][0]["profile"]["parameters"]
        assert params["XDim"] == pytest.approx(400.0)
        assert params["WallThickness"] == pytest.approx(12.0)

        cut = record["cross_section"]
        expected_area = (0.4 * 0.3) - (0.4 - 0.024) * (0.3 - 0.024)
        assert cut["area"] == pytest.approx(expected_area, rel=1e-2)
        assert cut["thickness"]["median"] == pytest.approx(0.012, rel=5e-2)

    def test_the_probe_reports_surface_areas(self, pile_model):
        ifc, plate, _ = pile_model
        report = analyze_elements(ifc, global_ids=[plate.GlobalId])
        box = report["elements"][0]["box"]
        # 0.5 x 0.01 x 6.0 metres
        assert box["volume"] == pytest.approx(0.03, rel=1e-3)
        assert box["surface_area"] == pytest.approx(6.13, rel=1e-3)
        assert box["top_area"] == pytest.approx(0.005, rel=1e-3)

    def test_stations_out_of_range_are_refused(self, pile_model):
        from ifc_console.core.results import ToolError

        ifc, plate, _ = pile_model
        with pytest.raises(ToolError) as caught:
            analyze_elements(ifc, global_ids=[plate.GlobalId], stations=(1.5,))
        assert caught.value.code == "INVALID_INPUT"

    def test_v2_contract_adds_orthonormal_frames_coverage_and_budgets(self, pile_model):
        ifc, plate, _ = pile_model
        report = analyze_elements(ifc, global_ids=[plate.GlobalId], detail="compact")
        assert report["analysis_version"] == "2.0"
        assert report["budgets"]["max_stations_per_element"] == 17
        record = report["elements"][0]
        axes = np.asarray(
            [
                record["frames"]["semantic"]["longitudinal"],
                record["frames"]["semantic"]["transverse"],
                record["frames"]["semantic"]["vertical"],
            ]
        )
        np.testing.assert_allclose(axes @ axes.T, np.eye(3), atol=1e-7)
        assert record["coverage"]["extracted"]
        assert record["geometry_signature"]["version"] == "1.0"
        assert record["analysis_evidence"]["stations_evaluated"] <= 17

    def test_fabrication_inventory_adds_exact_parameters_and_section_inertia(
        self, pile_model
    ):
        ifc, _, box = pile_model
        standard = analyze_elements(
            ifc, global_ids=[box.GlobalId], measurement_set="standard"
        )["elements"][0]
        fabrication = analyze_elements(
            ifc, global_ids=[box.GlobalId], measurement_set="fabrication"
        )["elements"][0]
        standard_ids = {item["id"] for item in standard["measurements"]}
        fabrication_ids = {item["id"] for item in fabrication["measurements"]}
        assert standard_ids < fabrication_ids
        assert "profile.parameter.wall_thickness" in fabrication_ids
        assert "section.second_moment_x" in fabrication_ids
        exact = next(
            item
            for item in fabrication["measurements"]
            if item["id"] == "profile.parameter.wall_thickness"
        )
        assert exact["evidence"]["ifc_attribute"] == "WallThickness"
        assert exact["uncertainty_si"] == 0.0

    def test_explicit_unknown_measurement_is_accounted_for(self, pile_model):
        ifc, plate, _ = pile_model
        requested = ["envelope.overall_height", "custom.not_supported"]
        record = analyze_elements(
            ifc,
            global_ids=[plate.GlobalId],
            measurement_ids=requested,
            include_sections=False,
        )["elements"][0]
        assert record["coverage"]["requested"] == requested
        assert record["coverage"]["extracted"] == ["envelope.overall_height"]
        assert record["coverage"]["unavailable"] == [
            {
                "id": "custom.not_supported",
                "reason": "not_supported_or_not_available_for_element",
            }
        ]
        assert record["section_analysis"]["strategy"] == "none"
        assert record["analysis_evidence"]["sections_skipped"] is True
        assert record["analysis_evidence"]["station_budget"] == 0

    def test_tapered_profile_emits_station_endpoints_not_a_singular_scalar(self):
        def profiles(ifc):
            return (
                ifc.createIfcRectangleProfileDef("AREA", "Start", None, 400.0, 300.0),
                ifc.createIfcRectangleProfileDef("AREA", "End", None, 200.0, 100.0),
            )

        ifc, element = _profile_model(profiles, tapered=True)
        record = analyze_elements(
            ifc,
            global_ids=[element.GlobalId],
            measurement_set="fabrication",
        )["elements"][0]
        by_id = {item["id"]: item for item in record["measurements"]}
        assert "profile.overall_width" not in by_id
        assert by_id["profile.overall_width.start"]["value_si"] == pytest.approx(0.4)
        assert by_id["profile.overall_width.start"]["station"] == 0.0
        assert by_id["profile.overall_width.end"]["value_si"] == pytest.approx(0.2)
        assert by_id["profile.overall_width.end"]["station"] == 1.0
        assert "profile.parameter.x_dim" not in by_id
        assert by_id["profile.parameter.x_dim.start"]["confidence"] == "high"
        ambiguity = {item["id"]: item for item in record["coverage"]["ambiguous"]}
        assert ambiguity["profile.overall_width"]["station_domain"] == [0.0, 1.0]
        assert ambiguity["profile.parameter.x_dim"]["reason"] == (
            "tapered_profile_requires_station"
        )
        assert record["profile_ranges"][0]["source"] == "tapered_profile_parameters"
        assert "tapered_profile_station_dependent" in record["flags"]

    def test_indexed_profile_segments_are_not_reported_as_exact(self):
        def profiles(ifc):
            points = ifc.createIfcCartesianPointList2D(
                ((0.0, 0.0), (500.0, 0.0), (500.0, 10.0), (0.0, 10.0))
            )
            segments = (ifc.createIfcLineIndex((1, 2, 3, 4, 1)),)
            curve = ifc.createIfcIndexedPolyCurve(points, segments, False)
            profile = ifc.createIfcArbitraryClosedProfileDef("AREA", "Indexed", curve)
            return profile, None

        ifc, element = _profile_model(profiles)
        record = analyze_elements(ifc, global_ids=[element.GlobalId])["elements"][0]
        width = next(
            item for item in record["measurements"] if item["id"] == "profile.overall_width"
        )
        assert width["source"] == "profile_curve_approximation"
        assert width["method"] == "sampled_or_chord_profile_curve"
        assert width["confidence"] == "medium"
        assert width["uncertainty_si"] > 0.0
        assert "indexed_segments_approximated" in width["flags"]
        assert "approximate_profile_curve_dimensions" in record["flags"]

    def test_high_precision_has_real_sampling_budgets(self, pile_model):
        ifc, plate, _ = pile_model
        standard = analyze_elements(
            ifc, global_ids=[plate.GlobalId], precision="standard"
        )
        high = analyze_elements(ifc, global_ids=[plate.GlobalId], precision="high")
        assert standard["budgets"]["max_stations_per_element"] == 17
        assert standard["budgets"]["max_thickness_rays_per_section"] == 2000
        assert high["budgets"]["max_stations_per_element"] == 33
        assert high["budgets"]["max_thickness_rays_per_section"] == 4000
        evidence = high["elements"][0]["analysis_evidence"]
        assert evidence["station_budget"] == 33
        assert evidence["thickness_ray_budget"] == 4000
        assert evidence["precision"] == {
            "mode": "high",
            "mesh_profile": "analysis",
            "mesh_reused": True,
            "higher_sampling": True,
        }

    def test_scale_aware_tolerance_reaches_sections(self, pile_model):
        ifc, plate, _ = pile_model
        record = analyze_elements(
            ifc,
            global_ids=[plate.GlobalId],
            detail="full",
        )["elements"][0]
        tolerance = record["tolerance"]["absolute_si"]
        analysis = record["section_analysis"]
        assert analysis["absolute_tolerance_si"] == pytest.approx(tolerance)
        dominant = analysis["representative_sections"]["dominant"]
        assert dominant["effective_tolerance_si"] == pytest.approx(tolerance)
        assert dominant["thickness_ray_budget"] == 2000

    @pytest.mark.parametrize("detail", ["compact", "standard", "full"])
    def test_single_element_response_is_bounded(self, pile_model, detail):
        ifc, _, box = pile_model
        report = analyze_elements(
            ifc,
            global_ids=[box.GlobalId],
            detail=detail,
            include_outline=True,
        )
        assert len(json.dumps(report, indent=2)) < 36_000
