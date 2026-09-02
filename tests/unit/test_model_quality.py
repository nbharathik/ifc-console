"""assess_model_quality: a deterministic scorecard with ordered improvements."""

from __future__ import annotations

from ifc_console.ifc.quality import DIMENSIONS, WEIGHTS, assess_model_quality, grade_for


def _dimension(report: dict, key: str) -> dict:
    return next(d for d in report["dimensions"] if d["key"] == key)


def test_the_scorecard_has_every_dimension_and_a_grade(ifc4) -> None:
    report = assess_model_quality(ifc4)

    assert [d["key"] for d in report["dimensions"]] == list(DIMENSIONS)
    assert 0.0 <= report["score"] <= 100.0
    assert report["grade"] in "ABCDE"
    assert report["counts"]["elements"] == 4
    assert report["counts"]["class_families"][0] == {"class": "IfcWall", "elements": 3}
    assert report["text"].startswith("Model quality")
    assert "Improvements, worst first:" in report["text"]
    for dimension in report["dimensions"]:
        assert dimension["weight"] == WEIGHTS[dimension["key"]]
        assert dimension["status"] in {"good", "fair", "poor", "n/a"}


def test_scores_are_fractions_of_elements_that_pass(ifc4) -> None:
    report = assess_model_quality(ifc4)

    materials = _dimension(report, "materials")
    assert materials["score"] == 0.0
    assert materials["findings"][0]["count"] == 4
    assert materials["findings"][0]["severity"] == "error"

    naming = _dimension(report, "naming")
    assert naming["score"] == 1.0
    assert naming["findings"] == []

    typing = _dimension(report, "typing")
    assert typing["metrics"]["typed_rate"] == 0.0
    assert typing["metrics"]["predefined_type_rate"] == 0.0

    spatial = _dimension(report, "spatial")
    assert spatial["metrics"]["contained_rate"] == 1.0
    assert spatial["metrics"]["storey_elevations_set"] is False

    properties = _dimension(report, "properties")
    walls = next(row for row in properties["metrics"]["classes"] if row["class"] == "IfcWall")
    assert walls["pset"] == "Pset_WallCommon"
    assert walls["present_rate"] == 1.0
    assert 0.0 < walls["key_filled_rate"] < 1.0
    door = next(row for row in properties["metrics"]["classes"] if row["class"] == "IfcDoor")
    assert door["present_rate"] == 0.0


def test_improving_the_model_raises_the_score(ifc4) -> None:
    import ifcopenshell.api.classification
    import ifcopenshell.api.material

    before = assess_model_quality(ifc4)
    material = ifcopenshell.api.material.add_material(ifc4, name="Concrete")
    for element in ifc4.by_type("IfcElement"):
        ifcopenshell.api.material.assign_material(
            ifc4, products=[element], type="IfcMaterial", material=material
        )
    system = ifcopenshell.api.classification.add_classification(ifc4, classification="Uniclass")
    for element in ifc4.by_type("IfcElement"):
        ifcopenshell.api.classification.add_reference(
            ifc4, products=[element], identification="Ss_25", name="Walls", classification=system
        )
    after = assess_model_quality(ifc4)

    assert after["score"] > before["score"]
    assert _dimension(after, "materials")["score"] == 1.0
    assert _dimension(after, "classification")["score"] == 1.0
    assert _dimension(after, "classification")["metrics"]["systems"][0]["name"] == "Uniclass"


def test_type_inherited_property_sets_count_for_the_occurrence(ifc4) -> None:
    import ifcopenshell.api.pset
    import ifcopenshell.api.root
    import ifcopenshell.api.type

    door = ifc4.by_type("IfcDoor")[0]
    door_type = ifcopenshell.api.root.create_entity(ifc4, ifc_class="IfcDoorType", name="D1")
    ifcopenshell.api.type.assign_type(ifc4, related_objects=[door], relating_type=door_type)
    pset = ifcopenshell.api.pset.add_pset(ifc4, product=door_type, name="Pset_DoorCommon")
    ifcopenshell.api.pset.edit_pset(ifc4, pset=pset, properties={"FireRating": "EI30"})

    report = assess_model_quality(ifc4)
    doors = next(
        row
        for row in _dimension(report, "properties")["metrics"]["classes"]
        if row["class"] == "IfcDoor"
    )
    assert doors["present_rate"] == 1.0
    assert doors["key_filled_rate"] > 0.0


def test_improvements_are_ordered_by_weighted_loss(ifc4) -> None:
    report = assess_model_quality(ifc4)

    top = report["top_improvements"]
    assert top
    losses = [(1.0 - item["score"]) * WEIGHTS[item["dimension"]] for item in top]
    assert losses == sorted(losses, reverse=True)
    assert all(item["score"] < 0.85 for item in top)
    assert len(top) <= 8


def test_examples_are_capped_per_finding(ifc4) -> None:
    report = assess_model_quality(ifc4, max_examples=1)
    for dimension in report["dimensions"]:
        for finding in dimension["findings"]:
            assert len(finding["examples"]) <= 1
            assert len(finding["global_ids"]) <= 1


def test_ai_authored_sets_are_counted_not_scored(ifc4) -> None:
    import ifcopenshell.api.pset

    wall = ifc4.by_type("IfcWall")[0]
    marked = ifcopenshell.api.pset.add_pset(ifc4, product=wall, name="IfcConsole_AI_Properties")
    ifcopenshell.api.pset.edit_pset(ifc4, pset=marked, properties={"Guess": "x"})

    report = assess_model_quality(ifc4)
    assert report["ai_authored"] == {
        "elements": 1,
        "property_sets": {"IfcConsole_AI_Properties": 1},
    }
    assert "AI-authored" in report["text"]


def test_grades_follow_the_thresholds() -> None:
    assert grade_for(95.0) == "A"
    assert grade_for(90.0) == "A"
    assert grade_for(80.0) == "B"
    assert grade_for(60.0) == "C"
    assert grade_for(45.0) == "D"
    assert grade_for(10.0) == "E"


def test_an_empty_model_scores_without_dividing_by_zero() -> None:
    import ifcopenshell.api.project
    import ifcopenshell.api.root

    ifc = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcProject", name="Empty")

    report = assess_model_quality(ifc)
    assert report["counts"]["elements"] == 0
    assert report["grade"] in "ABCDE"
    assert _dimension(report, "materials")["score"] is None
    assert _dimension(report, "properties")["score"] is None
