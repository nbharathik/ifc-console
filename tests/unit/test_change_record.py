"""A finished transaction says what changed, and what a viewer has to redraw."""

from __future__ import annotations

import ifcopenshell
import ifcopenshell.api
import numpy as np
import pytest

from ifc_console.ifc.change_record import summarize, verify_rollback


@pytest.fixture
def model():
    ifc = ifcopenshell.api.run("project.create_file", version="IFC4")
    project = ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcProject", name="P")
    ifcopenshell.api.run("unit.assign_unit", ifc)
    site = ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcSite", name="S")
    storey = ifcopenshell.api.run(
        "root.create_entity", ifc, ifc_class="IfcBuildingStorey", name="L1"
    )
    ifcopenshell.api.run("aggregate.assign_object", ifc, products=[site], relating_object=project)
    ifcopenshell.api.run("aggregate.assign_object", ifc, products=[storey], relating_object=site)
    wall = ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name="W")
    ifcopenshell.api.run(
        "spatial.assign_container", ifc, products=[wall], relating_structure=storey
    )
    ifc.set_history_size(20)
    return ifc, wall


def _step(ifc, edit):
    ifc.begin_transaction()
    transaction = ifc.transaction
    edit()
    ifc.end_transaction()
    return transaction


def test_a_property_edit_touches_the_owner_and_no_geometry(model) -> None:
    ifc, wall = model

    def edit() -> None:
        pset = ifcopenshell.api.run("pset.add_pset", ifc, product=wall, name="Pset_WallCommon")
        ifcopenshell.api.run("pset.edit_pset", ifc, pset=pset, properties={"FireRating": "F30"})

    summary = summarize(ifc, _step(ifc, edit))

    assert summary.guids == [wall.GlobalId]
    assert summary.geometry is False
    assert summary.tree is False
    assert summary.created > 0
    assert "IfcPropertySingleValue" in summary.classes


def test_renaming_an_object_asks_for_labels_only(model) -> None:
    ifc, wall = model

    summary = summarize(ifc, _step(ifc, lambda: setattr(wall, "Name", "Renamed")))

    assert summary.labels is True
    assert summary.geometry is False
    assert summary.tree is False
    assert summary.guids == [wall.GlobalId]
    assert (summary.created, summary.edited, summary.removed) == (0, 1, 0)


def test_creating_a_product_changes_the_tree_and_geometry(model) -> None:
    ifc, _wall = model
    made = []

    def edit() -> None:
        made.append(
            ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name="Second")
        )

    summary = summarize(ifc, _step(ifc, edit))

    assert summary.tree is True
    assert summary.geometry is True
    assert summary.created_guids == [made[0].GlobalId]


def test_removing_a_product_lists_its_guid(model) -> None:
    ifc, wall = model
    guid = wall.GlobalId

    summary = summarize(
        ifc, _step(ifc, lambda: ifcopenshell.api.run("root.remove_product", ifc, product=wall))
    )

    assert summary.removed_guids == [guid]
    assert summary.tree is True
    assert summary.geometry is True


def test_moving_an_object_is_a_geometry_change(model) -> None:
    ifc, wall = model

    def edit() -> None:
        matrix = np.eye(4)
        matrix[0, 3] = 2.0
        ifcopenshell.api.run("geometry.edit_object_placement", ifc, product=wall, matrix=matrix)

    summary = summarize(ifc, _step(ifc, edit))

    assert summary.geometry is True
    assert wall.GlobalId in summary.guids


def test_an_empty_transaction_reports_nothing(model) -> None:
    ifc, _wall = model

    summary = summarize(ifc, _step(ifc, lambda: None))

    assert summary.ops == 0
    assert summary.guids == []
    assert summary.geometry is False


def test_the_guid_list_is_capped_and_says_so(model) -> None:
    ifc, _wall = model
    walls = [
        ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name=f"w{i}")
        for i in range(5)
    ]

    def edit() -> None:
        for wall in walls:
            wall.Name = "x"

    summary = summarize(ifc, _step(ifc, edit), max_guids=3)

    assert len(summary.guids) == 3
    assert summary.truncated is True


def test_a_clean_rollback_verifies(model) -> None:
    ifc, wall = model
    ifc.begin_transaction()
    transaction = ifc.transaction
    wall.Name = "Changed"
    ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name="Extra")
    ifc.discard_transaction()

    assert wall.Name == "W"
    assert verify_rollback(ifc, transaction) is True


def test_a_rollback_that_left_something_behind_does_not_verify(model) -> None:
    ifc, wall = model
    ifc.begin_transaction()
    transaction = ifc.transaction
    wall.Name = "Changed"
    ifc.discard_transaction()
    wall.Name = "Tampered"  # a rollback that did not hold

    assert verify_rollback(ifc, transaction) is False
