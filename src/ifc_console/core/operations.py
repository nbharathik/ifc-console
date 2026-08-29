"""Typed operation definitions and the transport-neutral registry."""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin, get_type_hints

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, create_model

from ifc_console.core.capabilities import Capability, normalize_capabilities
from ifc_console.core.results import Envelope

OperationHandler = Callable[..., Awaitable[Any]]


def operation_tags(
    name: str,
    capabilities: Iterable[Capability | str],
    annotations: OperationAnnotations | None,
) -> frozenset[str]:
    """Stable discovery tags shared by agent adapters and MCP metadata.

    Tags are descriptive hints, not a permission boundary. Call-time
    capability policy remains authoritative.
    """
    normalized = tuple(str(capability) for capability in capabilities)
    tags = {"ifc"}
    for capability in normalized:
        family, _, action = capability.partition(":")
        tags.add(family)
        if action:
            tags.add(action)
    if annotations is not None and annotations.readOnlyHint is True:
        tags.add("read")
    if annotations is not None and annotations.destructiveHint is True:
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


# The same concept is spelled differently across the surface: query_elements
# takes `query`, measure_elements takes `selector`, search_elements takes
# `term`. Renaming any of them breaks callers, so the other spellings are
# accepted instead. AliasChoices keeps the canonical name in the published
# schema, so this costs nothing to advertise and saves a wasted round.
ARGUMENT_ALIASES: dict[str, tuple[str, ...]] = {
    "query": ("selector", "term"),
    "selector": ("query", "term"),
    "term": ("query", "selector"),
    "global_ids": ("guids", "ids"),
    "guids": ("global_ids", "ids"),
    "path": ("file_path",),
    "output_path": ("path", "file_path"),
    "ids_path": ("path", "file_path"),
    "limit": ("max_results",),
    "model": ("model_id",),
    "model_id": ("model",),
}


def argument_aliases(name: str, taken: Iterable[str]) -> tuple[str, ...]:
    """Alternative spellings accepted for one argument of one operation.

    An alias that is already a parameter of the same operation would be
    ambiguous, so it is dropped rather than guessed at.
    """
    reserved = set(taken)
    return tuple(alias for alias in ARGUMENT_ALIASES.get(name, ()) if alias not in reserved)


def _auto_title(key: str) -> str:
    return key.replace("_", " ").title().strip()


