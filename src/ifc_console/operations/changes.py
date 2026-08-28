"""Safe preview operations for structured property changes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.capabilities import Capability
from ifc_console.core.changes import IfcScalar, PropertyPreview
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
MODEL_ARG = "model_id of an attached model (see list_models); omit for the active model."

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
            "[PREVIEW] Build a revision-bound ChangeSet for occurrence-level "
            "IfcPropertySingleValue values. Missing properties and property sets may be "
            "created only when create_missing is explicit. The live model and source IFC are untouched. "
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
        create_missing: bool = False,
        nominal_type: Annotated[str | None, Field(max_length=255)] = None,
        expected_revision: str | None = None,
    ) -> Envelope:
        record = await core.transactions.preview_property_value(
            global_ids=global_ids,
            pset_name=pset_name,
            property_name=property_name,
            value=value,
            create_missing=create_missing,
            nominal_type=nominal_type,
            expected_revision=expected_revision,
        )
        return ok(
            {"change_set": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )

    @registry.tool(
        annotations=PREVIEW_ANN,
        data_model=ChangeSetData,
        required_capabilities=(Capability.MODEL_PREVIEW, Capability.ARTIFACT_WRITE),
        description=(
            "[PREVIEW] Build one revision-bound ChangeSet containing multiple "
            "occurrence-level property assignments for the same elements. The live model "
            "and source IFC are untouched; approval and commit remain explicit caller actions."
        ),
    )
    @enveloped(core, "preview_property_changes")
    async def preview_property_changes(
        global_ids: Annotated[list[str], Field(min_length=1, max_length=500)],
        properties: Annotated[list[PropertyPreview], Field(min_length=1, max_length=16)],
        expected_revision: str | None = None,
    ) -> Envelope:
        record = await core.transactions.preview_property_values(
            global_ids=global_ids,
            properties=properties,
            expected_revision=expected_revision,
        )
        return ok(
            {"change_set": record.model_dump(mode="json")},
            core.session_meta(),
            char_limit=char_limit,
        )

    @registry.tool(
        annotations=PREVIEW_ANN,
        data_model=ChangeSetData,
        description=(
            "[PREVIEW] Build a revision-bound ChangeSet that directly assigns a typed "
            "classification reference to IFC occurrences. Missing systems and references "
            "are created in the candidate only. The source IFC is untouched, and approval "
            "and commit remain explicit SDK or CLI caller actions."
        ),
    )
    @enveloped(core, "preview_classification_assignment")
    async def preview_classification_assignment(
        global_ids: Annotated[list[str], Field(min_length=1, max_length=500)],
        classification_name: Annotated[str, Field(min_length=1, max_length=255)],
        identification: Annotated[str, Field(min_length=1, max_length=255)],
        reference_name: Annotated[str, Field(min_length=1, max_length=255)],
        expected_revision: str | None = None,
    ) -> Envelope:
        record = await core.transactions.preview_classification_assignment(
            global_ids=global_ids,
            classification_name=classification_name,
            identification=identification,
            reference_name=reference_name,
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

    @registry.tool(
        annotations=READ_ANN,
        description=(
            "[QUERY] List every element in the open model that carries an "
            "AI-authored property set. Agents write only into the reserved "
            "IfcConsole_AI_ namespace, so this is the complete inventory of "
            "AI-assisted data in the file, with the provenance record (agent, "
            "model, method, source document) stored beside each value. Use it "
            "to review, report, or strip that layer."
        ),
    )
    @enveloped(core, "list_ai_authored_properties")
    async def list_ai_authored_properties(
        limit: Annotated[int, Field(ge=1, le=2000, description="Maximum elements.")] = 200,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        from ifc_console.ifc.ai_provenance import read_ai_properties

        session = core.resolve_session(model)
        data = await session.run(lambda: read_ai_properties(session.ifc, limit=limit))
        return ok(
            data,
            core.session_meta(),
            char_limit=char_limit,
            returned=len(data["elements"]),
        )
