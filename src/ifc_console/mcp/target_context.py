"""Copyable model/selection evidence for analysis and later guarded execution."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.core.results import ToolError

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.session.model import ModelSession


class SelectionContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str | None
    client_id: int | None
    global_ids: list[str] = Field(max_length=500)


class TargetContext(BaseModel):
    """A precondition, never permission to edit or a restriction on Python code."""

    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    revision: int = Field(ge=0)
    global_ids: list[str] = Field(default_factory=list, max_length=500)
    selection: SelectionContext | None = None


def model_revision(session: ModelSession) -> dict[str, Any]:
    return {
        "model_id": session.model_id,
        "fingerprint": session.fingerprint,
        "revision": session.revision,
    }


def selection_context(core: AppCore) -> dict[str, Any]:
    hub = core.viewer_hub
    return {
        "model_id": hub.selection_model_id,
        "client_id": hub.selection_client_id,
        "global_ids": list(hub.selection),
    }


def target_context(session: ModelSession, global_ids: list[str] | None = None) -> dict[str, Any]:
    return {**model_revision(session), "global_ids": list(dict.fromkeys(global_ids or []))}


def execution_context(session: ModelSession, expected: TargetContext | None) -> dict[str, Any]:
    context = target_context(session, expected.global_ids if expected else None)
    if expected is not None and expected.selection is not None:
        # Retain the reviewed selection through a batch. Replacing it with the
        # newest click would silently authorize a changed selection on retry.
        context["selection"] = expected.selection.model_dump()
    return context


def require_target_context(core: AppCore, expected: TargetContext) -> None:
    """Check on the serialized model worker immediately before running code.

    The active-model lifecycle lock is owned by execute_ifc_code. Selection
    is checked at execution start; a later click does not cancel a running edit.
    """
    session = core.session
    current = target_context(session, expected.global_ids)
    mismatches = [
        name
        for name in ("model_id", "fingerprint", "revision")
        if current[name] != getattr(expected, name)
    ]
    if expected.selection is not None:
        actual_selection = selection_context(core)
        current["selection"] = actual_selection
        previous = expected.selection.model_dump()
        # Selection ordering is presentation, not a change in intended objects.
        if any(
            actual_selection[name] != previous[name] for name in ("model_id", "client_id")
        ) or set(actual_selection["global_ids"]) != set(previous["global_ids"]):
            mismatches.append("selection")
    if mismatches:
        raise ToolError(
            "REVISION_CONFLICT",
            "analysis context changed: " + ", ".join(mismatches) + "; no code was run.",
            "Re-read get_viewer_selection and get_element for the original GlobalIds. "
            "Resolve the changed target explicitly and revalidate affected measurements; "
            "do not replace expected_context just to retry stale edits.",
            data={
                "expected_context": expected.model_dump(exclude_none=True),
                "current_context": current,
                "mismatches": mismatches,
                "executed": False,
            },
        )
    missing = []
    for gid in expected.global_ids:
        try:
            element = session.ifc.by_guid(gid)
        except (RuntimeError, ValueError):
            element = None
        if element is None:
            missing.append(gid)
    if missing:
        raise ToolError(
            "NOT_FOUND",
            "one or more analysis targets no longer exist; no code was run.",
            "Read get_element for these GlobalIds and resolve the intended target before writing.",
            data={"missing": missing, "current_context": current, "executed": False},
        )
