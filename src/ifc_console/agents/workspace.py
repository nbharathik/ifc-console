"""Everything the panel needs to explain one agent, in a single payload.

The workspace answers the questions a person actually has before they trust an
assistant with a model: what can it reach, what will it do in what order, what
is it allowed to write, and what is already in front of it. It is assembled
from the same composition the agent runs with, so it cannot drift from
reality: if a tool is missing here, the agent does not have it either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ifc_console.agents.blocks import BLOCK_BY_NAME
from ifc_console.agents.models import AgentLimits
from ifc_console.agents.provenance import ALLOWED_PSETS, PROVENANCE_PSET
from ifc_console.agents.skills import AgentSkillStore

if TYPE_CHECKING:
    from ifc_console.app import AppCore

# Mirrors chat_flow.js. The panel renders the stage rail from the browser copy;
# this one lets the CLI and any other host explain the same pipeline.
STAGES: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    (
        "scope",
        "Scope",
        "Find the elements the question is about",
        (
            "get_ifc_project_info",
            "search_elements",
            "query_elements",
            "get_element",
            "get_psets",
            "get_spatial_structure",
            "get_viewer_selection",
            "list_models",
            "get_georeferencing",
            "get_schema_docs",
        ),
    ),
    (
        "evidence",
        "Evidence",
        "Read the manuals, drawings, and images",
        (
            "list_project_documents",
            "search_ifc_knowledge",
            "get_knowledge_record",
            "get_project_reference_image",
            "get_project_document_page",
            "find_files",
            "get_api_docs",
        ),
    ),
    (
        "method",
        "Method",
        "Pick a rule, then measure or compute",
        (
            "get_measurement_recipe",
            "list_agent_skills",
            "get_agent_skill",
            "save_agent_skill",
            "measure_elements",
            "measure_distance",
            "compute_quantities",
            "get_element_geometry",
            "analyze_element_geometry",
            "detect_clashes",
            "execute_ifc_code",
        ),
    ),
    (
        "verify",
        "Verify",
        "Check the result against the model and the 3D view",
        (
            "validate_model",
            "validate_ids",
            "highlight_elements",
            "apply_color_theme",
            "get_viewer_screenshot",
            "get_viewer_measurements",
            "control_viewer",
            "orient",
            "list_ai_authored_properties",
            "get_change_set",
            "export_csv",
            "export_measurement_report",
        ),
    ),
    (
        "propose",
        "Propose",
        "Prepare a reviewable, AI-marked change",
        (
            "measure__propose_measured_value",
            "measure__propose_property_value",
            "preview_property_change",
            "preview_classification_assignment",
        ),
    ),
)

_STAGE_OF = {tool: index for index, row in enumerate(STAGES) for tool in row[3]}


def _description(description: str) -> str:
    """One display-safe line, with the internal operation marker dropped."""
    text = " ".join(str(description or "").split())
    if text.startswith("["):
        _, _, text = text.partition("] ")
    return text


def _summary(description: str) -> str:
    """The first sentence of the normalized tool description."""
    text = _description(description)
    sentence = text.split(". ")[0].rstrip(".")
    return sentence[:180]


def _tool_row(definition: Any) -> dict[str, Any]:
    annotations = definition.annotations or {}
    if not isinstance(annotations, dict):
        annotations = annotations.model_dump()
    description = _description(definition.description)
    input_schema = getattr(definition, "input_schema", {})
    if not isinstance(input_schema, dict):
        input_schema = {}
    required_capabilities = [
        str(getattr(capability, "value", capability))
        for capability in (getattr(definition, "required_capabilities", ()) or ())
    ]
    read_only = annotations.get("readOnlyHint")
    read_only = True if read_only is None else bool(read_only)
    stage = _STAGE_OF.get(definition.name)
    stage_id = STAGES[stage][0] if stage is not None else ""
    # "Writes" means the IFC. Exporting a CSV artifact is a file the user asked
    # for, not a change to their model, and conflating the two makes the panel
    # look more dangerous than it is.
    return {
        "name": definition.name,
        "summary": _summary(description),
        "description": description,
        "input_schema": input_schema,
        "required_capabilities": required_capabilities,
        "source": str(getattr(definition, "source", "") or "ifc-console"),
        "stage": stage_id,
        "stage_label": STAGES[stage][1] if stage is not None else "",
        "read_only": read_only,
        "writes_model": not read_only and stage_id == "propose",
        "writes_artifact": not read_only and stage_id != "propose",
        "requires_approval": bool(getattr(definition, "requires_approval", False)),
        "tags": sorted(getattr(definition, "tags", ()) or ()),
    }


def _writes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in rows if row["writes_model"]]


async def describe(
    core: AppCore,
    pack: Any,
    *,
    instructions: str = "",
) -> dict[str, Any]:
    """A complete, JSON-safe description of one agent as it would run now."""
    from ifc_console.agents.panel import panel_limits, panel_runtime
    from ifc_console.agents.presets import PRESET_BY_NAME

    info = pack.info
    viewer = core.viewer.enabled
    runtime = panel_runtime(core)

    preset = PRESET_BY_NAME.get(info.name)
    blueprint = getattr(pack, "blueprint", None)
    workflow = (
        blueprint.workflow.model_dump(mode="json") if blueprint is not None else None
    )
    declared_limits = getattr(pack, "declared_limits", None)
    if isinstance(declared_limits, AgentLimits):
        requested_limits = declared_limits
    elif blueprint is not None:
        requested_limits = AgentLimits(
            max_tool_rounds=blueprint.workflow.max_tool_rounds,
            max_tool_calls=blueprint.workflow.max_tool_calls,
        )
    elif preset is not None:
        requested_limits = AgentLimits(
            max_tool_rounds=preset.max_tool_rounds,
            max_tool_calls=preset.max_tool_calls,
        )
    else:
        requested_limits = AgentLimits()
    limits = panel_limits(core, requested_limits)

    if hasattr(pack, "compose"):
        composition = await pack.compose(
            runtime, viewer=viewer, instructions=instructions
        )
    else:
        from ifc_console.agents.blocks import compose

        composition = await compose(
            runtime,
            info.blocks,
            role="",
            extra_instructions=instructions,
            viewer=viewer,
            agent=info.name,
        )

    rows = sorted(
        (_tool_row(definition) for definition in composition.tools.definitions),
        key=lambda row: (row["stage_label"] or "zz", row["name"]),
    )
    held = {row["name"] for row in rows}

    blocks = []
    for name in info.blocks:
        block = BLOCK_BY_NAME.get(name)
        if block is None:
            continue
        # The proposal block builds its tools per run, so they are not listed
        # on the block itself. Report what the agent actually holds.
        owned = (
            [row["name"] for row in rows if row["writes_model"]]
            if block.proposals
            else [tool for tool in block.tools if tool in held]
        )
        blocks.append(
            {
                "name": block.name,
                "title": block.title,
                "description": block.description,
                "features": list(block.features),
                "viewer_only": block.viewer_only,
                "advanced": block.advanced,
                "available": bool(owned),
                "tools": owned,
                "missing": [tool for tool in block.tools if tool not in held],
            }
        )

    stages = [
        {
            "id": identifier,
            "label": label,
            "hint": hint,
            "available": bool(set(tools) & held),
            "tools": [tool for tool in tools if tool in held],
        }
        for identifier, label, hint, tools in STAGES
    ]

    role = preset.role if preset else (blueprint.instructions if blueprint else "")
    examples = [example.as_dict() for example in preset.examples] if preset else []
    summary = preset.summary if preset else ""

    return {
        "agent": info.model_dump(mode="json"),
        "kind": info.kind,
        "builtin": core.agent_packs.is_builtin(info.name),
        "workflow": workflow,
        "role": role,
        "summary": summary,
        "examples": examples,
        "blocks": blocks,
        "stages": stages,
        "tools": rows,
        "tool_count": len(rows),
        "writes": _writes(rows),
        "artifact_writes": [row["name"] for row in rows if row["writes_artifact"]],
        "skills": AgentSkillStore(core.store.project_dir).entries(),
        "unavailable_tools": list(composition.unavailable),
        "viewer": viewer,
        "mode": core.policy.mode.value,
        "write_policy": {
            "can_commit": False,
            "property_sets": list(ALLOWED_PSETS),
            "provenance_set": PROVENANCE_PSET,
            "note": (
                "Writes are previews only. Values land in the reserved "
                "IfcConsole_AI_ property sets with a provenance record, and a "
                "human approves and commits them."
            ),
        },
        "limits": {
            "max_tool_rounds": limits.max_tool_rounds,
            "max_tool_calls": limits.max_tool_calls,
            "timeout_s": limits.timeout_s,
        },
    }


# Plain chat is the panel's default surface, so "what can this thing reach"
# has to answer for it too. It holds no blocks and keeps no server-side thread:
# it is the console's whole tool surface behind one stateless loop.
PLAIN_CHAT_ROLE_NOTE = (
    "Plain chat runs the console's own tool loop. It has no capability blocks "
    "and no memory on the console: every turn is sent with the transcript this "
    "browser holds. Pick an assistant for standing instructions, project "
    "reference files, or AI-marked proposals."
)

PLAIN_CHAT_EXAMPLES = (
    ("What is in this model?", "Project, schema, and the spatial tree in one call."),
    ("Which walls have no fire rating?", "Query elements and read their property sets."),
    ("Quantities by storey", "Compute quantities and group them."),
    ("Check the model and list the worst problems", "Run validation and rank what it finds."),
)


def describe_plain_chat(core: AppCore) -> dict[str, Any]:
    """The same workspace payload for the no-agent surface."""
    rows = sorted(
        (_tool_row(definition) for definition in core.operation_service.definitions()),
        key=lambda row: (row["stage_label"] or "zz", row["name"]),
    )
    held = {row["name"] for row in rows}
    stages = [
        {
            "id": identifier,
            "label": label,
            "hint": hint,
            "available": bool(set(tools) & held),
            "tools": [tool for tool in tools if tool in held],
        }
        for identifier, label, hint, tools in STAGES
    ]
    return {
        "agent": {
            "name": "",
            "title": "Plain chat",
            "description": "Direct chat over the open model, with no agent around it.",
            "kind": "built-in",
            "features": [],
            "blocks": [],
            "starters": [prompt for prompt, _ in PLAIN_CHAT_EXAMPLES],
        },
        "kind": "built-in",
        "builtin": True,
        "plain": True,
        "workflow": None,
        "role": "",
        "summary": PLAIN_CHAT_ROLE_NOTE,
        "examples": [
            {"title": prompt, "prompt": prompt, "note": note}
            for prompt, note in PLAIN_CHAT_EXAMPLES
        ],
        "blocks": [],
        "stages": stages,
        "tools": rows,
        "tool_count": len(rows),
        "writes": _writes(rows),
        "artifact_writes": [row["name"] for row in rows if row["writes_artifact"]],
        "unavailable_tools": [],
        "viewer": core.viewer.enabled,
        "mode": core.policy.mode.value,
        "write_policy": {
            "can_commit": False,
            "property_sets": list(ALLOWED_PSETS),
            "provenance_set": PROVENANCE_PSET,
            "note": (
                "Plain chat can preview a change, never commit one. The session "
                "mode decides whether a preview is offered at all."
            ),
        },
        "limits": {
            "max_tool_rounds": core.settings.chat.max_tool_rounds,
            "max_tool_calls": None,
            "timeout_s": core.settings.chat.timeout_s,
        },
        "files": [],
    }


__all__ = ["STAGES", "describe", "describe_plain_chat"]
