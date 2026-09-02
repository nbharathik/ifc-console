"""Core primitives behind version 2 geometry analysis."""

from __future__ import annotations

import numpy as np
import pytest

from ifc_console.ifc import geometry, profile, representation, section
from ifc_console.ifc.mesh_analysis import scale_aware_tolerance
from ifc_console.ifc.similarity import (
    build_geometry_signature,
    compare_geometry_signatures,
)

_FACES = np.array(
    [
        [0, 2, 1],
        [0, 3, 2],
        [4, 5, 6],
        [4, 6, 7],
        [0, 1, 5],
        [0, 5, 4],
        [1, 2, 6],
        [1, 6, 5],
        [2, 3, 7],
        [2, 7, 6],
        [3, 0, 4],
        [3, 4, 7],
    ],
    dtype=np.int64,
)


def _box(low, high):
    low, high = np.asarray(low, dtype=float), np.asarray(high, dtype=float)
    vertices = np.array(
        [
            [low[0], low[1], low[2]],
            [high[0], low[1], low[2]],
            [high[0], high[1], low[2]],
            [low[0], high[1], low[2]],
            [low[0], low[1], high[2]],
            [high[0], low[1], high[2]],
            [high[0], high[1], high[2]],
            [low[0], high[1], high[2]],
        ]
    )
    return vertices, _FACES.copy()


def test_adaptive_sections_find_piecewise_profile_regions_within_budget():
    small, faces = _box([-2, -0.5, -0.5], [0, 0.5, 0.5])
    large, _ = _box([0, -1, -1], [2, 1, 1])
    analysis = section.adaptive_sections(
        np.vstack([small, large]),
        np.vstack([faces, faces + 8]),
        np.array([1.0, 0.0, 0.0]),
        max_stations=17,
    )
    assert analysis["stations_evaluated"] <= 17
    assert len(analysis["profile_regions"]) >= 2
    areas = {
        round(item["descriptor"]["area_si"], 3)
        for item in analysis["profile_regions"]
        if item["descriptor"]["area_si"] is not None
    }
    assert {1.0, 4.0} <= areas


def test_adaptive_sections_keep_a_constant_square_constant():
    vertices, faces = _box([0, -0.5, -0.5], [1, 0.5, 0.5])
    analysis = section.adaptive_sections(
        vertices,
        faces,
        np.array([1.0, 0.0, 0.0]),
        max_stations=17,
    )
    assert analysis["variation"] == "constant"
    assert analysis["stations_evaluated"] == 7
    assert len(analysis["profile_regions"]) == 1
    for station in analysis["stations"]:
        assert station["width"] == pytest.approx(1.0, abs=1e-8)
        assert station["height"] == pytest.approx(1.0, abs=1e-8)
        assert "section_equal_bounds_axes_ambiguous" in station["frame_flags"]


def test_rotated_square_section_bounds_are_intrinsic_and_station_stable():
    vertices, faces = _box([0, -0.5, -0.5], [1, 0.5, 0.5])
    angle = np.radians(31.0)
    cosine, sine = np.cos(angle), np.sin(angle)
    rotation = np.array([[1.0, 0.0, 0.0], [0.0, cosine, -sine], [0.0, sine, cosine]])
    rotated = vertices @ rotation.T
    analysis = section.adaptive_sections(
        rotated,
        faces[::-1],
        np.array([1.0, 0.0, 0.0]),
        max_stations=17,
    )
    assert analysis["variation"] == "constant"
    assert analysis["stations_evaluated"] == 7
    bounds = [(station["width"], station["height"]) for station in analysis["stations"]]
    assert bounds == pytest.approx([(1.0, 1.0)] * 7, abs=1e-8)


