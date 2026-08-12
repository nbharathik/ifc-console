"""Composable, provider-neutral tool sources for IFC agent applications.

The core operation registry remains the source of truth for IFC behavior.  This
module only adds composition: applications can select a safe subset of those
operations and combine it with trusted Python functions or remote MCP tools.
"""

from __future__ import annotations

import fnmatch
import inspect
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, ValidationError

from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import (
    OperationAnnotations,
    OperationRegistry,
)
from ifc_console.core.results import Envelope

_NAME_PART = re.compile(r"[^A-Za-z0-9_-]+")
_APPROVAL_CAPABILITIES = frozenset(
    {
        Capability.FILE_WRITE.value,
        Capability.MODEL_APPROVE.value,
        Capability.MODEL_COMMIT.value,
        Capability.MODEL_MUTATE.value,
        Capability.MODEL_RESTORE.value,
        Capability.PROCESS.value,
    }
)


class ToolDefinition(BaseModel):
    """A normalized tool contract understood by every agent model adapter."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None = None
    data_schema: dict[str, Any] | None = None
    annotations: dict[str, Any] = {}
    required_capabilities: tuple[str, ...] = ()
    tags: frozenset[str] = frozenset()
    permitted: bool = True
    requires_approval: bool = False
    source: str = ""
    native_name: str | None = None

    def provider_schema(self) -> dict[str, Any]:
        """Return the common function-tool shape used by model adapters."""

        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolCall(BaseModel):
    """One validated request from an agent to a selected toolset."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = {}


@runtime_checkable
class ToolSource(Protocol):
    """A source of callable JSON-schema tools."""

    namespace: str
    source_id: str

    async def list_tools(self) -> Sequence[ToolDefinition]: ...

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True)
class _Binding:
    definition: ToolDefinition
    source: ToolSource
    native_name: str


def _safe_part(value: str) -> str:
    cleaned = _NAME_PART.sub("_", value.strip()).strip("_")
    if not cleaned:
        raise ValueError("tool namespace must contain a letter, digit, underscore, or hyphen")
    return cleaned


def _public_name(namespace: str, name: str) -> str:
    return f"{_safe_part(namespace)}__{name}" if namespace else name


def _operation_tags(
    name: str,
    capabilities: Iterable[str],
    annotations: Mapping[str, Any],
) -> frozenset[str]:
    tags = {"ifc"}
    for capability in capabilities:
        family, _, action = capability.partition(":")
        tags.add(family)
        if action:
            tags.add(action)
    if annotations.get("readOnlyHint") is True:
        tags.add("read")
    if annotations.get("destructiveHint") is True:
        tags.update({"write", "destructive"})
    prefix = name.split("_", 1)[0]
    if prefix in {"get", "list", "find", "search", "describe", "orient"}:
        tags.add("read")
    if any(part in name for part in ("preview", "change_set")):
        tags.add("preview")
    if "viewer" in name or "highlight" in name or "color_theme" in name:
        tags.add("viewer")
    if "job" in name or "batch" in name or "workflow" in name:
        tags.add("automation")
    return frozenset(tags)


def definition_from_operation(
    payload: Mapping[str, Any],
    *,
    source: str = "ifc-console",
) -> ToolDefinition:
    """Normalize the dictionary returned by ``Workbench.tools()``."""

    capabilities = tuple(str(value) for value in payload.get("required_capabilities") or ())
    annotations = dict(payload.get("annotations") or {})
    return ToolDefinition(
        name=str(payload["name"]),
        native_name=str(payload["name"]),
        description=str(payload.get("description") or ""),
        input_schema=dict(payload.get("input_schema") or {}),
        output_schema=payload.get("output_schema"),
        data_schema=payload.get("data_schema"),
        annotations=annotations,
        required_capabilities=capabilities,
        tags=_operation_tags(str(payload["name"]), capabilities, annotations),
        permitted=bool(payload.get("permitted", True)),
        requires_approval=(
            bool(_APPROVAL_CAPABILITIES.intersection(capabilities))
            or annotations.get("destructiveHint") is True
        ),
        source=source,
    )


