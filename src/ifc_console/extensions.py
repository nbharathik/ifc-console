"""Trusted, install-time extensions for optional IFC Console products.

Operation plugins add individual tools and remain deny-by-default. Extensions
are a different boundary: an explicitly installed companion distribution may
add a complete first-party surface such as routes, state, operations, and a
browser panel. Core never imports an extension implementation directly.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.core.results import ToolError

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.core.operations import OperationRegistry

log = logging.getLogger("ifc-console.extensions")

EXTENSION_API_VERSION = "1"
ENTRY_POINT_GROUP = "ifc_console.extensions"


class ExtensionManifest(BaseModel):
    """Stable metadata exported before an extension is attached."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    api_version: Literal["1"] = "1"
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)


class BrowserPanel(BaseModel):
    """Declarative browser contribution consumed by the viewer shell.

    ``module_url`` must be an ES module exporting ``mountPanel(element)``.
    The mounted object may expose ``focus()``, ``refresh()``, and
    ``setVisible(bool)``; the shell feature-detects each method.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=80)
    module_url: str = Field(pattern=r"^/[A-Za-z0-9_./-]+\.js$")
    stylesheet_url: str | None = Field(
        default=None, pattern=r"^/[A-Za-z0-9_./-]+\.css$"
    )
    standalone_url: str | None = Field(default=None, pattern=r"^/[A-Za-z0-9_./?&=-]+$")


class ConsoleExtension(Protocol):
    """Contract implemented by an installed product extension."""

    manifest: ExtensionManifest | Mapping[str, Any]

    def attach(self, core: AppCore) -> object: ...

    def register_operations(
        self, core: AppCore, registry: OperationRegistry, state: object
    ) -> None: ...

    def http_routes(self, core: AppCore, state: object) -> Sequence[Any]: ...

    def status(self, core: AppCore, state: object) -> Mapping[str, Any]: ...

    def browser_panel(self, core: AppCore, state: object) -> BrowserPanel | None: ...

    def close(self, core: AppCore, state: object) -> None: ...


@dataclass(frozen=True)
class ExtensionRecord:
    name: str
    target: str
    distribution: str | None
    distribution_version: str | None
    status: Literal["loaded", "error"]
    manifest: ExtensionManifest | None = None
    error: str | None = None


@dataclass
class _LoadedExtension:
    manifest: ExtensionManifest
    extension: Any
    state: object
    operations_registered: bool = False


@dataclass
class AssistantState:
    """Compatibility state retained while the agent UI moves out of core."""

    enabled: bool = False
    provider: str = "openai"
    model: str = ""
    base_url: str = ""
    keys: dict[str, str] = field(default_factory=dict, repr=False)
    url: str | None = None

    def key_for(self, provider: str) -> str:
        return self.keys.get(provider, "")


class ExtensionManager:
    """Discover, validate, attach, and close installed product extensions."""

    def __init__(self, entry_points: Iterable[Any] | None = None) -> None:
        self._provided = tuple(entry_points) if entry_points is not None else None
        self._loaded: dict[str, _LoadedExtension] = {}
        self.records: list[ExtensionRecord] = []
        self._core: AppCore | None = None

    def discover(self) -> list[Any]:
        entries = (
            list(self._provided)
            if self._provided is not None
            else list(metadata.entry_points(group=ENTRY_POINT_GROUP))
        )
        return sorted(entries, key=lambda entry: (entry.name.casefold(), entry.value))

    @staticmethod
    def _distribution(entry: Any) -> tuple[str | None, str | None]:
        distribution = getattr(entry, "dist", None)
        if distribution is None:
            return None, None
        name = getattr(distribution, "name", None)
        version = getattr(distribution, "version", None)
        return (str(name) if name else None, str(version) if version else None)

    def attach(self, core: AppCore) -> list[ExtensionRecord]:
        """Attach each installed extension once without making core startup fragile."""

        if self._core is not None:
            return list(self.records)
        self._core = core
        seen: set[str] = set()
        for entry in self.discover():
            distribution, distribution_version = self._distribution(entry)
            common = {
                "name": entry.name,
                "target": entry.value,
                "distribution": distribution,
                "distribution_version": distribution_version,
            }
            try:
                if entry.name in seen:
                    raise ValueError(f"duplicate extension entry point {entry.name!r}")
                seen.add(entry.name)
                exported = entry.load()
                extension = exported() if inspect.isclass(exported) else exported
                manifest = ExtensionManifest.model_validate(
                    getattr(extension, "manifest", None)
                )
                if manifest.name != entry.name:
                    raise ValueError(
                        f"manifest name {manifest.name!r} does not match entry point "
                        f"{entry.name!r}"
                    )
                state = extension.attach(core)
                self._loaded[manifest.name] = _LoadedExtension(
                    manifest=manifest,
                    extension=extension,
                    state=state,
                )
                self.records.append(
                    ExtensionRecord(**common, status="loaded", manifest=manifest)
                )
            # An installed companion must never prevent the deterministic
            # console and viewer from starting. SystemExit/KeyboardInterrupt
            # still propagate; ordinary extension setup failures are recorded.
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                log.warning("extension %s did not load: %s", entry.name, message)
                self.records.append(ExtensionRecord(**common, status="error", error=message))
        return list(self.records)

    def available(self, name: str) -> bool:
        return name in self._loaded

    def state(self, name: str) -> object | None:
        loaded = self._loaded.get(name)
        return loaded.state if loaded is not None else None

    def require(self, name: str) -> object:
        state = self.state(name)
        if state is None:
            package = "ifc-console-agents" if name == "agents" else name
            raise ToolError(
                "EXTRA_NOT_INSTALLED",
                f"the {name} extension is not installed",
                f"Install {package} and restart IFC Console.",
            )
        return state

    def register_operations(self, registry: OperationRegistry) -> None:
        if self._core is None:
            raise RuntimeError("extensions must attach before registering operations")
        for loaded in self._loaded.values():
            if loaded.operations_registered:
                continue
            register = getattr(loaded.extension, "register_operations", None)
            if callable(register):
                register(self._core, registry, loaded.state)
            loaded.operations_registered = True

    def http_routes(self) -> list[Any]:
        if self._core is None:
            return []
        routes: list[Any] = []
        claimed: set[tuple[str, str]] = set()
        for loaded in self._loaded.values():
            provider = getattr(loaded.extension, "http_routes", None)
            if not callable(provider):
                continue
            for route in provider(self._core, loaded.state) or ():
                path = str(getattr(route, "path", ""))
                methods = getattr(route, "methods", None) or {"*"}
                keys = {(path, str(method)) for method in methods}
                duplicate = keys & claimed
                if duplicate:
                    raise RuntimeError(
                        "extension route collision: "
                        + ", ".join(f"{method} {path}" for path, method in sorted(duplicate))
                    )
                claimed.update(keys)
                routes.append(route)
        return routes

    def status(self) -> dict[str, Any]:
        if self._core is None:
            return {}
        result: dict[str, Any] = {}
        for name, loaded in self._loaded.items():
            provider = getattr(loaded.extension, "status", None)
            value = provider(self._core, loaded.state) if callable(provider) else {}
            normalized = dict(value or {})
            json.dumps(normalized)
            result[name] = normalized
        return result

    def browser_panels(self) -> list[dict[str, Any]]:
        if self._core is None:
            return []
        panels: list[dict[str, Any]] = []
        for loaded in self._loaded.values():
            provider = getattr(loaded.extension, "browser_panel", None)
            if not callable(provider):
                continue
            value = provider(self._core, loaded.state)
            if value is not None:
                panels.append(BrowserPanel.model_validate(value).model_dump(mode="json"))
        return panels

    def close(self) -> None:
        if self._core is None:
            return
        for loaded in reversed(tuple(self._loaded.values())):
            close = getattr(loaded.extension, "close", None)
            if not callable(close):
                continue
            try:
                close(self._core, loaded.state)
            except Exception:
                log.warning("extension %s close failed", loaded.manifest.name, exc_info=True)
        self._loaded.clear()
