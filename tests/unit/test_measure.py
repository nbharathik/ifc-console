"""measure_elements and measure_distance: methods, units, flags."""

from __future__ import annotations

import pytest

from ifc_console.ifc.measure import measure_distance, measure_elements
from ifc_console.ifc.quantities import compute_quantities
from ifc_console.mcp.envelope import ToolError


def add_layer_set(ifc, wall, layers):
    """Attach an IfcMaterialLayerSetUsage with the given (name, thickness) layers."""
    import ifcopenshell.api.material as material_api

    layer_set = material_api.add_material_set(ifc, name="WallLayers", set_type="IfcMaterialLayerSet")
    for name, thickness in layers:
        material = material_api.add_material(ifc, name=name)
        layer = material_api.add_layer(ifc, layer_set=layer_set, material=material)
        material_api.edit_layer(ifc, layer=layer, attributes={"LayerThickness": thickness})
    material_api.assign_material(
        ifc, products=[wall], type="IfcMaterialLayerSetUsage", material=layer_set
    )
    return layer_set


def add_width_qto(ifc, wall, width=200.0):
    import ifcopenshell.api.pset as pset_api

    qto = pset_api.add_qto(ifc, product=wall, name="Qto_WallBaseQuantities")
    pset_api.edit_qto(ifc, qto=qto, properties={"Width": width})
    return qto