def _normalize_result(value: Any, *, source: str) -> dict[str, Any]:
    if isinstance(value, Envelope):
        payload = value.model_dump(mode="json")
    elif isinstance(value, BaseModel):
        payload = {"ok": True, "data": value.model_dump(mode="json"), "meta": {}}
    elif isinstance(value, Mapping):
        payload = dict(value)
        if "ok" not in payload:
            payload = {"ok": True, "data": payload, "meta": {}}
    else:
        payload = {"ok": True, "data": {"result": value}, "meta": {}}
    meta = dict(payload.get("meta") or {})
    meta.setdefault("tool_source", source)
    payload["meta"] = meta
    return payload


class Toolset:
    """An immutable set of tool bindings with filtering and namespacing."""

    def __init__(self, bindings: Iterable[_Binding] = ()) -> None:
        by_name: dict[str, _Binding] = {}
        for binding in bindings:
            name = binding.definition.name
            if name in by_name:
                previous = by_name[name].definition.source
                raise ValueError(
                    f"tool name collision for {name!r} between {previous!r} "
                    f"and {binding.definition.source!r}; namespace one source"
                )
            by_name[name] = binding
        self._bindings = by_name

    @classmethod
    async def build(
        cls,
        *sources: ToolSource,
        permitted_only: bool = True,
    ) -> Toolset:
        bindings: list[_Binding] = []
        for source in sources:
            namespace = getattr(source, "namespace", "")
            source_id = getattr(source, "source_id", type(source).__name__)
            for definition in await source.list_tools():
                if permitted_only and not definition.permitted:
                    continue
                native_name = definition.native_name or definition.name
                public_name = _public_name(namespace, definition.name)
                projected = definition.model_copy(
                    update={
                        "name": public_name,
                        "native_name": native_name,
                        "source": definition.source or source_id,
                    }
                )
                bindings.append(_Binding(projected, source, native_name))
        return cls(bindings)

    @property
    def definitions(self) -> tuple[ToolDefinition, ...]:
        return tuple(self._bindings[name].definition for name in sorted(self._bindings))

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    def __len__(self) -> int:
        return len(self._bindings)

    def __contains__(self, name: object) -> bool:
        return name in self._bindings

    def __add__(self, other: Toolset) -> Toolset:
        return Toolset((*self._bindings.values(), *other._bindings.values()))

    def schemas(self) -> list[dict[str, Any]]:
        return [definition.provider_schema() for definition in self.definitions]

    def include(
        self,
        *patterns: str,
        tags: Iterable[str] = (),
        capabilities: Iterable[str | Capability] = (),
    ) -> Toolset:
        """Select bindings matching a name glob and/or any requested metadata."""

        wanted_tags = {str(tag) for tag in tags}
        wanted_caps = {
            capability.value if isinstance(capability, Capability) else str(capability)
            for capability in capabilities
        }

        def selected(binding: _Binding) -> bool:
            definition = binding.definition
            name_match = bool(patterns) and any(
                fnmatch.fnmatchcase(definition.name, pattern)
                or fnmatch.fnmatchcase(binding.native_name, pattern)
                for pattern in patterns
            )
            tag_match = bool(wanted_tags.intersection(definition.tags))
            cap_match = bool(wanted_caps.intersection(definition.required_capabilities))
            return name_match or tag_match or cap_match

        if not patterns and not wanted_tags and not wanted_caps:
            return self
        return Toolset(binding for binding in self._bindings.values() if selected(binding))

    def exclude(
        self,
        *patterns: str,
        tags: Iterable[str] = (),
        capabilities: Iterable[str | Capability] = (),
    ) -> Toolset:
        rejected = self.include(*patterns, tags=tags, capabilities=capabilities)
        names = set(rejected.names)
        return Toolset(binding for name, binding in self._bindings.items() if name not in names)

    def require(self, name: str) -> ToolDefinition:
        binding = self._bindings.get(name)
        if binding is None:
            raise KeyError(name)
        return binding.definition

    async def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        binding = self._bindings.get(name)
        if binding is None:
            return {
                "ok": False,
                "error": {
                    "code": "TOOL_NOT_FOUND",
                    "message": f"no selected tool named {name!r}",
                    "hint": f"Selected tools: {', '.join(self.names)}",
                },
                "meta": {"tool_source": "toolset"},
            }
        try:
            value = await binding.source.call_tool(binding.native_name, arguments or {})
            return _normalize_result(value, source=binding.definition.source)
        except ValidationError as exc:
            return {
                "ok": False,
                "error": {
                    "code": "INVALID_INPUT",
                    "message": "tool arguments failed validation",
                    "hint": "; ".join(
                        f"{'.'.join(str(part) for part in row['loc'])}: {row['msg']}"
                        for row in exc.errors(include_url=False, include_input=False)
                    ),
                },
                "meta": {"tool_source": binding.definition.source},
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": {
                    "code": "TOOL_SOURCE_FAILED",
                    "message": f"tool source failed with {type(exc).__name__}",
                    "hint": "Inspect the application log and tool-source health.",
                },
                "meta": {"tool_source": binding.definition.source},
            }

    async def aclose(self) -> None:
        seen: set[int] = set()
        for binding in self._bindings.values():
            identity = id(binding.source)
            if identity in seen:
                continue
            seen.add(identity)
            await binding.source.aclose()


