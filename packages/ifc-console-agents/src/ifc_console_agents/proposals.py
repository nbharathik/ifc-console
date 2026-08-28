"""The one narrow write path an agent gets: reviewable, marked proposals.

Two tools, both preview-only. The model may propose a measured metric or a
named property, always into the reserved AI namespace, and every proposal is
accompanied by a provenance record naming the agent, the model, the method,
and the document it came from. Approval and commit stay with the host, so
nothing here can change an IFC file.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations
from ifc_console.ifc.ai_provenance import (
    MEASUREMENT_PROPERTIES,
    MEASUREMENT_PSET,
    PROPERTY_PSET,
    PROVENANCE_PSET,
    Provenance,
    measurement_property,
    provenance_property_name,
    stamp,
    validate_property_name,
)
from ifc_console.toolsets import FunctionToolSource
from pydantic import Field

MeasurementMetric = Literal[
    "length",
    "width",
    "height",
    "thickness",
    "depth",
    "perimeter",
    "area",
    "volume",
    "distance",
    "count",
]

PREVIEW_ANN = OperationAnnotations(readOnlyHint=False, destructiveHint=False)
PREVIEW_CAPS = (Capability.MODEL_PREVIEW, Capability.ARTIFACT_WRITE)


def _change_set_id(result: dict[str, Any]) -> str:
    if not result.get("ok"):
        return ""
    record = (result.get("data") or {}).get("change_set") or {}
    identifier = record.get("change_set_id")
    return identifier if isinstance(identifier, str) else ""


async def _propose(
    runtime: Any,
    *,
    global_ids: list[str],
    pset: str,
    property_name: str,
    value: Any,
    nominal_type: str | None,
    provenance: Provenance,
    proposals: list[str],
) -> dict[str, Any]:
    """Preview one value and its provenance marker as one approval unit."""
    unique = list(dict.fromkeys(global_ids))
    marker_name = provenance_property_name(pset, property_name)
    result = await runtime.call(
        "preview_property_changes",
        global_ids=unique,
        properties=[
            {
                "pset_name": pset,
                "property_name": property_name,
                "value": value,
                "create_missing": True,
                "nominal_type": nominal_type,
            },
            {
                "pset_name": PROVENANCE_PSET,
                "property_name": marker_name,
                "value": provenance.with_proposal(uuid.uuid4().hex).to_json(),
                "create_missing": True,
                "nominal_type": "IfcText",
            },
        ],
    )
    change_set_id = _change_set_id(result)
    if not change_set_id:
        return result
    proposals.append(change_set_id)
    data = dict(result.get("data") or {})
    preview = data.get("change_set")
    if isinstance(preview, dict) and not isinstance(preview.get("change_set"), dict):
        # Oversized operation results retain only the artifact ID. Keep the
        # established first-change shape used by proposal UIs; the full,
        # verified record remains available through get_change_set.
        data["change_set"] = {
            **preview,
            "change_set": {
                "changes": [
                    {
                        "pset_name": pset,
                        "property_name": property_name,
                        "after": value,
                    }
                ]
            },
        }
    data["provenance_change_set"] = change_set_id
    data["ai_marked"] = True
    data["pset_name"] = pset
    data["property_name"] = property_name
    data["elements"] = len(unique)
    return {**result, "data": data}


def build_proposal_source(
    runtime: Any,
    proposals: list[str],
    *,
    agent: str = "measurement-agent",
    model_label: str = "",
    instructions: str = "",
    allow_named_properties: bool = True,
) -> FunctionToolSource:
    """Namespaced preview tools bound to one agent run.

    The property set is fixed in host code; the model chooses only the value,
    the property name inside the AI namespace, and the evidence it cites.
    """
    source = FunctionToolSource(namespace="measure", source_id="ifc-console:measure-agent")

    @source.tool(
        tags={"preview", "measurement"},
        annotations=PREVIEW_ANN,
        required_capabilities=PREVIEW_CAPS,
    )
    async def propose_measured_value(
        global_ids: Annotated[list[str], Field(min_length=1, max_length=500)],
        metric: MeasurementMetric,
        value: float,
        unit: Annotated[str, Field(max_length=32)] = "",
        method: Annotated[str, Field(max_length=120)] = "",
        source: Annotated[str, Field(max_length=240)] = "",
        confidence: Literal["high", "medium", "low", ""] = "",
    ) -> dict[str, Any]:
        """Preview an AI-assisted measurement in IfcConsole_AI_Measurements.

        Pass only GlobalIds measured in this run and a value in the model's
        file units. Name the method you used and the document and page it came
        from: both are stored in the provenance record beside the value. A
        human must approve and commit; this call changes no IFC data.
        """
        property_name, nominal_type = measurement_property(metric)
        record = Provenance(
            agent=agent,
            property_name=property_name,
            pset=MEASUREMENT_PSET,
            method=method or "unstated",
            model=model_label,
            source=source,
            unit=unit,
            confidence=confidence,
            instructions=instructions,
            written_at=stamp(),
        )
        return await _propose(
            runtime,
            global_ids=global_ids,
            pset=MEASUREMENT_PSET,
            property_name=property_name,
            value=value,
            nominal_type=nominal_type,
            provenance=record,
            proposals=proposals,
        )

    if allow_named_properties:

        @source.tool(
            tags={"preview", "properties"},
            annotations=PREVIEW_ANN,
            required_capabilities=PREVIEW_CAPS,
        )
        async def propose_property_value(
            global_ids: Annotated[list[str], Field(min_length=1, max_length=500)],
            property_name: Annotated[str, Field(min_length=1, max_length=63)],
            value: str | float | int | bool,
            nominal_type: Literal[
                "IfcText", "IfcLabel", "IfcReal", "IfcInteger", "IfcBoolean"
            ] = "IfcText",
            unit: Annotated[str, Field(max_length=32)] = "",
            method: Annotated[str, Field(max_length=120)] = "",
            source: Annotated[str, Field(max_length=240)] = "",
            confidence: Literal["high", "medium", "low", ""] = "",
        ) -> dict[str, Any]:
            """Preview any AI-derived property in IfcConsole_AI_Properties.

            Use this for values a document or the user's own procedure defines
            and that are not one of the standard measured metrics. The property
            set is fixed: agents never write into authored property sets. Name
            the method and cite the document and page; both are stored in the
            provenance record. A human must approve and commit.
            """
            try:
                clean = validate_property_name(property_name)
            except ValueError as exc:
                return {
                    "ok": False,
                    "error": {"code": "INVALID_INPUT", "message": str(exc), "hint": ""},
                    "meta": {},
                }
            record = Provenance(
                agent=agent,
                property_name=clean,
                pset=PROPERTY_PSET,
                method=method or "unstated",
                model=model_label,
                source=source,
                unit=unit,
                confidence=confidence,
                instructions=instructions,
                written_at=stamp(),
            )
            return await _propose(
                runtime,
                global_ids=global_ids,
                pset=PROPERTY_PSET,
                property_name=clean,
                value=value,
                nominal_type=nominal_type,
                provenance=record,
                proposals=proposals,
            )

    return source


PROPOSAL_TOOLS = ("measure__propose_measured_value", "measure__propose_property_value")

__all__ = [
    "MEASUREMENT_PROPERTIES",
    "PROPOSAL_TOOLS",
    "MeasurementMetric",
    "build_proposal_source",
]
