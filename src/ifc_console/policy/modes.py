"""Session modes and the gate matrix.

Two modes, owned by the human: ask (default) lets the AI query but blocks
anything that would change the model; edit lets it mutate and save. Mode
changes only via the TUI / launch flags. There is deliberately no MCP tool
that can call set_mode. The two modes are compatibility profiles over typed
capabilities, so every adapter and future plugin can use the same vocabulary.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

from ifc_console.core.capabilities import (
    ASK_CAPABILITIES,
    CALLER_ONLY_CAPABILITIES,
    EDIT_CAPABILITIES,
    SYSTEM_CAPABILITIES,
    Authority,
    Capability,
    CapabilityDecision,
    normalize_capabilities,
)

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

    def granted_capabilities(self, *, authority: Authority = "tool") -> tuple[Capability, ...]:
        granted = set(ASK_CAPABILITIES if self.mode is Mode.ASK else EDIT_CAPABILITIES)
        if authority in ("caller", "worker"):
            granted.update(CALLER_ONLY_CAPABILITIES)
        if self.allow_system_access and self.mode is Mode.EDIT:
            granted.update(SYSTEM_CAPABILITIES)
        return normalize_capabilities(list(granted))

    def evaluate(
        self,
        required: tuple[Capability | str, ...] | list[Capability | str],
        *,
        authority: Authority = "tool",
    ) -> CapabilityDecision:
        requested = normalize_capabilities(required)
        granted = self.granted_capabilities(authority=authority)
        missing = tuple(item for item in requested if item not in granted)
        profile = f"{self.mode.value}:{authority}"
        if not missing:
            rule = "compatibility profile grants every requested capability"
        elif any(item in SYSTEM_CAPABILITIES for item in missing):
            rule = "system capabilities require edit mode and exec.allow_system_access"
        elif Capability.MODEL_APPROVE in missing:
            rule = "approval is reserved for an authenticated caller, never a tool"
        else:
            rule = f"the {self.mode.value} compatibility profile does not grant this capability"
        return CapabilityDecision(
            allowed=not missing,
            profile=profile,
            authority=authority,
            required=requested,
            granted=granted,
            missing=missing,
            rule=rule,
        )

    def require(
        self,
        required: tuple[Capability | str, ...] | list[Capability | str],
        *,
        authority: Authority = "tool",
        action: str = "operation",
    ) -> CapabilityDecision:
        from ifc_console.core.results import ToolError

        decision = self.evaluate(required, authority=authority)
        if decision.allowed:
            return decision
        missing = ", ".join(item.value for item in decision.missing)
        edit_only = {
            Capability.MODEL_MUTATE,
            Capability.MODEL_COMMIT,
            Capability.MODEL_RESTORE,
        }
        if self.mode is Mode.ASK and any(item in edit_only for item in decision.missing):
            raise ToolError(
                "ASK_MODE_BLOCKED",
                f"{action} is unavailable while the session is in ask mode.",
                "The user must switch to edit mode explicitly, then retry.",
                data={"missing_capabilities": [item.value for item in decision.missing]},
            )
        raise ToolError(
            "CAPABILITY_DENIED",
            f"{action} requires capabilities that this profile does not grant: {missing}.",
            decision.rule,
            data={
                "profile": decision.profile,
                "missing_capabilities": [item.value for item in decision.missing],
            },
        )

    def is_escalation(self, new_mode: Mode) -> bool:
        return _ESCALATION_ORDER[new_mode] > _ESCALATION_ORDER[self.mode]

    def set_mode(self, new_mode: Mode, *, by: str) -> Mode:
        old, self.mode = self.mode, new_mode
        if self._audit:
            self._audit.record("mode_change", old=old.value, new=new_mode.value, by=by)
        if self._events:
            self._events.emit("mode_changed", mode=new_mode.value, old=old.value, by=by)
        return old
