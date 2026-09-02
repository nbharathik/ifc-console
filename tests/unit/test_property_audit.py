"""audit_element_properties: the schema's templates against what is there."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.core.results import ToolError
from ifc_console.ifc.property_audit import (
    PropertyIndex,
    audit_element_properties,
    core_set_names,
    derivation_hint,
)


def _wall(ifc, name: str):
    return next(w for w in ifc.by_type("IfcWall") if w.Name == name)


def _rows(element: dict) -> dict[str, dict]:
    return {row["name"]: row for row in element["psets"]}


def test_core_scope_reports_the_common_pset_and_base_quantities(ifc4) -> None:
    report = audit_element_properties(ifc4, global_ids=[_wall(ifc4, "Wall-1").GlobalId])

    element = report["elements"][0]
    assert element["class"] == "IfcWall"
    assert element["storey"] == "Level 1"
    assert element["core_sets"] == {"pset": "Pset_WallCommon", "qto": "Qto_WallBaseQuantities"}
    rows = _rows(element)
    assert set(rows) == {"Pset_WallCommon", "Qto_WallBaseQuantities"}
    assert rows["Pset_WallCommon"]["defined_on"] == "occurrence"
    assert set(rows["Pset_WallCommon"]["filled"]) == {"IsExternal", "FireRating"}
    assert "LoadBearing" in rows["Pset_WallCommon"]["missing"]
    assert rows["Qto_WallBaseQuantities"]["defined_on"] == "none"
    assert element["summary"]["filled"] == 2
    assert element["summary"]["expected"] == 22
    assert report["summary"]["completeness"] == pytest.approx(2 / 22, abs=1e-3)


def test_gaps_are_grouped_by_where_they_can_be_derived_from(ifc4) -> None:
    report = audit_element_properties(ifc4, global_ids=[_wall(ifc4, "Wall-2").GlobalId])

    derivable = report["elements"][0]["derivable"]
    assert "Qto_WallBaseQuantities.Length" in derivable["geometry"]
    assert "Qto_WallBaseQuantities.GrossVolume" in derivable["geometry"]
    assert "Pset_WallCommon.FireRating" in derivable["document"]
    assert "Pset_WallCommon.LoadBearing" in derivable["type"]
    assert "Qto_WallBaseQuantities.GrossWeight" in derivable["material"]
    assert derivation_hint("Pset_Anything", "Unknown", "pset")[0] == "manual"


def test_full_detail_carries_data_types_and_enumerations(ifc4) -> None:
    door = ifc4.by_type("IfcDoor")[0]
    report = audit_element_properties(ifc4, global_ids=[door.GlobalId], detail="full")

    rows = _rows(report["elements"][0])
    status = next(p for p in rows["Pset_DoorCommon"]["properties"] if p["name"] == "Status")
    assert status["status"] == "missing"
    assert status["data_type"] == "IfcLabel"
    assert "NEW" in status["enumeration"]
    assert status["derivable"] == "project"
    assert "hint" not in status
    assert "template_type" not in status
    assert "data sheet" in report["hints"]["FireRating"]
    assert "NEW" in report["hints"]["Status"]


def test_values_inherited_from_the_type_count_as_filled(ifc4) -> None:
    import ifcopenshell.api.pset
    import ifcopenshell.api.root
    import ifcopenshell.api.type

    wall = _wall(ifc4, "Wall-3")
    wall_type = ifcopenshell.api.root.create_entity(ifc4, ifc_class="IfcWallType", name="WT-1")
    ifcopenshell.api.type.assign_type(ifc4, related_objects=[wall], relating_type=wall_type)
    pset = ifcopenshell.api.pset.add_pset(ifc4, product=wall_type, name="Pset_WallCommon")
    ifcopenshell.api.pset.edit_pset(ifc4, pset=pset, properties={"LoadBearing": True})

    report = audit_element_properties(ifc4, global_ids=[wall.GlobalId], detail="full")

    element = report["elements"][0]
    assert element["type"] == {
        "class": "IfcWallType",
        "name": "WT-1",
        "global_id": wall_type.GlobalId,
    }
    common = _rows(element)["Pset_WallCommon"]
    assert common["defined_on"] == "both"
    assert "LoadBearing" in common["filled"]
    load = next(p for p in common["properties"] if p["name"] == "LoadBearing")
    assert load["value"] is True
    assert load["source"] == "type"


def test_an_empty_property_is_reported_as_empty_not_missing(ifc4) -> None:
    wall = _wall(ifc4, "Wall-2")
    pset = next(
        rel.RelatingPropertyDefinition
        for rel in wall.IsDefinedBy
        if rel.is_a("IfcRelDefinesByProperties")
    )
    blank = ifc4.createIfcPropertySingleValue("FireRating", None, None, None)
    pset.HasProperties = [*pset.HasProperties, blank]

    report = audit_element_properties(ifc4, global_ids=[wall.GlobalId], detail="full")
    fire = next(
        p
        for p in _rows(report["elements"][0])["Pset_WallCommon"]["properties"]
        if p["name"] == "FireRating"
    )
    assert fire["status"] == "empty"


def test_all_scope_lists_every_template_and_custom_and_ai_sets_are_separated(ifc4) -> None:
    import ifcopenshell.api.pset

    wall = _wall(ifc4, "Wall-1")
    custom = ifcopenshell.api.pset.add_pset(ifc4, product=wall, name="CPset_Project")
    ifcopenshell.api.pset.edit_pset(ifc4, pset=custom, properties={"Zone": "A"})
    marked = ifcopenshell.api.pset.add_pset(ifc4, product=wall, name="IfcConsole_AI_Properties")
    ifcopenshell.api.pset.edit_pset(ifc4, pset=marked, properties={"Guess": "x"})

    report = audit_element_properties(ifc4, global_ids=[wall.GlobalId], psets="all")

    element = report["elements"][0]
    assert len(element["psets"]) == len(element["applicable_psets"]) == 13
    assert element["custom_psets"] == ["CPset_Project"]
    assert element["ai_authored_psets"] == ["IfcConsole_AI_Properties"]
    # A named request narrows to exactly those templates.
    narrow = audit_element_properties(
        ifc4, global_ids=[wall.GlobalId], pset_names=["Pset_ServiceLife"]
    )
    assert [row["name"] for row in narrow["elements"][0]["psets"]] == ["Pset_ServiceLife"]


def test_a_selector_pages_and_reports_the_total(ifc4) -> None:
    report = audit_element_properties(ifc4, selector="IfcWall", max_elements=2)

    assert report["returned"] == 2
    assert report["total"] == 3
    assert report["truncated"] is True
    most = report["summary"]["most_missing"]
    assert most[0]["elements"] == 2
    assert all(item["elements"] <= 2 for item in most)


def test_unknown_ids_are_listed_not_raised(ifc4) -> None:
    report = audit_element_properties(
        ifc4, global_ids=["0000000000000000000000", _wall(ifc4, "Wall-1").GlobalId]
    )
    assert report["missing"] == ["0000000000000000000000"]
    assert report["returned"] == 1


def test_bad_arguments_raise_tool_errors(ifc4) -> None:
    with pytest.raises(ToolError) as no_scope:
        audit_element_properties(ifc4)
    assert no_scope.value.code == "INVALID_INPUT"
    with pytest.raises(ToolError) as bad_selector:
        audit_element_properties(ifc4, selector="IfcWall, [[")
    assert bad_selector.value.code == "INVALID_QUERY"
    with pytest.raises(ToolError):
        audit_element_properties(ifc4, selector="IfcWall", psets="bogus")
    with pytest.raises(ToolError):
        audit_element_properties(ifc4, selector="IfcWall", detail="bogus")


def test_the_index_walks_aggregates_to_the_storey(ifc4) -> None:
    import ifcopenshell.api.aggregate
    import ifcopenshell.api.root

    wall = _wall(ifc4, "Wall-1")
    part = ifcopenshell.api.root.create_entity(ifc4, ifc_class="IfcWall", name="Leaf")
    ifcopenshell.api.aggregate.assign_object(ifc4, products=[part], relating_object=wall)
    loose = ifcopenshell.api.root.create_entity(ifc4, ifc_class="IfcWall", name="Loose")

    index = PropertyIndex(ifc4)
    assert index.storey(part).Name == "Level 1"
    assert index.is_contained(part) is True
    assert index.storey(loose) is None
    assert index.is_contained(loose) is False


def test_core_set_names_climb_standard_case_classes() -> None:
    names = ["Pset_WallCommon", "Qto_WallBaseQuantities", "Pset_Condition"]
    assert core_set_names("IfcWallStandardCase", names) == (
        "Pset_WallCommon",
        "Qto_WallBaseQuantities",
    )
    assert core_set_names("IfcWall", ["Pset_Condition"]) == (None, None)


def test_ifc2x3_uses_its_own_templates(minimal_ifc4_path: Path) -> None:
    import ifcopenshell

    ifc = ifcopenshell.open(str(minimal_ifc4_path.parent / "minimal_ifc2x3.ifc"))
    report = audit_element_properties(ifc, selector="IfcWall", max_elements=1)

    assert report["templates"] == "IFC2X3"
    element = report["elements"][0]
    assert element["core_sets"]["pset"] == "Pset_WallCommon"
    assert element["summary"]["expected"] > 0