class TestStoredQto:
    def test_reads_the_stored_width_in_both_unit_systems(self, ifc4):
        wall = ifc4.by_type("IfcWall")[0]
        add_width_qto(ifc4, wall, 200.0)
        report = measure_elements(
            ifc4, global_ids=[wall.GlobalId], method="stored_qto", quantity="Width"
        )
        record = report["elements"][0]
        assert record["value"] == pytest.approx(200.0)
        assert record["unit"] == "MILLIMETRE"
        assert record["value_si"] == pytest.approx(0.2)
        assert record["si_unit"] == "METRE"
        assert record["inputs"]["qto_set"] == "Qto_WallBaseQuantities"
        assert record["flags"] == []

    def test_missing_quantity_is_flagged_not_fatal(self, ifc4):
        report = measure_elements(ifc4, selector="IfcWall", method="stored_qto", quantity="Width")
        assert report["summary"]["measured"] == 0
        assert report["summary"]["missing"] == 3
        assert all("no_quantity_sets" in r["flags"] for r in report["elements"])

    def test_quantity_is_required(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            measure_elements(ifc4, selector="IfcWall", method="stored_qto")
        assert excinfo.value.code == "INVALID_INPUT"


class TestLayerSum:
    def test_sums_all_layers(self, ifc4):
        wall = ifc4.by_type("IfcWall")[0]
        add_layer_set(ifc4, wall, [("Concrete", 180.0), ("Plaster Finish", 20.0)])
        report = measure_elements(ifc4, global_ids=[wall.GlobalId], method="layer_sum")
        record = report["elements"][0]
        assert record["value"] == pytest.approx(200.0)
        assert record["value_si"] == pytest.approx(0.2)
        assert len(record["inputs"]["layers"]) == 2

    def test_exclude_globs_drop_finishes(self, ifc4):
        wall = ifc4.by_type("IfcWall")[0]
        add_layer_set(ifc4, wall, [("Concrete", 180.0), ("Plaster Finish", 20.0)])
        report = measure_elements(
            ifc4,
            global_ids=[wall.GlobalId],
            method="layer_sum",
            metric="thickness",
            exclude_layers=["*finish*"],
        )
        record = report["elements"][0]
        assert record["metric"] == "thickness"
        assert record["value"] == pytest.approx(180.0)
        excluded = [item for item in record["inputs"]["layers"] if not item["included"]]
        assert len(excluded) == 1
        assert excluded[0]["material"] == "Plaster Finish"

    def test_walls_without_layers_are_flagged(self, ifc4):
        report = measure_elements(ifc4, selector="IfcWall", method="layer_sum")
        assert all("no_layer_set" in r["flags"] for r in report["elements"])
        assert report["summary"]["measured"] == 0


class TestGeometryExtent:
    def test_thickness_along_the_local_y_axis(self, ifc4):
        report = measure_elements(
            ifc4, selector="IfcWall", method="geometry_extent", metric="thickness"
        )
        assert report["summary"]["measured"] == 3
        for record in report["elements"]:
            assert record["value"] == pytest.approx(200.0, rel=0.02)
            assert record["unit"] == "MILLIMETRE"
            assert record["value_si"] == pytest.approx(0.2, rel=0.02)

    def test_world_axis_sees_the_rotation(self, ifc4):
        report = measure_elements(
            ifc4, selector="IfcWall, Name=Wall-2", method="geometry_extent", axis="world_x"
        )
        assert report["elements"][0]["value"] == pytest.approx(200.0, rel=0.05)

    def test_bad_axis_is_a_clear_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            measure_elements(ifc4, selector="IfcWall", method="geometry_extent", axis="diagonal")
        assert excinfo.value.code == "INVALID_INPUT"

    def test_bad_method_is_a_clear_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            measure_elements(ifc4, selector="IfcWall", method="guess")
        assert excinfo.value.code == "INVALID_INPUT"


class TestMeasureDistance:
    def test_stacked_walls_touch(self, ifc4):
        """Wall-3 sits directly on top of Wall-1."""
        walls = {w.Name: w for w in ifc4.by_type("IfcWall")}
        report = measure_distance(
            ifc4,
            global_ids_a=[walls["Wall-1"].GlobalId],
            global_ids_b=[walls["Wall-3"].GlobalId],
        )
        assert report["aabb_gap"]["si"] == pytest.approx(0.0, abs=1e-6)
        assert report["surface_distance"]["si"] == pytest.approx(0.0, abs=1e-6)
        assert report["units"]["length_unit"] == "MILLIMETRE"

    def test_sets_pick_the_closest_pair(self, ifc4):
        report = measure_distance(ifc4, set_a="IfcWall, Name=Wall-1", set_b="IfcWall")
        assert report["a"]["name"] == "Wall-1"
        assert report["b"]["name"] in {"Wall-2", "Wall-3"}
        assert report["closest_pairs"]

    def test_same_single_element_is_an_error(self, ifc4):
        wall = ifc4.by_type("IfcWall")[0]
        with pytest.raises(ToolError) as excinfo:
            measure_distance(
                ifc4, global_ids_a=[wall.GlobalId], global_ids_b=[wall.GlobalId]
            )
        assert excinfo.value.code == "INVALID_INPUT"


class TestDerivedQuantities:
    def test_derived_fallback_fills_missing_walls(self, ifc4):
        report = compute_quantities(ifc4, "IfcWall", source="derived")
        assert report["source"] == "stored+derived"
        assert report["derived_elements"] == 3
        # walls are 5x0.2x3, 4x0.2x3 and 5x0.2x3 metres; totals are file units (mm)
        assert report["totals"]["Width"] == pytest.approx(600.0, rel=0.02)
        assert report["totals"]["GrossVolume"] == pytest.approx(8.4e9, rel=0.05)

    def test_stored_values_are_never_overwritten(self, ifc4):
        wall = ifc4.by_type("IfcWall")[0]
        add_width_qto(ifc4, wall, 200.0)
        report = compute_quantities(ifc4, "IfcWall", source="derived")
        assert report["derived_elements"] == 2
        assert report["totals"]["Width"] == pytest.approx(600.0, rel=0.02)

    def test_stored_mode_is_unchanged(self, ifc4):
        report = compute_quantities(ifc4, "IfcWall", source="stored")
        assert report["source"] == "stored"
        assert "derived_elements" not in report
        assert "source='derived'" in report["note"]

    def test_bad_source_is_a_clear_error(self, ifc4):
        with pytest.raises(ToolError) as excinfo:
            compute_quantities(ifc4, "IfcWall", source="magic")
        assert excinfo.value.code == "INVALID_INPUT"
