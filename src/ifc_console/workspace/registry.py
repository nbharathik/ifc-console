"""ModelRegistry: the resident models and the attached companion files.

Exactly one model is active, and the active model is the only writable one.
Everything else is held read-only, evicted LRU when the budget demands it,
and never evicted while it has unsaved changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ifc_console.core.results import ToolError
from ifc_console.session.model import ModelSession
from ifc_console.workspace.index import slug


@dataclass
class Attachment:
    """A companion file the LLM should know about, for example an IDS."""

    alias: str
    path: Path
    kind: str
    consumed_by: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = {
            "alias": self.alias,
            "path": str(self.path),
            "name": self.path.name,
            "kind": self.kind,
            "consumed_by": self.consumed_by,
        }
        row.update(self.detail)
        return row


class ModelRegistry:
    def __init__(self, *, max_resident: int = 3, max_total_mb: int = 6144) -> None:
        self.max_resident = max(1, max_resident)
        self.max_total_mb = max(0, max_total_mb)
        self.sessions: dict[str, ModelSession] = {}
        self.attachments: dict[str, Attachment] = {}
        self.active_id: str | None = None
        # A never-loaded session so `core.session` is always a ModelSession,
        # exactly as it was before the registry existed.
        self._idle = ModelSession()
        self._order: list[str] = []  # LRU, most recently used last

    # -- access ---------------------------------------------------------------
    @property
    def active(self) -> ModelSession:
        if self.active_id is None:
            return self._idle
        return self.sessions[self.active_id]

    def get(self, model_id: str) -> ModelSession | None:
        return self.sessions.get(model_id)

    def require(self, model_id: str) -> ModelSession:
        session = self.sessions.get(model_id)
        if session is None:
            known = ", ".join(sorted(self.sessions)) or "(none)"
            raise ToolError(
                "MODEL_NOT_FOUND",
                f"no resident model with id {model_id!r}.",
                f"Resident models: {known}. Call list_models, or attach the file first.",
            )
        self.touch(model_id)
        return session

    def touch(self, model_id: str) -> None:
        if model_id in self._order:
            self._order.remove(model_id)
        self._order.append(model_id)

    @property
    def attached_ids(self) -> list[str]:
        return [mid for mid in self.sessions if mid != self.active_id]

    def resident_bytes(self) -> int:
        return sum(s.size_bytes for s in self.sessions.values())

    # -- ids ------------------------------------------------------------------
    def make_id(self, path: Path, alias: str | None = None) -> str:
        base = slug(alias or path.stem)
        occupied = self.sessions.keys() | self.attachments.keys()
        if base not in occupied:
            return base
        n = 2
        while f"{base}-{n}" in occupied:
            n += 1
        return f"{base}-{n}"

    def id_for_path(self, path: Path) -> str | None:
        for model_id, session in self.sessions.items():
            if session.path == path:
                return model_id
        return None

    # -- budget ---------------------------------------------------------------
    def _evictable(self) -> list[str]:
        """Clean, non-active models, least recently used first."""
        order = [mid for mid in self._order if mid in self.sessions]
        order += [mid for mid in self.sessions if mid not in order]
        return [
            mid
            for mid in order
            if mid != self.active_id
            and not self.sessions[mid].dirty
            and not self.sessions[mid].poisoned
        ]

    def plan_room(self, incoming_bytes: int, *, replacing: Iterable[str] = ()) -> list[str]:
        """Return the clean models to evict so a newcomer fits.

        This is a dry run. Existing sessions remain intact when the request
        cannot fit or the caller later fails to load the new model.
        """
        replacing_ids = {mid for mid in replacing if mid in self.sessions}
        remaining = set(self.sessions) - replacing_ids
        evicted: list[str] = []
        budget = self.max_total_mb * 1_048_576

        def over_capacity() -> bool:
            return len(remaining) + 1 > self.max_resident

        def over_budget() -> bool:
            resident = sum(self.sessions[mid].size_bytes for mid in remaining)
            return bool(budget) and resident + incoming_bytes > budget

        while over_capacity() or over_budget():
            candidates = [mid for mid in self._evictable() if mid in remaining]
            if not candidates:
                break
            victim = candidates[0]
            evicted.append(victim)
            remaining.remove(victim)
        if over_capacity():
            raise ToolError(
                "WORKSPACE_BUDGET",
                f"{len(remaining)} models would remain and none can be freed "
                f"(limit {self.max_resident}).",
                "Detach one with detach, or ask the user to raise "
                "workspace.max_resident in the ifc-console terminal.",
            )
        if over_budget():
            resident = sum(self.sessions[mid].size_bytes for mid in remaining)
            mb = (resident + incoming_bytes) / 1_048_576
            raise ToolError(
                "WORKSPACE_BUDGET",
                f"loading this model would hold {mb:.0f} MB, over the "
                f"{self.max_total_mb} MB workspace budget.",
                "Detach a model with detach, or ask the user to raise "
                "workspace.max_total_mb in the ifc-console terminal.",
            )
        return evicted

    def make_room(self, incoming_bytes: int) -> list[str]:
        """Evict clean models until a newcomer fits. Returns evicted ids."""
        evicted = self.plan_room(incoming_bytes)
        for model_id in evicted:
            self.drop(model_id)
        return evicted

    # -- lifecycle ------------------------------------------------------------
    def add(self, model_id: str, session: ModelSession, *, active: bool) -> str:
        session.model_id = model_id
        self.sessions[model_id] = session
        self.touch(model_id)
        if active:
            self.set_active(model_id)
        else:
            session.read_only = True
        return model_id

    def set_active(self, model_id: str) -> None:
        if model_id not in self.sessions:
            self.require(model_id)
        previous = self.active_id
        if self.sessions[model_id].poisoned or (
            previous is not None
            and previous != model_id
            and self.sessions[previous].poisoned
        ):
            raise ToolError(
                "MODEL_BUSY",
                "a cancelled or timed-out model operation is still fenced.",
                "Reload that model before changing the active model.",
            )
        if previous is not None and previous in self.sessions:
            self.sessions[previous].read_only = True
        self.active_id = model_id
        self.sessions[model_id].read_only = False
        self.touch(model_id)

    def drop(self, model_id: str, *, force: bool = False) -> ModelSession:
        """Free a resident model. force discards unsaved changes, which only
        the replace-the-active-model path does (the user confirmed it there)."""
        session = self.require(model_id)
        if session.poisoned:
            raise ToolError(
                "MODEL_BUSY",
                f"{model_id} has a cancelled or timed-out operation still fenced.",
                "Reload the model before detaching or replacing it.",
            )
        if session.dirty and not force:
            raise ToolError(
                "UNSAVED_CHANGES",
                f"{model_id} has unsaved changes.",
                "Save it first (make it active with set_active_model, then "
                "save_ifc_file), or ask the user to discard them with /reload.",
            )
        del self.sessions[model_id]
        if model_id in self._order:
            self._order.remove(model_id)
        session.close()
        if self.active_id == model_id:
            self.active_id = None
            remaining = [mid for mid in reversed(self._order) if mid in self.sessions]
            if remaining:
                self.set_active(remaining[0])
        return session

    def close_all(self) -> None:
        for session in self.sessions.values():
            session.close()
        self.sessions.clear()
        self._order.clear()
        self.active_id = None
        self._idle.close()

    # -- attachments ----------------------------------------------------------
    def attach_file(self, attachment: Attachment) -> Attachment:
        self.attachments[attachment.alias] = attachment
        return attachment

    def detach_file(self, alias: str) -> Attachment:
        attachment = self.attachments.pop(alias, None)
        if attachment is None:
            known = ", ".join(sorted(self.attachments)) or "(none)"
            raise ToolError(
                "NOT_FOUND",
                f"no attached file with alias {alias!r}.",
                f"Attached files: {known}. Call list_models to see them.",
            )
        return attachment

    def unique_attachment_alias(self, path: Path, alias: str | None = None) -> str:
        # Models and attachments share one id space, so this is make_id.
        return self.make_id(path, alias)

    # -- reporting ------------------------------------------------------------
    def model_rows(self) -> list[dict[str, Any]]:
        rows = []
        for model_id, session in self.sessions.items():
            rows.append(
                {
                    "model_id": model_id,
                    "name": session.name,
                    "path": str(session.path) if session.path else None,
                    "schema": session.schema,
                    "size_bytes": session.size_bytes,
                    "active": model_id == self.active_id,
                    "writable": not session.read_only,
                    "dirty": session.dirty,
                    "fingerprint": session.fingerprint,
                }
            )
        rows.sort(key=lambda r: (not r["active"], r["model_id"]))
        return rows

    def meta_extras(self) -> dict[str, Any]:
        """Additive meta keys: only present once more than one file is in play."""
        extras: dict[str, Any] = {}
        if self.active_id is not None:
            extras["model_id"] = self.active_id
        if len(self.sessions) > 1:
            extras["models"] = len(self.sessions)
        if self.attachments:
            extras["attachments"] = len(self.attachments)
        return extras
