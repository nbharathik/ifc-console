"""AI-authored values must stay identifiable and separable forever."""

from __future__ import annotations

import json

import pytest

from ifc_console.ifc.ai_provenance import (
    MEASUREMENT_PSET,
    PREFIX,
    PROPERTY_PSET,
    PROVENANCE_PROPERTY,
    PROVENANCE_PSET,
    Provenance,
    is_ai_authored,
    measurement_property,
    provenance_property_name,
    read_ai_properties,
    validate_property_name,
    validate_pset,
)


def _model():
    import ifcopenshell.api.project
    import ifcopenshell.api.root
    import ifcopenshell.api.unit

    ifc = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcProject", name="P")
    unit = ifcopenshell.api.unit.add_si_unit(ifc, unit_type="LENGTHUNIT")
    ifcopenshell.api.unit.assign_unit(ifc, units=[unit])
    return ifc


def _pset(ifc, product, name: str, properties: dict) -> None:
    import ifcopenshell.api.pset

    pset = ifcopenshell.api.pset.add_pset(ifc, product=product, name=name)
    ifcopenshell.api.pset.edit_pset(ifc, pset=pset, properties=properties)


class TestNamespace:
    def test_every_agent_written_set_shares_one_prefix(self):
        for name in (MEASUREMENT_PSET, PROPERTY_PSET, PROVENANCE_PSET):
            assert name.startswith(PREFIX)
            assert is_ai_authored(name)

    def test_authored_property_sets_are_not_mistaken_for_ai_ones(self):
        for name in ("Pset_WallCommon", "Qto_WallBaseQuantities", "CustomCompanyData"):
            assert not is_ai_authored(name)

    def test_agents_cannot_write_into_an_authored_property_set(self):
        assert validate_pset(MEASUREMENT_PSET) == MEASUREMENT_PSET
        assert validate_pset(PROPERTY_PSET) == PROPERTY_PSET
        for name in ("Pset_WallCommon", PROVENANCE_PSET, "IfcConsole_AI_Anything"):
            with pytest.raises(ValueError):
                validate_pset(name)

    def test_property_names_are_conservative(self):
        assert validate_property_name(" UValue ") == "UValue"
        for name in ("", "9lives", "a.b", "a:b", "a/b", "x" * 64, "drop table"):
            with pytest.raises(ValueError):
                validate_property_name(name)

    def test_every_metric_maps_to_a_typed_property(self):
        name, nominal = measurement_property("thickness")
        assert name == "MeasuredThickness"
        assert nominal == "IfcLengthMeasure"
        assert measurement_property("area")[1] == "IfcAreaMeasure"
        with pytest.raises(ValueError):
            measurement_property("vibes")


class TestProvenanceRecord:
    def test_the_record_names_who_wrote_the_value_and_why(self):
        record = Provenance(
            agent="measurement-agent",
            property_name="MeasuredThickness",
            pset=MEASUREMENT_PSET,
            method="geometry_extent",
            model="anthropic/claude-sonnet-5",
            source="QS-Manual.pdf p12",
            unit="mm",
            confidence="high",
        ).with_change_set("sha256:abc")
        payload = json.loads(record.to_json())
        assert payload["ai_generated"] is True
        assert payload["agent"] == "measurement-agent"
        assert payload["method"] == "geometry_extent"
        assert payload["source"] == "QS-Manual.pdf p12"
        assert payload["change_set"] == "sha256:abc"
        assert payload["property"] == f"{MEASUREMENT_PSET}.MeasuredThickness"
        assert payload["written_at"].endswith("+00:00")

    def test_empty_fields_are_left_out_rather_than_stored_as_blanks(self):
        payload = json.loads(
            Provenance(agent="a", property_name="P", pset=PROPERTY_PSET, method="m").to_json()
        )
        assert "source" not in payload
        assert "confidence" not in payload

    def test_long_user_instructions_are_truncated_not_embedded_whole(self):
        payload = json.loads(
            Provenance(
                agent="a",
                property_name="P",
                pset=PROPERTY_PSET,
                method="m",
                instructions="x" * 5000,
            ).to_json()
        )
        assert len(payload["instructions"]) == 240

    def test_proposal_id_is_distinct_from_the_legacy_change_set_reference(self):
        payload = json.loads(
            Provenance(agent="a", property_name="P", pset=PROPERTY_PSET, method="m")
            .with_proposal("proposal-123")
            .to_json()
        )
        assert payload["proposal_id"] == "proposal-123"
        assert "change_set" not in payload