def test_closed_section_reports_centroid_and_second_moments():
    vertices, faces = _box([-1, -0.5, -0.5], [1, 0.5, 0.5])
    metrics = section.section_metrics(vertices, faces, np.zeros(3), np.array([0.0, 0.0, 1.0]))
    assert metrics["centroid_2d"] == pytest.approx([0.0, 0.0], abs=1e-9)
    moments = metrics["second_moments_si4"]
    assert moments["i_xx"] == pytest.approx(2.0 / 12.0, rel=1e-8)
    assert moments["i_yy"] == pytest.approx(8.0 / 12.0, rel=1e-8)


def test_tolerance_records_georeferenced_resolution_and_precision():
    vertices, _ = _box([0, 0, 0], [5, 0.2, 3])
    vertices += np.array([2_700_000.0, 1_200_000.0, 120.0])
    tessellation = {"settings": {"mesher-linear-deflection": 0.0005}}
    standard = scale_aware_tolerance(
        vertices,
        file_unit_scale=0.001,
        tessellation=tessellation,
        precision="standard",
    )
    high = scale_aware_tolerance(
        vertices,
        file_unit_scale=0.001,
        tessellation=tessellation,
        precision="high",
    )
    assert standard["absolute_si"] >= standard["floating_point_resolution_si"]
    assert high["absolute_si"] <= standard["absolute_si"]
    assert standard["policy"] == "scale_aware_v1"


def test_mapped_boolean_inventory_keeps_both_operands_and_uniform_scale(ifc4):
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.root

    context = ifcopenshell.api.context.add_context(ifc4, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        ifc4,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=context,
    )
    profile = ifc4.createIfcRectangleProfileDef("AREA", "R", None, 2.0, 1.0)
    direction = ifc4.createIfcDirection((0.0, 0.0, 1.0))
    first = ifc4.createIfcExtrudedAreaSolid(profile, None, direction, 3.0)
    second = ifc4.createIfcExtrudedAreaSolid(profile, None, direction, 1.0)
    boolean = ifc4.createIfcBooleanResult("DIFFERENCE", first, second)
    source_rep = ifc4.createIfcShapeRepresentation(body, "Body", "CSG", [boolean])
    origin = ifc4.createIfcAxis2Placement3D(ifc4.createIfcCartesianPoint((0.0, 0.0, 0.0)))
    rep_map = ifc4.createIfcRepresentationMap(origin, source_rep)
    target = ifc4.create_entity(
        "IfcCartesianTransformationOperator3D",
        LocalOrigin=ifc4.createIfcCartesianPoint((0.0, 0.0, 0.0)),
        Scale=2.0,
    )
    mapped = ifc4.createIfcMappedItem(rep_map, target)
    occurrence_rep = ifc4.createIfcShapeRepresentation(
        body, "Body", "MappedRepresentation", [mapped]
    )
    element = ifcopenshell.api.root.create_entity(ifc4, ifc_class="IfcMember", name="Mapped")
    ifcopenshell.api.geometry.assign_representation(
        ifc4, product=element, representation=occurrence_rep
    )

    inventory = representation.representation_inventory(element)
    assert inventory["classes"]["IfcMappedItem"] == 1
    assert inventory["classes"]["IfcBooleanResult"] == 1
    assert inventory["classes"]["IfcExtrudedAreaSolid"] == 2
    assert "boolean_modified_geometry" in inventory["flags"]
    assert inventory["unique_mapped_sources"] == 1
    assert inventory["nodes_skipped"] == 0
    assert inventory["unsupported_nodes"] == 0
    sources = representation.solid_sources(element)
    assert {source.boolean_role for source in sources} == {"base", "modifier"}
    assert all(source.uniform_scale == pytest.approx(2.0) for source in sources)


