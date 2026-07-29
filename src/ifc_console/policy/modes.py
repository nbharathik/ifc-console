"""Session modes and the gate matrix.

Two modes, owned by the human: ask (default) lets the AI query but blocks
anything that would change the model or write files; edit lets it mutate
and save. Mode changes only via the TUI / launch flags. There is
deliberately no MCP tool that can call set_mode. Finer-grained permission
prompts belong to the AI client, not this server.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ifc_console.audit import AuditLog
    from ifc_console.events import EventBus


class Mode(str, enum.Enum):
    ASK = "ask"
    EDIT = "edit"


class OpClass(str, enum.Enum):
    QUERY = "QUERY"
    VIEW = "VIEW"
    # Writes an output file (report, export); never touches the model, so it
    # is allowed in ask mode. Still allowed-dir checked and audited.
    ARTIFACT = "ARTIFACT"
    EDIT = "EDIT"
    SYSTEM = "SYSTEM"


class Verdict(str, enum.Enum):
    ALLOW = "allow"
    DENY_ASK = "deny_ask"        # EDIT/SYSTEM code while the session is in ask mode
    DENY_SYSTEM = "deny_system"  # SYSTEM code while exec.allow_system_access is false


_ESCALATION_ORDER = {Mode.ASK: 0, Mode.EDIT: 1}


class PolicyEngine:
    def __init__(
        self,
        mode: Mode,
        *,
        allow_system_access: bool,
        events: EventBus | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.mode = mode
        self.allow_system_access = allow_system_access
        self._events = events
        self._audit = audit

    # -- gate matrix --------------------------------------------------------
    def decide(self, op_class: OpClass) -> Verdict:
        if op_class in (OpClass.QUERY, OpClass.VIEW, OpClass.ARTIFACT):
            return Verdict.ALLOW
        if self.mode is Mode.ASK:
            return Verdict.DENY_ASK
        if op_class is OpClass.SYSTEM and not self.allow_system_access:
            return Verdict.DENY_SYSTEM
        return Verdict.ALLOW

    def is_escalation(self, new_mode: Mode) -> bool:
        return _ESCALATION_ORDER[new_mode] > _ESCALATION_ORDER[self.mode]

    def set_mode(self, new_mode: Mode, *, by: str) -> Mode:
        old, self.mode = self.mode, new_mode
        if self._audit:
            self._audit.record("mode_change", old=old.value, new=new_mode.value, by=by)
        if self._events:
            self._events.emit("mode_changed", mode=new_mode.value, old=old.value, by=by)
        return old
