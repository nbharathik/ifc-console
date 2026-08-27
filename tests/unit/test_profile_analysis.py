"""Cross-section math and the full element probe, on known geometry."""

from __future__ import annotations

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
