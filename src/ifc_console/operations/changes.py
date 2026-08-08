"""Safe preview operations for structured property changes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.changes import IfcScalar
from ifc_console.core.operation_data import ChangeSetData
from ifc_console.core.operations import OperationAnnotations, OperationRegistry
from ifc_console.core.results import Envelope, ok

if TYPE_CHECKING:
    from ifc_console.app import AppCore

PREVIEW_ANN = OperationAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
READ_ANN = OperationAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def register(registry: OperationRegistry, core: AppCore) -> None:
    char_limit = core.settings.exec.output_char_limit

    @registry.tool(
        annotations=PREVIEW_ANN,
        data_model=ChangeSetData,
        description=(
            "[PREVIEW] Build a revision-bound ChangeSet for existing occurrence-level "
            "IfcPropertySingleValue values. The live model and source IFC are untouched. "
            "This writes a local preview artifact but cannot approve or commit it. "
            "Approval and commit remain explicit SDK or CLI caller actions."
        ),
    )
    @enveloped(core, "preview_property_change")
    async def preview_property_change(
        global_ids: Annotated[list[str], Field(min_length=1, max_length=500)],
        pset_name: Annotated[str, Field(min_length=1, max_length=255)],
        property_name: Annotated[str, Field(min_length=1, max_length=255)],
        value: IfcScalar,
        expected_revision: str | None = None,
    ) -> Envelope:
        record = await core.transactions.preview_property_value(
            global_ids=global_ids,
            pset_name=pset_name,
            property_name=property_name,
            value=value,
            expected_revision=expected_revision,
        )
        return ok(
            {"change_set": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )

    @registry.tool(
        annotations=READ_ANN,
        data_model=ChangeSetData,
        description=(
            "[PREVIEW] Read a verified ChangeSet artifact. This cannot approve, commit, "
            "or restore model bytes."
        ),
    )
    @enveloped(core, "get_change_set")
    async def get_change_set(change_set_id: str) -> Envelope:
        record = core.transactions.get_change_set(change_set_id)
        return ok(
            {"change_set": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )
