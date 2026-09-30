"""The edit trail tracks the saved state through undo, redo and trimming."""

from __future__ import annotations

from ifc_console.ifc.change_record import ChangeSummary
from ifc_console.session import history as history_module
from ifc_console.session.history import ChangeHistory, ChangeRecord


def _record(name: str = "edit") -> ChangeRecord:
    return ChangeRecord.new("execute_ifc_code", name, ChangeSummary(edited=1))


def test_a_new_history_is_at_its_saved_state() -> None:
    history = ChangeHistory()

    assert history.at_saved_state
    assert history.unsaved == 0
    assert not history.can_undo and not history.can_redo


def test_steps_count_until_the_model_is_saved() -> None:
    history = ChangeHistory()
    history.push(_record("a"), alive=1)
    history.push(_record("b"), alive=2)

    assert history.unsaved == 2
    assert not history.at_saved_state

    history.mark_saved()

    assert history.unsaved == 0
    assert history.at_saved_state
    assert history.can_undo


def test_undoing_back_to_the_saved_step_is_clean_again() -> None:
    history = ChangeHistory()
    history.push(_record("a"), alive=1)
    history.mark_saved()
    history.push(_record("b"), alive=2)

    undone = history.undo(alive=1)

    assert undone.description == "b"
    assert history.at_saved_state
    assert history.can_redo


def test_undoing_past_the_saved_step_is_dirty_in_the_other_direction() -> None:
    history = ChangeHistory()
    history.push(_record("a"), alive=1)
    history.mark_saved()

    history.undo(alive=0)

    assert not history.at_saved_state
    assert history.unsaved == 1


def test_redo_replays_the_step_and_a_new_edit_drops_the_redo_stack() -> None:
    history = ChangeHistory()
    history.push(_record("a"), alive=1)
    history.undo(alive=0)
    assert history.redo(alive=1).description == "a"

    history.undo(alive=0)
    history.push(_record("b"), alive=1)

    assert not history.can_redo
    assert [r.description for r in history.applied] == ["b"]


def test_steps_beyond_the_undo_depth_are_marked_not_undoable() -> None:
    history = ChangeHistory()
    for name in ("a", "b", "c"):
        history.push(_record(name), alive=2)

    assert [r.undoable for r in history.applied] == [False, True, True]
    assert history.can_undo


def test_when_the_log_holds_nothing_there_is_nothing_to_undo() -> None:
    history = ChangeHistory()
    history.push(_record("a"), alive=0)

    assert not history.can_undo


def test_forgetting_undo_keeps_the_records_but_not_the_ability() -> None:
    history = ChangeHistory()
    history.push(_record("a"), alive=1)
    history.forget_undo()

    assert history.applied and not history.can_undo


def test_trimming_old_records_keeps_the_saved_marker_honest(monkeypatch) -> None:
    monkeypatch.setattr(history_module, "MAX_RECORDS", 3)
    history = ChangeHistory()
    history.push(_record("a"), alive=3)
    history.mark_saved()
    for name in ("b", "c", "d"):
        history.push(_record(name), alive=3)

    assert len(history.applied) == 3
    assert history.position == 4
    assert history.unsaved == 3
    assert history.recent(2) == ["c", "d"]
