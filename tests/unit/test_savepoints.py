"""An edit's own IfcOpenShell transaction survives APIs that open transactions of their own."""

from __future__ import annotations

import ifcopenshell
import ifcopenshell.api
import pytest

from ifc_console.policy.guards import GuardError
from ifc_console.policy.savepoints import savepoints


@pytest.fixture
def model():
    ifc = ifcopenshell.api.run("project.create_file", version="IFC4")
    ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcProject", name="P")
    ifc.set_history_size(20)
    return ifc


def test_a_nested_discard_rolls_back_only_its_own_work(model) -> None:
    ifc = model
    ifc.begin_transaction()
    host = ifc.transaction
    kept = ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name="kept")

    with savepoints(ifc, host):
        ifc.begin_transaction()
        ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name="temp")
        ifc.discard_transaction()
        assert ifc.transaction is host

    ifc.end_transaction()

    assert [w.Name for w in ifc.by_type("IfcWall")] == ["kept"]
    assert len(ifc.history) == 1
    assert kept.Name == "kept"


def test_a_nested_end_keeps_the_work_in_the_host_transaction(model) -> None:
    ifc = model
    ifc.begin_transaction()
    host = ifc.transaction

    with savepoints(ifc, host):
        ifc.begin_transaction()
        ifcopenshell.api.run("root.create_entity", ifc, ifc_class="IfcWall", name="inner")
        ifc.end_transaction()
        assert ifc.transaction is host

    ifc.discard_transaction()

    assert ifc.by_type("IfcWall") == []


def test_undo_and_redo_are_refused_inside_a_run(model) -> None:
    ifc = model
    ifc.begin_transaction()
    with savepoints(ifc, ifc.transaction):
        with pytest.raises(GuardError, match="/undo"):
            ifc.undo()
        with pytest.raises(GuardError, match="/redo"):
            ifc.redo()
    ifc.discard_transaction()


def test_the_file_gets_its_own_methods_back(model) -> None:
    ifc = model
    ifc.begin_transaction()
    with savepoints(ifc, ifc.transaction):
        assert "begin_transaction" in ifc.__dict__
    ifc.discard_transaction()

    assert not any(
        name in ifc.__dict__
        for name in ("begin_transaction", "end_transaction", "discard_transaction", "undo", "redo")
    )
    ifc.begin_transaction()
    assert ifc.transaction is not None
    ifc.discard_transaction()