def test_repeated_mapped_elements_reuse_intrinsic_solid_analysis(ifc4, monkeypatch):
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.root

    context = ifcopenshell.api.context.add_context(ifc4, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        ifc4,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=context,
    )
    rectangle = ifc4.createIfcRectangleProfileDef("AREA", "Shared", None, 2.0, 1.0)
    direction = ifc4.createIfcDirection((0.0, 0.0, 1.0))
    solid = ifc4.createIfcExtrudedAreaSolid(rectangle, None, direction, 3.0)
    source_rep = ifc4.createIfcShapeRepresentation(body, "Body", "SweptSolid", [solid])
    origin = ifc4.createIfcAxis2Placement3D(ifc4.createIfcCartesianPoint((0.0, 0.0, 0.0)))
    rep_map = ifc4.createIfcRepresentationMap(origin, source_rep)
    elements = []
    for index in range(2):
        target = ifc4.create_entity(
            "IfcCartesianTransformationOperator3D",
            LocalOrigin=ifc4.createIfcCartesianPoint((float(index) * 5.0, 0.0, 0.0)),
        )
        mapped = ifc4.createIfcMappedItem(rep_map, target)
        occurrence_rep = ifc4.createIfcShapeRepresentation(
            body, "Body", "MappedRepresentation", [mapped]
        )
        element = ifcopenshell.api.root.create_entity(
            ifc4, ifc_class="IfcMember", name=f"Mapped {index + 1}"
        )
        ifcopenshell.api.geometry.assign_representation(
            ifc4, product=element, representation=occurrence_rep
        )
        elements.append(element)

    intrinsic_calls = 0
    original = profile._intrinsic_swept_record

    def counted_intrinsic(solid, factor, *, reuse):
        nonlocal intrinsic_calls
        intrinsic_calls += 1
        return original(solid, factor, reuse=reuse)

    monkeypatch.setattr(profile, "_intrinsic_swept_record", counted_intrinsic)
    vertices, faces = _box([-0.5, -0.5, 0.0], [0.5, 0.5, 3.0])

    def meshes(_ifc, requested, *, profile="standard", max_triangles=None):
        return {element.id(): (vertices, faces) for element in requested}

    with geometry.mesh_provider(meshes):
        report = profile.analyze_elements(
            ifc4,
            global_ids=[element.GlobalId for element in elements],
            detail="compact",
            station_strategy="none",
            include_sections=False,
        )

    reuse = report["performance"]["intrinsic_reuse"]
    assert intrinsic_calls == 1
    assert reuse == {
        "unique_mapped_sources": 1,
        "mapped_source_occurrences": 2,
        "solid_definitions_computed": 1,
        "solid_cache_hits": 1,
        "profile_definitions_computed": 1,
        "profile_cache_hits": 0,
    }
    assert all(
        record["representation_inventory"]["mapped_source_ids"] == [rep_map.id()]
        for record in report["elements"]
    )


def _signature_record(extents, *, geometry_family="constant_profile_extrusion", cls="IfcMember"):
    names = ("length", "width", "height")
    return {
        "object": {"class": cls},
        "geometry_family": geometry_family,
        "swept_solids": [{"profile": {"family": "rectangle"}}],
        "topology": {"connected_components": 1, "closed_shells": 1, "through_holes": 0},
        "measurements": [
            {"id": f"envelope.overall_{name}", "value_si": value}
            for name, value in zip(names, extents, strict=True)
        ],
        "section_analysis": {
            "variation": "constant",
            "representative_sections": {
                "dominant": {
                    "descriptor": {
                        "width_si": extents[1],
                        "height_si": extents[2],
                        "loop_count": 1,
                        "hole_count": 0,
                    }
                }
            },
        },
    }


def test_similarity_is_scale_invariant_and_rejects_incompatible_family():
    exemplar = build_geometry_signature(_signature_record([6.0, 0.5, 0.1]))
    scaled = build_geometry_signature(_signature_record([12.0, 1.0, 0.2]))
    different = build_geometry_signature(
        _signature_record([6.0, 0.5, 0.1], geometry_family="surface_or_brep", cls="IfcWall")
    )
    accepted = compare_geometry_signatures(exemplar, scaled)
    rejected = compare_geometry_signatures(exemplar, different)
    assert accepted["matched"] is True
    assert accepted["score"] == pytest.approx(1.0)
    assert rejected["matched"] is False
    assert rejected["hard_filters_passed"] is False
    assert rejected["mismatches"]
