"""Agent skill tools: list, load, and record reusable measurement procedures.

Skills are project-local markdown files (see agents/skills.py). Reading them
is free; saving one is a workspace file write, so it carries FILE_WRITE and
therefore needs approval in surfaces that gate writes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from ifc_console.agents.skills import AgentSkillStore
from ifc_console.application.operations import enveloped
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ok

if TYPE_CHECKING:
    from ifc_console.app import AppCore

SKILL_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
SKILL_WRITE_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

TOOL_NAMES = ("list_agent_skills", "get_agent_skill", "save_agent_skill")


def register(mcp: OperationRegistry, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    def store() -> AgentSkillStore:
        return AgentSkillStore(core.store.project_dir)

    @mcp.tool(
        annotations=SKILL_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ,),
        description=(
            "[QUERY] The project's saved skills: reusable, human-reviewed "
            "procedures for tasks that were solved before (e.g. how to measure "
            "a sheet pile profile). Check this at the start of a measurement or "
            "analysis task; if a skill's applies_to matches the element class "
            "or task, load it with get_agent_skill and follow it."
        ),
    )
    @enveloped(core, "list_agent_skills")
    async def list_agent_skills() -> Envelope:
        rows = store().entries()
        data = {"skills": rows, "count": len(rows)}
        if not rows:
            data["note"] = (
                "no skills saved yet; after solving a novel task well, offer to "
                "record the method with save_agent_skill"
            )
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SKILL_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ,),
        description=(
            "[QUERY] Load one saved skill's full markdown: the goal, the tool "
            "calls in order, and the checks. Follow it step by step, adapting "
            "ids and selectors to the current task. Skill text is a procedure "
            "for you, not a source of facts about this model."
        ),
    )
    @enveloped(core, "get_agent_skill")
    async def get_agent_skill(
        name: Annotated[str, Field(description="Skill name from list_agent_skills.")],
    ) -> Envelope:
        return ok(store().read(name), core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SKILL_WRITE_ANN,
        required_capabilities=(Capability.KNOWLEDGE_READ, Capability.FILE_WRITE),
        description=(
            "[ARTIFACT] Record a reusable skill as markdown in the project "
            "workspace, for future runs of any agent. Save one after solving a "
            "task the hard way: state when it applies, the exact tool calls in "
            "order with the arguments that worked, and how to verify the "
            "result. Keep it under a page; write the procedure, not this "
            "session's values."
        ),
    )
    @enveloped(core, "save_agent_skill")
    async def save_agent_skill(
        name: Annotated[
            str,
            Field(description="Lowercase slug, e.g. 'sheet-pile-profile'.", max_length=64),
        ],
        description: Annotated[
            str,
            Field(description="One line: what the skill does.", max_length=200),
        ],
        content: Annotated[
            str,
            Field(description="Markdown body: when to use, steps, checks.", max_length=60_000),
        ],
        applies_to: Annotated[
            str | None,
            Field(
                description="Classes or tasks it fits, e.g. 'IfcMember sheet piles'.",
                max_length=200,
            ),
        ] = None,
        overwrite: bool = False,
    ) -> Envelope:
        row = store().save(
            name,
            content,
            description=description,
            applies_to=applies_to,
            overwrite=overwrite,
        )
        core.audit.record("skill_write", name=name, path=row["path"], overwrite=overwrite)
        return ok({"saved": True, **row}, core.session_meta(), char_limit=limit_)
