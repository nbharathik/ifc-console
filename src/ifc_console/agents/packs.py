"""Definitions and registry for the agents that ship with IFC Console.

A pack is deliberately smaller than an extension system: it describes one
built-in assistant and constructs its scoped :class:`Agent` over a runtime.
External discovery and installation are not part of the current product.
Applications embedding IFC Console may still register a pack explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from ifc_console.agents.agent import Agent

# Declarative capabilities rendered by the first-party panel.
KNOWN_FEATURES = ("files", "vision", "viewer", "proposals")


class AgentPackInfo(BaseModel):
    """What the panel and the CLI show about one pack."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    version: str = Field(default="0", max_length=40)
    features: tuple[str, ...] = ()
    starters: tuple[str, ...] = Field(default=(), max_length=8)
    kind: Literal["built-in", "custom"] = "built-in"
    blocks: tuple[str, ...] = ()

    @field_validator("features")
    @classmethod
    def _known_features(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        unknown = sorted(set(value) - set(KNOWN_FEATURES))
        if unknown:
            raise ValueError(f"unknown agent feature(s): {', '.join(unknown)}")
        return tuple(dict.fromkeys(value))

    @field_validator("starters")
    @classmethod
    def _short_starters(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(prompt.strip() for prompt in value if prompt.strip())
        if any(len(prompt) > 180 for prompt in cleaned):
            raise ValueError("agent starter prompts must be at most 180 characters")
        return tuple(dict.fromkeys(cleaned))


@runtime_checkable
class AgentPack(Protocol):
    """One agent the console can host in its panel."""

    info: AgentPackInfo

    async def build(
        self,
        runtime: Any,
        *,
        model: Any,
        viewer: bool = False,
        instructions: str = "",
        model_label: str = "",
    ) -> Agent: ...


class AgentPackRegistry:
    """Built-in packs plus packs registered explicitly by an embedding app."""

    def __init__(self, project_dir: str | Path | None = None) -> None:
        self._packs: dict[str, AgentPack] = {}
        self._order: list[str] = []
        self._builtin_names: set[str] = set()
        self._custom_names: set[str] = set()
        self.problems: list[str] = []
        self.blueprints = None
        from ifc_console.agents.builtin import builtin_packs

        for pack in builtin_packs():
            self.register(pack, builtin=True)
        if project_dir is not None:
            from ifc_console.agents.blueprints import AgentBlueprintStore

            self.blueprints = AgentBlueprintStore(project_dir)
            self.refresh_custom()

    def register(self, pack: AgentPack, *, builtin: bool = False) -> None:
        """Add a trusted pack programmatically (tests and host applications)."""
        info = pack.info
        if not isinstance(info, AgentPackInfo):
            raise TypeError("pack.info must be an AgentPackInfo")
        self._packs[info.name] = pack
        if builtin:
            self._builtin_names.add(info.name)
            if info.name not in self._order:
                self._order.append(info.name)

    def is_builtin(self, name: str) -> bool:
        return name in self._builtin_names

    def refresh_custom(self) -> None:
        """Reload project blueprints without disturbing host-registered packs."""
        if self.blueprints is None:
            return
        from ifc_console.agents.blueprints import BlueprintPack

        for name in self._custom_names:
            self._packs.pop(name, None)
        self._custom_names.clear()
        blueprints = self.blueprints.load()
        problems = list(self.blueprints.problems)
        for blueprint in blueprints:
            if blueprint.name in self._builtin_names:
                problems.append(f"{blueprint.name}: conflicts with a built-in agent")
                continue
            pack = BlueprintPack(blueprint)
            self._packs[pack.info.name] = pack
            self._custom_names.add(pack.info.name)
        self.problems = problems

    def save_blueprint(self, blueprint: Any) -> AgentPack:
        if self.blueprints is None:
            raise RuntimeError("this registry has no project blueprint store")
        if blueprint.name in self._builtin_names:
            raise ValueError("custom agents cannot replace built-in agents")
        from ifc_console.agents.blueprints import BlueprintPack

        self.blueprints.save(blueprint)
        pack = BlueprintPack(blueprint)
        self._packs[pack.info.name] = pack
        self._custom_names.add(pack.info.name)
        return pack

    def delete_blueprint(self, name: str) -> bool:
        """Forget one custom agent, on disk and in this process."""
        if self.blueprints is None or name in self._builtin_names:
            return False
        removed = self.blueprints.delete(name)
        if removed:
            self._packs.pop(name, None)
            self._custom_names.discard(name)
        return removed

    def installed(self) -> list[AgentPackInfo]:
        """Every pack available in this Console process.

        Built-ins keep the order they were registered in, because that order is
        a recommendation: the general assistant is the one to start with.
        A user's own agents follow, alphabetically.
        """
        self.refresh_custom()
        builtin = [name for name in self._order if name in self._packs]
        rest = sorted(set(self._packs) - set(builtin))
        return [self._packs[name].info for name in [*builtin, *rest]]

    def active(self) -> list[AgentPackInfo]:
        """Every available pack; there is no install/activation layer yet."""
        return self.installed()

    def get(self, name: str) -> AgentPack | None:
        """A pack by name, or ``None`` when it is not shipped/registered."""
        if name.startswith("custom-"):
            self.refresh_custom()
        return self._packs.get(name)


__all__ = [
    "KNOWN_FEATURES",
    "AgentPack",
    "AgentPackInfo",
    "AgentPackRegistry",
]