class TestAudit:
    def test_only_ai_authored_elements_are_reported_with_their_record(self):
        import ifcopenshell.api.root

        ifc = _model()
        marked = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="Wall-1")
        authored = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="Wall-2")
        _pset(ifc, marked, MEASUREMENT_PSET, {"MeasuredThickness": 0.24})
        record = Provenance(
            agent="measurement-agent",
            property_name="MeasuredThickness",
            pset=MEASUREMENT_PSET,
            method="geometry_extent",
            source="QS-Manual.pdf p12",
        )
        _pset(ifc, marked, PROVENANCE_PSET, {PROVENANCE_PROPERTY: record.to_json()})
        _pset(ifc, authored, "Pset_WallCommon", {"FireRating": "F30"})

        report = read_ai_properties(ifc)
        assert len(report["elements"]) == 1
        row = report["elements"][0]
        assert row["name"] == "Wall-1"
        assert row["properties"][f"{MEASUREMENT_PSET}.MeasuredThickness"] == 0.24
        assert row["provenance"]["agent"] == "measurement-agent"
        assert row["provenance"]["source"] == "QS-Manual.pdf p12"
        assert report["property_sets"] == {MEASUREMENT_PSET: 1, PROVENANCE_PSET: 1}
        assert report["truncated"] is False

    def test_a_clean_model_reports_nothing(self):
        import ifcopenshell.api.root

        ifc = _model()
        wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="W")
        _pset(ifc, wall, "Pset_WallCommon", {"FireRating": "F30"})
        report = read_ai_properties(ifc)
        assert report["elements"] == []
        assert report["property_sets"] == {}

    def test_an_unreadable_provenance_record_is_reported_not_swallowed(self):
        import ifcopenshell.api.root

        ifc = _model()
        wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="W")
        _pset(ifc, wall, MEASUREMENT_PSET, {"MeasuredThickness": 1.0})
        _pset(ifc, wall, PROVENANCE_PSET, {PROVENANCE_PROPERTY: "not json"})
        row = read_ai_properties(ifc)["elements"][0]
        assert row["provenance"]["raw"] == "not json"

    def test_each_target_property_keeps_an_independent_provenance_record(self):
        import ifcopenshell.api.root

        ifc = _model()
        wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="W")
        target_name = "MeasuredThickness"
        _pset(ifc, wall, MEASUREMENT_PSET, {target_name: 0.24})
        _pset(ifc, wall, PROPERTY_PSET, {target_name: "from schedule"})
        measured = Provenance(
            agent="measurement-agent",
            property_name=target_name,
            pset=MEASUREMENT_PSET,
            method="geometry_extent",
            written_at="2026-01-01T00:00:00+00:00",
        )
        scheduled = Provenance(
            agent="property-agent",
            property_name=target_name,
            pset=PROPERTY_PSET,
            method="schedule_lookup",
            written_at="2026-01-02T00:00:00+00:00",
        )
        measured_marker = provenance_property_name(MEASUREMENT_PSET, target_name)
        scheduled_marker = provenance_property_name(PROPERTY_PSET, target_name)
        assert measured_marker != scheduled_marker
        _pset(
            ifc,
            wall,
            PROVENANCE_PSET,
            {
                measured_marker: measured.to_json(),
                scheduled_marker: scheduled.to_json(),
            },
        )

        row = read_ai_properties(ifc)["elements"][0]
        measured_key = f"{MEASUREMENT_PSET}.{target_name}"
        scheduled_key = f"{PROPERTY_PSET}.{target_name}"
        assert row["properties"][measured_key] == 0.24
        assert row["properties"][scheduled_key] == "from schedule"
        assert row["provenance_by_property"][measured_key]["method"] == "geometry_extent"
        assert row["provenance_by_property"][scheduled_key]["method"] == "schedule_lookup"
        assert row["provenance"]["agent"] == "property-agent"
        assert not any(key.startswith(f"{PROVENANCE_PSET}.") for key in row["properties"])

    def test_newer_per_property_marker_supersedes_a_legacy_record(self):
        import ifcopenshell.api.root

        ifc = _model()
        wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="W")
        target = f"{MEASUREMENT_PSET}.MeasuredThickness"
        old = Provenance(
            agent="old-agent",
            property_name="MeasuredThickness",
            pset=MEASUREMENT_PSET,
            method="old",
            written_at="2025-01-01T00:00:00+00:00",
        )
        new = Provenance(
            agent="new-agent",
            property_name="MeasuredThickness",
            pset=MEASUREMENT_PSET,
            method="new",
            written_at="2026-01-01T00:00:00+00:00",
        )
        _pset(ifc, wall, MEASUREMENT_PSET, {"MeasuredThickness": 0.3})
        _pset(
            ifc,
            wall,
            PROVENANCE_PSET,
            {
                PROVENANCE_PROPERTY: old.to_json(),
                provenance_property_name(MEASUREMENT_PSET, "MeasuredThickness"): new.to_json(),
            },
        )

        row = read_ai_properties(ifc)["elements"][0]
        assert row["provenance"]["agent"] == "new-agent"
        assert row["provenance_by_property"][target]["agent"] == "new-agent"

    def test_the_report_is_bounded(self):
        import ifcopenshell.api.root

        ifc = _model()
        for index in range(6):
            wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name=f"W{index}")
            _pset(ifc, wall, MEASUREMENT_PSET, {"MeasuredThickness": 1.0})
        report = read_ai_properties(ifc, limit=3)
        assert len(report["elements"]) == 3
        assert report["truncated"] is True
