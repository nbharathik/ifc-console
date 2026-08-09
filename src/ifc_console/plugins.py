"""Allowlisted Python operation plugins for trusted local integrations."""

from __future__ import annotations

import inspect
from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.core.operations import OperationRegistry

PLUGIN_API_VERSION = "1"
ENTRY_POINT_GROUP = "ifc_console.plugins"


class PluginManifest(BaseModel):
    """Metadata exported by every compatible plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["1"] = "1"
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    homepage: str | None = None


class PluginRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    target: str
    distribution: str | None = None
    distribution_version: str | None = None
    status: Literal[
        "disabled", "configured", "not_allowed", "loaded", "error", "missing"
    ]
    manifest: PluginManifest | None = None
    operations: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class PluginAPI:
    """The versioned object passed to a plugin's register method."""

    registry: OperationRegistry
    core: AppCore
    api_version: str = PLUGIN_API_VERSION


class PluginManager:
    def __init__(self, entry_points: list[Any] | None = None) -> None:
        self._provided = entry_points
        self.records: list[PluginRecord] = []

    def discover(self) -> list[Any]:
        if self._provided is not None:
            found = list(self._provided)
        else:
            found = list(metadata.entry_points(group=ENTRY_POINT_GROUP))
        return sorted(found, key=lambda entry: (entry.name.lower(), entry.value))

    @staticmethod
    def _distribution(entry: Any) -> tuple[str | None, str | None]:
        dist = getattr(entry, "dist", None)
        if dist is None:
            return None, None
        name = getattr(dist, "name", None)
        version = getattr(dist, "version", None)
        return str(name) if name else None, str(version) if version else None

    def inventory(self, *, enabled: bool, allow: set[str]) -> list[PluginRecord]:
        records: list[PluginRecord] = []
        found_names: set[str] = set()
        entries = self.discover()
        name_counts = Counter(entry.name.lower() for entry in entries)
        for entry in entries:
            name = entry.name.lower()
            found_names.add(name)
            distribution, version = self._distribution(entry)
            status = "not_allowed" if enabled else "disabled"
            error = None
            if enabled and name in allow:
                status = "configured"
                if name_counts[name] > 1:
                    status = "error"
                    error = "multiple installed entry points use this allowed plugin name"
            records.append(
                PluginRecord(
                    name=name,
                    target=entry.value,
                    distribution=distribution,
                    distribution_version=version,
                    status=status,
                    error=error,
                )
            )
        for missing in sorted(allow - found_names):
            records.append(
                PluginRecord(
                    name=missing,
                    target="",
                    status="missing",
                    error="allowed plugin is not installed",
                )
            )
        return records

    def load_configured(self, core: AppCore, registry: OperationRegistry) -> list[PluginRecord]:
        settings = core.settings.plugins
        allow = {name.strip().lower() for name in settings.allow if name.strip()}
        if not settings.enabled:
            self.records = self.inventory(enabled=False, allow=allow)
            return self.records

        records: list[PluginRecord] = []
        found_names: set[str] = set()
        entries = self.discover()
        name_counts = Counter(entry.name.lower() for entry in entries)
        for entry in entries:
            name = entry.name.lower()
            found_names.add(name)
            distribution, distribution_version = self._distribution(entry)
            common = {
                "name": name,
                "target": entry.value,
                "distribution": distribution,
                "distribution_version": distribution_version,
            }
            if name not in allow:
                records.append(PluginRecord(**common, status="not_allowed"))
                continue
            if name_counts[name] > 1:
                records.append(
                    PluginRecord(
                        **common,
                        status="error",
                        error="multiple installed entry points use this allowed plugin name",
                    )
                )
                continue

            before = {spec.name for spec in registry.specs()}
            try:
                loaded = entry.load()
                plugin = loaded() if inspect.isclass(loaded) else loaded
                manifest = PluginManifest.model_validate(getattr(plugin, "manifest", None))
                if manifest.name != name:
                    raise ValueError(
                        f"manifest name {manifest.name!r} does not match entry point {name!r}"
                    )
                register = getattr(plugin, "register", None)
                if not callable(register):
                    raise TypeError("plugin must expose register(api)")
                register(PluginAPI(registry=registry, core=core))
                added = tuple(
                    sorted(spec.name for spec in registry.specs() if spec.name not in before)
                )
                if not added:
                    raise ValueError("plugin registered no operations")
                record = PluginRecord(
                    **common,
                    status="loaded",
                    manifest=manifest,
                    operations=added,
                )
                core.audit.record(
                    "plugin_loaded",
                    plugin=name,
                    plugin_version=manifest.version,
                    operations=list(added),
                )
            except Exception as exc:
                for spec in list(registry.specs()):
                    if spec.name not in before:
                        registry.remove_tool(spec.name)
                record = PluginRecord(
                    **common,
                    status="error",
                    error=f"{type(exc).__name__}: {exc}"[:500],
                )
                core.audit.record("plugin_failed", plugin=name, error=record.error)
            records.append(record)

        for missing in sorted(allow - found_names):
            records.append(
                PluginRecord(
                    name=missing,
                    target="",
                    status="missing",
                    error="allowed plugin is not installed",
                )
            )
        self.records = records
        return records


__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_API_VERSION",
    "PluginAPI",
    "PluginManager",
    "PluginManifest",
    "PluginRecord",
]