def strip_auto_titles(schema: Any) -> Any:
    """Drop the titles Pydantic derives from field names.

    Every one of them restates the key it sits under, so across a full tool
    listing they are thousands of characters that tell a model nothing.
    Titles someone wrote by hand differ from the derived form and stay.
    """
    if isinstance(schema, list):
        return [strip_auto_titles(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: strip_auto_titles(value) for key, value in schema.items()}
    properties = out.get("properties")
    if out.get("type") == "object" and isinstance(properties, dict):
        for key, sub in properties.items():
            if isinstance(sub, dict) and sub.get("title") in (_auto_title(key), key):
                sub.pop("title")
    return out


# An output schema is paid for on every listing, so it has to be cheaper than
# the shape it explains. Past this the deeply nested job and change-set models
# cost more context than a caller saves by reading them.
MAX_OUTPUT_SCHEMA_CHARS = 2_500


def _has_list(annotation: Any) -> bool:
    if annotation is list or get_origin(annotation) is list:
        return True
    return any(_has_list(argument) for argument in get_args(annotation))


@functools.lru_cache(maxsize=512)
def _raw_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema generation dominates any tool listing, and an
    operation's models are fixed once it is registered. Callers never see this
    dict: strip_auto_titles rebuilds it, so each of them gets a fresh copy.
    """
    return model.model_json_schema()


@functools.lru_cache(maxsize=512)
def _is_bulk(model: type[BaseModel]) -> bool:
    """Does this payload grow with the model?

    Declaring an output schema also makes the transport send the result twice,
    as text and as structured content. A one-off schema never repays that on a
    payload measured in thousands of characters per call.
    """
    return any(_has_list(field.annotation) for field in model.model_fields.values())


class _DescribedEnvelope(Envelope):
    # Handlers return a plain Envelope, so the specialization has to accept one.
    model_config = ConfigDict(from_attributes=True)


@functools.cache
def _envelope_for(data_model: type[BaseModel]) -> type[Envelope]:
    """An Envelope whose `data` documents one tool's payload shape.

    The permissive mapping stays first in the union on purpose: a paged or
    truncated payload carries an extra `truncation` key and must survive the
    round trip intact. The declared model is documentation for the caller,
    not a filter on the way out.
    """
    return create_model(
        f"{data_model.__name__}Envelope",
        __base__=_DescribedEnvelope,
        data=(dict[str, Any] | data_model | None, None),
    )


class OperationAnnotations(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str | None = None
    readOnlyHint: bool | None = None
    destructiveHint: bool | None = None
    idempotentHint: bool | None = None
    openWorldHint: bool | None = None


class OperationImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: bytes
    format: str


class OperationDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    data_schema: dict[str, Any] | None = None
    annotations: OperationAnnotations
    required_capabilities: tuple[Capability, ...] = ()


def _argument_model(fn: OperationHandler) -> type[BaseModel]:
    signature = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)
    names = set(signature.parameters)
    fields: dict[str, Any] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, Any)
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        # Public operation arguments such as `schema` can share a name with a
        # BaseModel method. Keep the public alias and use a private field name
        # so Pydantic does not emit one warning per registry construction.
        field_name = f"{name}_" if hasattr(BaseModel, name) else name
        if field_name != name:
            annotation = Annotated[annotation, Field(alias=name)]
        else:
            aliases = argument_aliases(name, names)
            if aliases:
                annotation = Annotated[
                    annotation, Field(validation_alias=AliasChoices(name, *aliases))
                ]
        fields[field_name] = (annotation, default)
    return create_model(
        f"{fn.__name__}Arguments",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )


@dataclass(frozen=True)
class OperationSpec:
    name: str
    description: str
    annotations: OperationAnnotations
    handler: OperationHandler
    argument_model: type[BaseModel]
    structured_output: bool
    data_model: type[BaseModel] | None = None
    required_capabilities: tuple[Capability, ...] = ()

    @property
    def input_schema(self) -> dict[str, Any]:
        schema = strip_auto_titles(_raw_json_schema(self.argument_model))
        # FastMCP omits this keyword from the public tool schema. Keep the SDK
        # definition byte-compatible while the application validator still
        # rejects unknown arguments through the model's extra="forbid" rule.
        schema.pop("additionalProperties", None)
        schema.pop("title", None)  # always "<name>Arguments"; the tool name says it
        return schema

    @property
    def envelope_model(self) -> type[Envelope] | None:
        """The Envelope specialization this operation returns, if it declared one."""
        if self.output_schema is None:
            return None
        return _envelope_for(self.data_model)  # type: ignore[arg-type]

    @property
    def output_schema(self) -> dict[str, Any] | None:
        """The result contract, or None when the operation never declared one.

        A bare Envelope schema is identical for every tool, so publishing it
        costs a client thousands of characters and teaches it nothing. A tool
        publishes one only when it declared a data shape that is small and
        bounded; the rest publish nothing, which also stops their results
        crossing the wire twice. `data_schema` still carries every declared
        shape for callers that want it off the listing path.
        """
        if not self.structured_output or self.data_model is None:
            return None
        if _is_bulk(self.data_model):
            return None
        schema = strip_auto_titles(_raw_json_schema(_envelope_for(self.data_model)))
        return schema if len(json.dumps(schema)) <= MAX_OUTPUT_SCHEMA_CHARS else None

    @property
    def data_schema(self) -> dict[str, Any] | None:
        return strip_auto_titles(_raw_json_schema(self.data_model)) if self.data_model else None

    @property
    def inputSchema(self) -> dict[str, Any]:
        return self.input_schema

    @property
    def outputSchema(self) -> dict[str, Any] | None:
        return self.output_schema

    def definition(self) -> OperationDefinition:
        return OperationDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            data_schema=self.data_schema,
            annotations=self.annotations,
            required_capabilities=self.required_capabilities,
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = self.argument_model.model_validate(arguments)
        # Handlers are typed Python callables, so preserve validated nested
        # BaseModel instances instead of serializing them back to dictionaries.
        # Only the top-level generated field name needs to be mapped to its
        # public alias, such as schema_ back to schema.
        return {
            field.alias or field_name: getattr(validated, field_name)
            for field_name, field in self.argument_model.model_fields.items()
        }


class OperationRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, OperationSpec] = {}
        self.handlers: dict[str, OperationHandler] = {}

    def tool(
        self,
        name: str | None = None,
        *,
        description: str | None = None,
        annotations: OperationAnnotations | None = None,
        structured_output: bool | None = None,
        data_model: type[BaseModel] | None = None,
        required_capabilities: Iterable[Capability | str] | None = None,
        **_ignored: Any,
    ) -> Callable[[OperationHandler], OperationHandler]:
        def decorate(fn: OperationHandler) -> OperationHandler:
            operation_name = name or fn.__name__
            if operation_name in self._specs:
                raise ValueError(f"operation {operation_name!r} is already registered")
            spec = OperationSpec(
                name=operation_name,
                description=description or inspect.getdoc(fn) or "",
                annotations=annotations or OperationAnnotations(),
                handler=fn,
                argument_model=_argument_model(fn),
                structured_output=structured_output is not False,
                data_model=data_model,
                required_capabilities=normalize_capabilities(
                    list(required_capabilities)
                    if required_capabilities is not None
                    else list(_default_capabilities(operation_name, annotations))
                ),
            )
            self._specs[operation_name] = spec
            self.handlers[operation_name] = fn
            return fn

        return decorate

    def get(self, name: str) -> OperationSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> OperationSpec:
        spec = self.get(name)
        if spec is None:
            raise KeyError(name)
        return spec

    def remove_tool(self, name: str) -> None:
        self._specs.pop(name, None)
        self.handlers.pop(name, None)

    def _snapshot(self) -> dict[str, OperationSpec]:
        return dict(self._specs)

    def _restore(self, snapshot: dict[str, OperationSpec]) -> None:
        self._specs.clear()
        self._specs.update(snapshot)
        self.handlers.clear()
        self.handlers.update({name: spec.handler for name, spec in snapshot.items()})

    def definitions(self) -> list[OperationDefinition]:
        return [self._specs[name].definition() for name in sorted(self._specs)]

    def specs(self, names: Iterable[str] | None = None) -> list[OperationSpec]:
        selected = set(names) if names is not None else None
        return [
            self._specs[name]
            for name in sorted(self._specs)
            if selected is None or name in selected
        ]

    async def list_tools(self) -> list[OperationSpec]:
        return self.specs()

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)


_CAPABILITY_GROUPS: tuple[tuple[frozenset[str], tuple[Capability, ...]], ...] = (
    (
        frozenset({"list_ifc_files", "find_files"}),
        (Capability.FILE_READ,),
    ),
    (
        frozenset({"open_ifc_file", "attach"}),
        (Capability.FILE_READ, Capability.SESSION_MANAGE),
    ),
    (
        frozenset({"detach", "set_active_model"}),
        (Capability.SESSION_MANAGE,),
    ),
    (
        frozenset({"save_ifc_file"}),
        (Capability.MODEL_COMMIT, Capability.FILE_WRITE),
    ),
    (
        frozenset({"export_csv"}),
        (Capability.MODEL_READ, Capability.FILE_WRITE, Capability.ARTIFACT_WRITE),
    ),
    (
        frozenset({"validate_ids"}),
        (Capability.MODEL_READ, Capability.FILE_READ),
    ),
    (
        frozenset(
            {
                "detect_clashes",
                "get_element_geometry",
                "inspect_element_mesh",
                "measure_elements",
                "measure_directional_extent",
                "measure_distance",
                "measure_local_thickness",
                "slice_element_mesh",
                "analyze_element_geometry",
            }
        ),
        (Capability.MODEL_READ, Capability.GEOMETRY),
    ),
    (
        frozenset({"export_measurement_report"}),
        (
            Capability.MODEL_READ,
            Capability.GEOMETRY,
            Capability.FILE_WRITE,
            Capability.ARTIFACT_WRITE,
        ),
    ),
    (
        frozenset(
            {
                "search_ifc_knowledge",
                "get_knowledge_record",
                "list_project_documents",
                "get_api_docs",
                "get_measurement_recipe",
            }
        ),
        (Capability.KNOWLEDGE_READ,),
    ),
    (
        frozenset({"get_project_reference_image"}),
        (Capability.KNOWLEDGE_READ, Capability.FILE_READ),
    ),
    (
        frozenset({"execute_ifc_code"}),
        (Capability.GENERATED_CODE,),
    ),
    (
        frozenset({"submit_validation_job"}),
        (Capability.MODEL_READ, Capability.JOB_SUBMIT, Capability.ARTIFACT_WRITE),
    ),
    (
        frozenset({"get_job", "list_jobs"}),
        (Capability.JOB_READ,),
    ),
    (
        frozenset({"cancel_job"}),
        (Capability.JOB_CANCEL,),
    ),
    (
        frozenset({"get_artifact", "list_artifacts", "get_change_set"}),
        (Capability.ARTIFACT_READ,),
    ),
    (
        frozenset({"preview_property_change", "preview_classification_assignment"}),
        (Capability.MODEL_PREVIEW, Capability.ARTIFACT_WRITE),
    ),
    (
        frozenset(
            {"get_viewer_selection", "get_viewer_measurements", "get_viewer_screenshot"}
        ),
        (Capability.VIEWER_READ,),
    ),
    (
        frozenset({"highlight_elements", "apply_color_theme", "control_viewer"}),
        (Capability.VIEWER_CONTROL,),
    ),
)


def _default_capabilities(
    name: str, annotations: OperationAnnotations | None
) -> tuple[Capability, ...]:
    """Compatibility mapping until every built-in declaration is explicit.

    Unknown third-party operations fail closed: non-read-only operations need
    model mutation authority. Plugins should always declare their exact set.
    """
    for names, capabilities in _CAPABILITY_GROUPS:
        if name in names:
            return capabilities
    if annotations is not None and annotations.readOnlyHint is False:
        return (Capability.MODEL_MUTATE,)
    return (Capability.MODEL_READ,)
