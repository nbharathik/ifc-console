"""The edit trail of one model: what each step did and where the saved state sits."""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ifc_console.ifc.change_record import ChangeSummary

MAX_RECORDS = 100


@dataclass
class ChangeRecord:
    change_id: str
    at: str
    tool: str
    description: str
    summary: ChangeSummary = field(default_factory=ChangeSummary)
    # False once IfcOpenShell no longer holds the step (past the undo depth).
    undoable: bool = True

    @classmethod
    def new(cls, tool: str, description: str, summary: ChangeSummary) -> ChangeRecord:
        return cls(
            change_id=secrets.token_hex(3),
            at=datetime.now(timezone.utc).isoformat(),
            tool=tool,
            description=(description or "model edit").strip()[:200],
            summary=summary,
        )

    def event_fields(self, direction: str = "apply") -> dict[str, Any]:
        """What a live view needs to follow this step without a full reload.

        `direction` is "undo" when the step is being taken back, which shows the
        names as they were.
        """
        summary = self.summary
        fields: dict[str, Any] = {
            "change_id": self.change_id,
            "guids": list(summary.guids),
            "created_guids": list(summary.created_guids),
            "removed_guids": list(summary.removed_guids),
            "geometry": summary.geometry,
            "tree": summary.tree,
            "labels": summary.labels,
            "truncated": summary.truncated,
        }
        if summary.labels and not summary.truncated:
            names = summary.names_before if direction == "undo" else summary.names
            fields["names"] = dict(names)
        return fields

    def to_dict(self, *, detail: bool = False) -> dict[str, Any]:
        summary = self.summary
        out: dict[str, Any] = {
            "id": self.change_id,
            "at": self.at,
            "tool": self.tool,
            "description": self.description,
            "undoable": self.undoable,
            "created": summary.created,
            "edited": summary.edited,
            "removed": summary.removed,
            "geometry": summary.geometry,
        }
        if detail:
            out["classes"] = dict(summary.classes)
            out["guids"] = summary.guids[:20]
            out["guid_count"] = len(summary.guids)
        return out


class ChangeHistory:
    """Applied and undone steps, with positions counted from the model's load.

    The undo itself lives in IfcOpenShell's transaction log; this list says what
    each step was. Positions are absolute, so trimming old steps never moves
    the marker that says which step the file on disk holds.
    """

    def __init__(self) -> None:
        self.applied: list[ChangeRecord] = []
        self.undone: list[ChangeRecord] = []
        self._base = 0
        self._saved = 0

    @property
    def position(self) -> int:
        return self._base + len(self.applied)

    @property
    def unsaved(self) -> int:
        """Steps between the model and the file on disk, in either direction."""
        return abs(self.position - self._saved)

    @property
    def saved_index(self) -> int:
        """How many of the listed steps the file on disk includes (may fall outside)."""
        return self._saved - self._base

    @property
    def at_saved_state(self) -> bool:
        return self.position == self._saved

    @property
    def can_undo(self) -> bool:
        return bool(self.applied) and self.applied[-1].undoable

    @property
    def can_redo(self) -> bool:
        return bool(self.undone)

    def push(self, record: ChangeRecord, *, alive: int) -> None:
        """Add a step. `alive` is how many steps the transaction log still holds."""
        self.applied.append(record)
        self.undone.clear()
        while len(self.applied) > MAX_RECORDS:
            self.applied.pop(0)
            self._base += 1
        self.sync(alive)

    def undo(self, *, alive: int) -> ChangeRecord:
        record = self.applied.pop()
        self.undone.append(record)
        self.sync(alive)
        return record

    def redo(self, *, alive: int) -> ChangeRecord:
        record = self.undone.pop()
        self.applied.append(record)
        self.sync(alive)
        return record

    def sync(self, alive: int) -> None:
        cut = len(self.applied) - max(0, alive)
        for index, record in enumerate(self.applied):
            record.undoable = index >= cut

    def forget_undo(self) -> None:
        """Nothing before this point can be undone any more."""
        for record in self.applied:
            record.undoable = False
        self.undone.clear()

    def mark_saved(self) -> None:
        self._saved = self.position

    def recent(self, limit: int) -> list[str]:
        return [record.description for record in self.applied[-limit:]]
