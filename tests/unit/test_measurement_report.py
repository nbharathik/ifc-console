"""Markdown reports for legacy and normalized geometry analysis records."""

from ifc_console.ifc.report import build_measurement_report

UNITS = {"length_unit": "millimetre", "to_si_factor": 0.001}


def _report(analysis: dict, **metadata) -> str:
    return build_measurement_report(
        title="Geometry evidence",
        model={"project": "Test", "file": "sample.ifc", "schema": "IFC4"},
        units=UNITS,
        entries=[{"analysis": analysis}],
        **metadata,
    )


def test_legacy_dimensions_keep_the_original_report_shape():
    text = _report(
        {
            "global_id": "legacy-guid",
            "class": "IfcMember",
            "name": "Legacy member",
            "dimensions": {"width": {"file": 200.0, "si": 0.2, "source": "profile_parameter"}},
            "flags": [],
        }
    )

    assert "### Dimensions" in text
    assert "| Width (b) | 200 millimetre | 0.2 m | profile_parameter |" in text
    assert "### Parametric measurements" not in text


def test_v2_measurements_are_grouped_with_coverage_and_alternatives():
    text = _report(
        {
            "analysis_version": "2.0",
            "object": {
                "global_id": "member-guid",
                "class": "IfcMember",
                "name": "Rotated I section",
                "type": "IPE 200",
            },
            "geometry_family": "constant_profile_extrusion",
            "measurements": [
                {
                    "id": "envelope.overall_length",
                    "label": "Overall length",
                    "quantity_kind": "length",
                    "value_si": 4.0,
                    "value_file": 4000.0,
                    "si_unit": "m",
                    "source": "profile_parameter",
                    "method": "ifc_representation",
                    "frame": "semantic",
                    "direction": "longitudinal",
                    "component": "whole_object",
                    "confidence": "high",
                    "uncertainty_si": 0.0,
                    "flags": [],
                    "alternatives": [
                        {
                            "value_si": 4.002,
                            "si_unit": "m",
                            "source": "mesh_axis",
                            "method": "directional_extent",
                            "confidence": "medium",
                            "absolute_delta_si": 0.002,
                            "relative_delta": 0.0005,
                            "status": "within_tolerance",
                        }
                    ],
                },
                {
                    "id": "profile.web_thickness",
                    "quantity_kind": "length",
                    "range_si": {"minimum": 0.006, "maximum": 0.008},
                    "source": "adaptive_section",
                    "method": "thickness_modes",
                    "frame": "semantic",
                    "station": 0.5,
                    "component": "body_1",
                    "confidence": "medium",
                    "flags": ["variable_profile"],
                    "alternatives": [],
                },
            ],
            "coverage": {
                "requested": [
                    "envelope.overall_length",
                    "profile.web_thickness",
                    "opening.clear_width",
                ],
                "extracted": ["envelope.overall_length", "profile.web_thickness"],
                "unavailable": [
                    {"id": "opening.clear_width", "reason": "no opening was represented"}
                ],
                "ambiguous": [],
                "conflicting": [
                    {"id": "profile.web_thickness", "reason": "section values vary by station"}
                ],
            },
            # v2 keeps this compatibility view, but the report must not duplicate it.
            "dimensions": {"length": {"file": 4000.0, "si": 4.0, "source": "profile_parameter"}},
            "flags": ["variable_profile"],
        }
    )

    assert "- Analysis contract: 2.0" in text
    assert "- Geometry family: constant_profile_extrusion" in text
    assert "#### Envelope" in text
    assert "#### Profile" in text
    assert "`envelope.overall_length`" in text
    assert "4 m" in text and "4000 millimetre" in text
    assert "0.006 to 0.008 m" in text
    assert "station 50%" in text and "body_1" in text
    assert "### Alternative evidence and source deltas" in text
    assert "0.002 SI, 0.05%" in text
    assert "### Measurement coverage" in text
    assert "no opening was represented" in text
    assert "section values vary by station" in text
    assert "### Dimensions" in text
    assert "| Length (L) | 4000 millimetre | 4 m | profile_parameter |" in text


def test_v2_report_header_pins_the_analysis_and_model_revision():
    text = _report(
        {"global_id": "guid", "class": "IfcWall", "measurements": [], "flags": []},
        analysis_version="2.0",
        model_revision={
            "model_id": "main-model",
            "fingerprint": "sha256-fixture",
            "revision": 7,
        },
    )

    assert "- Analysis contract: 2.0" in text
    assert "- Model revision: model_id=main-model, fingerprint=sha256-fixture, revision=7" in text
