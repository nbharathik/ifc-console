"""set_properties: structured property edits, applied whole or not at all.

The narrow, checkable way to edit. `execute_ifc_code` can do anything; this can
only set single-value properties on named elements, so its effect is small
enough to preview (`dry_run`) and to state exactly in the change record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.application.operations import enveloped
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.ifc import property_edit
from ifc_console.policy.modes import OpClass, Verdict

if TYPE_CHECKING:
    from ifc_console.app import AppCore

SET_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

MAX_ENTRIES = 50
MAX_TARGETS = 2000
MAX_ROWS = 100

_DESCRIPTION = (
    "[EDIT] Set property values on elements by GlobalId, all or nothing: if any "
    "entry is invalid nothing changes. Each entry names global_ids, a pset, a "
    "property and a value. An existing property keeps its IFC type; a new one "
    "takes `type` or the type its value implies (IfcLabel, IfcReal, IfcInteger, "
    "IfcBoolean). Missing property sets are created on the element. "
    "dry_run=true returns the same rows and changes nothing. One call is one "
    "undo step for the user (/undo). Edit mode only."
)


class PropertyChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_ids: Annotated[
        list[str], Field(min_length=1, max_length=500, description="Occurrence GlobalIds.")
    ]
    pset: Annotated[str, Field(min_length=1, max_length=120, description="e.g. Pset_WallCommon.")]
    property: Annotated[str, Field(min_length=1, max_length=120, description="e.g. FireRating.")]
    value: Annotated[
        str | int | float | bool | None,
        Field(description="Text, number or boolean; null clears an existing value."),
    ]
    type: Annotated[
        str | None,
        Field(max_length=60, description="IFC value type, e.g. IfcLengthMeasure. Optional."),
    ] = None


def _apply(ifc: Any, changes: list[PropertyChange]) -> list[dict[str, Any]]:
    """Every change, in order, on the model worker. Raises on the first problem."""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for change in changes:
        pset_name = change.pset.strip()
        property_name = change.property.strip()
        value = property_edit.scalar(change.value)
        for global_id in dict.fromkeys(item.strip() for item in change.global_ids):
            key = (global_id, pset_name, property_name)
            if key in seen:
                raise ToolError(
                    "INVALID_INPUT",
                    f"{pset_name}.{property_name} is set twice for {global_id}.",
                    "Give each element, property set and property once per call.",
                )
            seen.add(key)
            element = property_edit.find_element(ifc, global_id)
            pset = property_edit.find_pset(element, pset_name)
            prop = property_edit.find_property(pset, property_name) if pset is not None else None
            row: dict[str, Any] = {
                "global_id": global_id,
                "pset": pset_name,
                "property": property_name,
            }
            try:
                if prop is not None:
                    if prop.NominalValue is None:
                        before = None
                        nominal_type = change.type or property_edit.infer_nominal_type(value)
                    else:
                        nominal_type, before = property_edit.read_nominal(prop)
                        nominal_type = change.type or nominal_type
                    if before is not None and property_edit.same(before, value):
                        row.update(before=before, after=before, action="unchanged")
                    else:
                        after = property_edit.assign(ifc, prop, nominal_type, value)
                        row.update(
                            before=before,
                            after=after,
                            action="unchanged" if property_edit.same(before, after) else "updated",
                        )
                else:
                    nominal_type = change.type or property_edit.infer_nominal_type(value)
                    created_pset = pset is None
                    _, _, after = property_edit.create_property(
                        ifc, element, pset, pset_name, property_name, nominal_type, value
                    )
                    row.update(before=None, after=after, action="created")
                    if created_pset:
                        row["pset_created"] = True
            except ToolError:
                raise
            except Exception as exc:
                raise ToolError(
                    "INVALID_INPUT",
                    f"{global_id}: {type(exc).__name__}: {exc}",
                    "Check that the GlobalId names an object that can carry property sets.",
                ) from exc
            rows.append(row)
    return rows


def _as_input_error(exc: ToolError) -> ToolError:
    """The editors share code with the ChangeSet worker, which has its own code."""
    if exc.code != property_edit.INVALID:
        return exc
    return ToolError("INVALID_INPUT", exc.message, exc.hint, data=exc.data)


def register(mcp: OperationRegistry, core: AppCore) -> None:
    @mcp.tool(
        annotations=SET_ANN,
        description=_DESCRIPTION,
        required_capabilities=(Capability.MODEL_MUTATE,),
    )
    @enveloped(core, "set_properties")
    @core.active_model_operation
    async def set_properties(
        changes: Annotated[
            list[PropertyChange],
            Field(min_length=1, max_length=MAX_ENTRIES, description="The edits to apply."),
        ],
        dry_run: Annotated[bool, Field(description="Report the rows without changing anything.")] = False,
        description: Annotated[
            str, Field(max_length=200, description="One-line intent, shown to the user.")
        ] = "",
    ) -> Envelope:
        session = core.session
        if core.policy.decide(OpClass.EDIT) is Verdict.DENY_ASK:
            raise ToolError(
                "ASK_MODE_BLOCKED",
                "set_properties is unavailable while the session is in ask mode.",
                "Ask the user to run /mode edit in the ifc-console terminal, then retry.",
            )
        session.require_writable()
        targets = sum(len(dict.fromkeys(item.strip() for item in c.global_ids)) for c in changes)
        if targets > MAX_TARGETS:
            raise ToolError(
                "INVALID_INPUT",
                f"{targets} targets is over the limit of {MAX_TARGETS} per call.",
                "Split the work into several calls.",
            )
        intent = description or f"set properties on {targets} target(s)"
        async with session.edit_lock:
            if core.policy.decide(OpClass.EDIT) is Verdict.DENY_ASK:
                raise ToolError(
                    "ASK_MODE_BLOCKED",
                    "the session went back to ask mode while this call waited.",
                    "Ask the user to run /mode edit in the ifc-console terminal, then retry.",
                )

            def job():
                return session.mutate(
                    lambda: _apply(session.ifc, changes),
                    tool="set_properties",
                    description=intent,
                    dry_run=dry_run,
                )

            try:
                rows, outcome = await session.run(
                    job,
                    timeout=core.settings.exec.edit_timeout_seconds,
                    timeout_code="EXEC_TIMEOUT",
                )
            except ToolError as exc:
                failure = getattr(exc, "mutation_outcome", None)
                rolled = bool(failure and failure.rolled_back and failure.verified)
                mapped = _as_input_error(exc)
                mapped.data = {**(mapped.data or {}), "rolled_back": rolled}
                core.audit.record(
                    "set_properties", ok=False, dry_run=dry_run, targets=targets, error=mapped.code
                )
                if mapped is exc:
                    raise
                raise mapped from exc

        record = outcome.record
        counts: dict[str, int] = {"targets": len(rows)}
        for row in rows:
            counts[row["action"]] = counts.get(row["action"], 0) + 1
        counts["property_sets_created"] = sum(1 for row in rows if row.get("pset_created"))
        core.audit.record(
            "set_properties",
            ok=True,
            dry_run=dry_run,
            targets=targets,
            change_id=record.change_id if record else None,
            **{key: value for key, value in counts.items() if key != "targets"},
        )
        if record is not None:
            core.events.emit(
                "model_mutated",
                tool="set_properties",
                description=intent,
                changes=session.change_count,
                **record.event_fields(),
            )
        data: dict[str, Any] = {
            "applied": record is not None,
            "dry_run": dry_run,
            "counts": counts,
            "rows": rows[:MAX_ROWS],
            "rows_truncated": len(rows) > MAX_ROWS,
        }
        if record is not None:
            data["change"] = record.to_dict()
        elif dry_run:
            data["note"] = "dry run: the model was not changed"
            data["verified"] = outcome.verified
        else:
            data["note"] = "every property already had that value; nothing changed"
        return ok(data, core.session_meta(), char_limit=lambda: core.settings.exec.output_char_limit)
