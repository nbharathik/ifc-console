"""Definitions and registry for the agents that ship with IFC Console.

A pack is deliberately smaller than an extension system: it describes one
built-in assistant and constructs its scoped :class:`Agent` over a runtime.
External discovery and installation are not part of the current product.
Applications embedding IFC Console may still register a pack explicitly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from ifc_console.agents.agent import Agent

# Declarative capabilities rendered by the first-party panel.
KNOWN_FEATURES = ("files",)


class AgentPackInfo(BaseModel):
    """What the panel and the CLI show about one pack."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$", max_length=64)
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=300)
    version: str = Field(default="0", max_length=40)
    features: tuple[str, ...] = ()
    starters: tuple[str, ...] = Field(default=(), max_length=8)

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

    async def build(self, runtime: Any, *, model: Any, viewer: bool = False) -> Agent: ...


class AgentPackRegistry:
    """Built-in packs plus packs registered explicitly by an embedding app."""

    def __init__(self) -> None:
        self._packs: dict[str, AgentPack] = {}
        self._builtin_names: set[str] = set()
        self.problems: list[str] = []
        from ifc_console.agents.builtin import builtin_packs

        for pack in builtin_packs():
            self.register(pack, builtin=True)

    def register(self, pack: AgentPack, *, builtin: bool = False) -> None:
        """Add a trusted pack programmatically (tests and host applications)."""
        info = pack.info
        if not isinstance(info, AgentPackInfo):
            raise TypeError("pack.info must be an AgentPackInfo")
        self._packs[info.name] = pack
        if builtin:
            self._builtin_names.add(pack.info.name)

    def is_builtin(self, name: str) -> bool:
        return name in self._builtin_names

    def installed(self) -> list[AgentPackInfo]:
        """Every pack available in this Console process."""
        return [self._packs[name].info for name in sorted(self._packs)]

    def active(self) -> list[AgentPackInfo]:
        """Every available pack; there is no install/activation layer yet."""
        return self.installed()

    def get(self, name: str) -> AgentPack | None:
        """A pack by name, or ``None`` when it is not shipped/registered."""
        return self._packs.get(name)


__all__ = [
    "KNOWN_FEATURES",
    "AgentPack",
    "AgentPackInfo",
    "AgentPackRegistry",
]
