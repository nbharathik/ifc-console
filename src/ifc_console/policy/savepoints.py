"""Nested transactions inside one guarded edit.

An edit runs as a single IfcOpenShell transaction. A few IfcOpenShell APIs open,
discard or end transactions of their own to throw temporary entities away, which
would replace or drop the host's transaction. For the length of the run the file
therefore answers those calls with savepoints on the host transaction, and
refuses undo and redo, which belong to the user.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from ifc_console.policy.guards import GuardError

_PATCHED = ("begin_transaction", "end_transaction", "discard_transaction", "undo", "redo")

UNDO_REFUSED = (
    "undo and redo are the user's to run: ask them to use /undo or /redo in the "
    "ifc-console terminal."
)


@contextlib.contextmanager
def savepoints(ifc: Any, transaction: Any) -> Iterator[None]:
    """Route this file's transaction calls to savepoints on `transaction`."""
    from ifcopenshell.file import Transaction

    marks: list[int] = []

    def begin_transaction() -> None:
        marks.append(len(transaction.operations))

    def end_transaction() -> None:
        if marks:
            marks.pop()

    def discard_transaction() -> None:
        if not marks:
            return
        start = marks.pop()
        inner = Transaction(ifc)
        inner.operations = transaction.operations[start:]
        del transaction.operations[start:]
        inner.rollback()

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise GuardError(UNDO_REFUSED)

    patches = {
        "begin_transaction": begin_transaction,
        "end_transaction": end_transaction,
        "discard_transaction": discard_transaction,
        "undo": refuse,
        "redo": refuse,
    }
    for name, function in patches.items():
        setattr(ifc, name, function)
    try:
        yield
    finally:
        for name in _PATCHED:
            ifc.__dict__.pop(name, None)
