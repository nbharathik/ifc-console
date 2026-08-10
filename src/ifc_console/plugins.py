"""Allowlisted Python operation plugins for trusted local integrations."""

from __future__ import annotations

import inspect
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import metadata
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ifc_console.core.results import Envelope, ErrorInfo

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.core.operations import OperationRegistry

PLUGIN_API_VERSION = "1"
ENTRY_POINT_GROUP = "ifc_console.plugins"
_OPERATION_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class PluginManifest(BaseModel):
    """Metadata exported by every compatible plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["1"] = "1"
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    homepage: str | None = None

    @field_validator("version")
    @classmethod
    def version_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("plugin version cannot be blank")
        return value


class PluginRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    target: str
    distribution: str | None = None
    distribution_version: str | None = None
    status: Literal["disabled", "configured", "not_allowed", "loaded", "error", "missing"]
    manifest: PluginManifest | None = None
    operations: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class PluginAPI:
    """The versioned object passed to a plugin's register method."""

    registry: OperationRegistry
    core: AppCore
    api_version: str = PLUGIN_API_VERSION

    def success(self, data: dict[str, Any], **meta: Any) -> Envelope:
        """Build a successful envelope for host normalization."""
        return Envelope(
            ok=True,
            data=data,
            meta={**self.core.session_meta(), **meta},
        )

    def failure(
        self,
        code: str,
        message: str,
        hint: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> Envelope:
        """Build a failed envelope with an actionable hint."""
        return Envelope(
            ok=False,
            error=ErrorInfo(code=code, message=message, hint=hint),
            data=data,
            meta=self.core.session_meta(),
        )


class OperationPlugin(Protocol):
    """Static contract implemented by an operation plugin entry point."""

    manifest: PluginManifest | Mapping[str, Any]

    def register(self, api: PluginAPI) -> None: ...


@dataclass(frozen=True)
class _LoadedPlugin:
    name: str
    plugin: Any
    api: PluginAPI


class _PluginContractError(ValueError):
    pass


def _manifest(plugin: Any) -> PluginManifest:
    try:
        return PluginManifest.model_validate(getattr(plugin, "manifest", None))
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in exc.errors(include_url=False, include_input=False)
        )
        raise _PluginContractError(f"plugin manifest is invalid: {issues}") from exc


class PluginManager:
    def __init__(self, entry_points: list[Any] | None = None) -> None:
        self._provided = entry_points
        self.records: list[PluginRecord] = []
        self._loaded: list[_LoadedPlugin] = []

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

            snapshot = registry._snapshot()
            before = set(snapshot)
            plugin: Any | None = None
            api: PluginAPI | None = None
            try:
                loaded = entry.load()
                plugin = loaded() if inspect.isclass(loaded) else loaded
                api = PluginAPI(registry=registry, core=core)
                manifest = _manifest(plugin)
                if manifest.name != name:
                    raise _PluginContractError(
                        f"manifest name {manifest.name!r} does not match entry point {name!r}"
                    )
                register = getattr(plugin, "register", None)
                if not callable(register):
                    raise _PluginContractError("plugin must expose register(api)")
                registration = register(api)
                if inspect.isawaitable(registration):
                    close = getattr(registration, "close", None)
                    if callable(close):
                        close()
                    raise _PluginContractError("plugin register(api) must be synchronous")
                current_specs = {spec.name: spec for spec in registry.specs()}
                inconsistent = sorted(
                    set(current_specs).symmetric_difference(registry.handlers)
                    | {
                        operation_name
                        for operation_name, spec in current_specs.items()
                        if registry.handlers.get(operation_name) is not spec.handler
                    }
                )
                if inconsistent:
                    raise _PluginContractError(
                        "plugin left the operation registry inconsistent: "
                        + ", ".join(inconsistent)
                    )
                changed = sorted(
                    operation_name
                    for operation_name, spec in snapshot.items()
                    if registry.get(operation_name) is not spec
                )
                if changed:
                    raise _PluginContractError(
                        f"plugin modified existing operations: {', '.join(changed)}"
                    )
                added = tuple(
                    sorted(spec.name for spec in registry.specs() if spec.name not in before)
                )
                if not added:
                    raise _PluginContractError("plugin registered no operations")
                invalid_names = [
                    operation_name
                    for operation_name in added
                    if _OPERATION_NAME.fullmatch(operation_name) is None
                ]
                if invalid_names:
                    raise _PluginContractError(
                        "plugin operation names must be 1 to 128 characters and use "
                        "only ASCII letters, digits, dots, underscores, or hyphens: "
                        + ", ".join(repr(item) for item in invalid_names)
                    )
                unstructured = [
                    operation_name
                    for operation_name in added
                    if not registry.require(operation_name).structured_output
                ]
                if unstructured:
                    raise _PluginContractError(
                        "plugin operations must use structured output: " + ", ".join(unstructured)
                    )
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
                self._loaded.append(_LoadedPlugin(name=name, plugin=plugin, api=api))
            except Exception as exc:
                registry._restore(snapshot)
                if plugin is not None and api is not None:
                    try:
                        self._shutdown_one(_LoadedPlugin(name=name, plugin=plugin, api=api))
                    finally:
                        registry._restore(snapshot)
                error = (
                    f"PluginContractError: {exc}"
                    if isinstance(exc, _PluginContractError)
                    else f"{type(exc).__name__}: plugin setup failed"
                )
                record = PluginRecord(
                    **common,
                    status="error",
                    error=error[:500],
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

    @staticmethod
    def _shutdown_one(loaded: _LoadedPlugin) -> None:
        shutdown = getattr(loaded.plugin, "shutdown", None)
        if shutdown is None:
            return
        if not callable(shutdown):
            loaded.api.core.audit.record(
                "plugin_shutdown_failed",
                plugin=loaded.name,
                error="TypeError: plugin shutdown attribute is not callable",
            )
            return
        try:
            result = shutdown(loaded.api)
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("plugin shutdown(api) must be synchronous")
            loaded.api.core.audit.record("plugin_shutdown", plugin=loaded.name)
        except Exception as exc:
            loaded.api.core.audit.record(
                "plugin_shutdown_failed",
                plugin=loaded.name,
                error=f"{type(exc).__name__}: plugin shutdown failed",
            )

    def close(self) -> None:
        """Run optional plugin shutdown hooks once, in reverse load order."""
        loaded, self._loaded = self._loaded, []
        for item in reversed(loaded):
            self._shutdown_one(item)


__all__ = [
    "ENTRY_POINT_GROUP",
    "PLUGIN_API_VERSION",
    "PluginAPI",
    "PluginManager",
    "PluginManifest",
    "PluginRecord",
    "OperationPlugin",
]