class FunctionToolSource:
    """Trusted application functions exposed beside IFC or MCP tools."""

    def __init__(self, *, namespace: str = "company", source_id: str | None = None) -> None:
        self.namespace = _safe_part(namespace) if namespace else ""
        self.source_id = source_id or (f"python:{self.namespace}" if self.namespace else "python")
        self._registry = OperationRegistry()
        self._metadata: dict[str, tuple[frozenset[str], bool]] = {}

    def tool(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        annotations: OperationAnnotations | None = None,
        data_model: type[BaseModel] | None = None,
        required_capabilities: Iterable[Capability | str] = (),
        tags: Iterable[str] = (),
        requires_approval: bool = False,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register an async or sync trusted function as an application tool."""

        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            operation_name = name or fn.__name__
            wrapped = self._registry.tool(
                name=operation_name,
                description=description,
                annotations=annotations,
                data_model=data_model,
                required_capabilities=required_capabilities,
            )(fn)  # type: ignore[arg-type]
            self._metadata[operation_name] = (
                frozenset({"python", "company", *(str(tag) for tag in tags)}),
                requires_approval,
            )
            return wrapped

        return decorate

    async def list_tools(self) -> Sequence[ToolDefinition]:
        definitions: list[ToolDefinition] = []
        for operation in self._registry.definitions():
            tags, approval = self._metadata.get(operation.name, (frozenset(), False))
            capabilities = tuple(capability.value for capability in operation.required_capabilities)
            annotations = operation.annotations.model_dump(exclude_none=True)
            definitions.append(
                ToolDefinition(
                    name=operation.name,
                    native_name=operation.name,
                    description=operation.description,
                    input_schema=operation.input_schema,
                    output_schema=operation.output_schema,
                    data_schema=operation.data_schema,
                    annotations=annotations,
                    required_capabilities=capabilities,
                    tags=tags,
                    requires_approval=(
                        approval or bool(_APPROVAL_CAPABILITIES.intersection(capabilities))
                    ),
                    source=self.source_id,
                )
            )
        return definitions

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        specification = self._registry.require(name)
        validated = specification.validate_arguments(dict(arguments))
        result = specification.handler(**validated)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def aclose(self) -> None:
        return None


ToolCallNext = Callable[[ToolCall], Awaitable[dict[str, Any]]]


@runtime_checkable
class ToolMiddleware(Protocol):
    """Application middleware around a tool call."""

    async def __call__(self, call: ToolCall, call_next: ToolCallNext) -> dict[str, Any]: ...


__all__ = [
    "FunctionToolSource",
    "ToolCall",
    "ToolCallNext",
    "ToolDefinition",
    "ToolMiddleware",
    "ToolSource",
    "Toolset",
    "definition_from_operation",
]
