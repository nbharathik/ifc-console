"""Typed operation definitions and the transport-neutral registry."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, get_type_hints

from pydantic import BaseModel, ConfigDict, create_model

from ifc_console.core.results import Envelope

OperationHandler = Callable[..., Awaitable[Any]]


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


def _argument_model(fn: OperationHandler) -> type[BaseModel]:
    signature = inspect.signature(fn)
    hints = get_type_hints(fn, include_extras=True)
    fields: dict[str, tuple[Any, Any]] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(name, Any)
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[name] = (annotation, default)
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

    @property
    def input_schema(self) -> dict[str, Any]:
        schema = self.argument_model.model_json_schema()
        # FastMCP omits this keyword from the public tool schema. Keep the SDK
        # definition byte-compatible while the application validator still
        # rejects unknown arguments through the model's extra="forbid" rule.
        schema.pop("additionalProperties", None)
        return schema

    @property
    def output_schema(self) -> dict[str, Any] | None:
        return Envelope.model_json_schema() if self.structured_output else None

    @property
    def data_schema(self) -> dict[str, Any] | None:
        return self.data_model.model_json_schema() if self.data_model else None

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
        )

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        validated = self.argument_model.model_validate(arguments)
        return {name: getattr(validated, name) for name in self.argument_model.model_fields}


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
