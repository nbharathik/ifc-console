"""Project-local agents assembled from the shared capability blocks.

Blueprints are data, not Python plugins. They can set an agent's role prompt
and pick from :mod:`ifc_console.agents.blocks`, but cannot name arbitrary
operations or weaken the runtime's capability and approval policy. The same
:func:`~ifc_console.agents.blocks.compose` call builds them and the built-in
agents, so a custom agent is never a second-class citizen.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ifc_console.agents.agent import Agent
from ifc_console.agents.blocks import BLOCK_BY_NAME, BLOCKS, compose, features_for
from ifc_console.agents.models import AgentLimits
from ifc_console.agents.packs import AgentPackInfo

BLUEPRINTS_DIR = Path(".ifc-console") / "agents" / "custom"

DEFAULT_ROLE = "You are a project-specific IFC assistant built from reviewed capability blocks."

WorkflowStrategy = Literal["adaptive", "evidence-first", "fast-scan"]

STRATEGY_GUIDANCE: dict[WorkflowStrategy, str] = {
    "adaptive": (
        "Workflow strategy: choose the shortest reliable path for the request. "
        "Gather evidence, measure, verify, or propose only when needed."
    ),
    "evidence-first": (
        "Workflow strategy: gather and cite model or document evidence before "
        "measuring, validating, or proposing."
    ),
    "fast-scan": (
        "Workflow strategy: start with broad, low-cost checks. Investigate only "
        "the highest-impact findings unless the user asks for exhaustive work."
    ),
}


class AgentWorkflow(BaseModel):
    """Execution preferences for a custom agent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    strategy: WorkflowStrategy = "adaptive"
    max_tool_rounds: int = Field(default=12, ge=1, le=100, strict=True)
    max_tool_calls: int = Field(default=48, ge=1, le=1000, strict=True)


class AgentBlueprint(BaseModel):
    """A custom agent definition safe to persist in a project."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = "1"
    name: str = Field(pattern=r"^custom-[a-z0-9][a-z0-9-]*$", max_length=64)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    instructions: str = Field(min_length=1, max_length=12_000)
    blocks: tuple[str, ...] = Field(min_length=1, max_length=len(BLOCKS))
    workflow: AgentWorkflow = Field(default_factory=AgentWorkflow)
    starters: tuple[str, ...] = Field(default=(), max_length=6)
    content_paths: tuple[str, ...] | None = Field(default=None, max_length=500)

    @field_validator("blocks")
    @classmethod
    def _known_blocks(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(BLOCK_BY_NAME))
        if unknown:
            raise ValueError(f"unknown agent block(s): {', '.join(unknown)}")
        return tuple(dict.fromkeys(value))

    @field_validator("starters")
    @classmethod
    def _clean_starters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(item.strip() for item in value if item.strip())
        if any(len(item) > 180 for item in cleaned):
            raise ValueError("starter prompts must be at most 180 characters")
        return tuple(dict.fromkeys(cleaned))

    @field_validator("content_paths")
    @classmethod
    def _clean_content_paths(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        from ifc_console.agents.content import normalize_content_path

        cleaned: list[str] = []
        for raw in value:
            path = normalize_content_path(raw)
            if path is None:
                raise ValueError("content paths must be project-relative paths")
            if path not in cleaned:
                cleaned.append(path)
        return tuple(cleaned)


def blueprint_name(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.casefold()).strip("-") or "agent"
    return f"custom-{slug[:55].rstrip('-')}"


class AgentBlueprintStore:
    """Atomic project-local storage for custom agent definitions."""

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.directory = self.project_dir / BLUEPRINTS_DIR
        self.problems: list[str] = []

    def load(self) -> list[AgentBlueprint]:
        self.problems = []
        if not self.directory.is_dir():
            return []
        blueprints: list[AgentBlueprint] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                blueprints.append(
                    AgentBlueprint.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except Exception as exc:
                self.problems.append(f"{path.name}: {exc}")
        return blueprints

    def save(self, blueprint: AgentBlueprint) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{blueprint.name}.json"
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(blueprint.model_dump_json(indent=2))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def delete(self, name: str) -> bool:
        """Remove one blueprint file. Returns True when something was deleted."""
        if not re.fullmatch(r"custom-[a-z0-9][a-z0-9-]*", name or ""):
            return False
        target = self.directory / f"{name}.json"
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True


class BlueprintPack:
    """Adapt one declarative blueprint to the normal AgentPack protocol."""

    def __init__(self, blueprint: AgentBlueprint) -> None:
        self.blueprint = blueprint
        self.declared_limits = AgentLimits(
            max_tool_rounds=blueprint.workflow.max_tool_rounds,
            max_tool_calls=blueprint.workflow.max_tool_calls,
        )
        self.info = AgentPackInfo(
            name=blueprint.name,
            title=blueprint.title,
            description=blueprint.description,
            version=blueprint.version,
            features=features_for(blueprint.blocks),
            starters=blueprint.starters,
            kind="custom",
            blocks=blueprint.blocks,
        )

    async def compose(
        self,
        runtime: Any,
        *,
        viewer: bool = False,
        instructions: str = "",
        model_label: str = "",
    ):
        extra = "\n\n".join(
            part for part in (self.blueprint.instructions.strip(), instructions.strip()) if part
        )
        role = "\n\n".join(
            (DEFAULT_ROLE, STRATEGY_GUIDANCE[self.blueprint.workflow.strategy])
        )
        return await compose(
            runtime,
            self.blueprint.blocks,
            role=role,
            extra_instructions=extra,
            viewer=viewer,
            agent=self.blueprint.name,
            model_label=model_label,
        )

    async def build(
        self,
        runtime: Any,
        *,
        model: Any,
        viewer: bool = False,
        instructions: str = "",
        model_label: str = "",
    ) -> Agent:
        composition = await self.compose(
            runtime,
            viewer=viewer,
            instructions=instructions,
            model_label=model_label,
        )
        return Agent(
            name=self.blueprint.name,
            model=model,
            tools=composition.tools,
            instructions=composition.instructions,
            limits=self.declared_limits,
        )


__all__ = [
    "BLOCKS",
    "BLOCK_BY_NAME",
    "BLUEPRINTS_DIR",
    "DEFAULT_ROLE",
    "AgentBlueprint",
    "AgentBlueprintStore",
    "AgentWorkflow",
    "BlueprintPack",
    "STRATEGY_GUIDANCE",
    "WorkflowStrategy",
    "blueprint_name",
]
