"""Build the public Python SDK and plugin API contract snapshot."""

from __future__ import annotations

import dataclasses
import enum
import inspect
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

SDK_GOLDEN_PATH = Path(__file__).parent / "golden" / "sdk_contract.json"


def _annotation(value: Any) -> str:
    if value is inspect.Signature.empty:
        return ""
    rendered = value if isinstance(value, str) else str(value)
    return rendered.replace("typing.", "")


def _method_contract(owner: type[Any]) -> dict[str, Any]:
    methods: dict[str, Any] = {}
    for name, method in inspect.getmembers(owner):
        if name.startswith("_") or not (inspect.isfunction(method) or inspect.ismethod(method)):
            continue
        signature = inspect.signature(method)
        parameters = []
        for parameter in signature.parameters.values():
            if parameter.name in {"self", "cls"}:
                continue
            item: dict[str, Any] = {
                "name": parameter.name,
                "kind": parameter.kind.name,
                "annotation": _annotation(parameter.annotation),
            }
            if parameter.default is inspect.Signature.empty:
                item["required"] = True
            else:
                item["default"] = repr(parameter.default)
            parameters.append(item)
        methods[name] = {
            "async": inspect.iscoroutinefunction(method),
            "parameters": parameters,
            "returns": _annotation(signature.return_annotation),
        }
    return methods


def _dataclass_contract(owner: type[Any]) -> list[dict[str, Any]]:
    fields = []
    for field in dataclasses.fields(owner):
        item: dict[str, Any] = {
            "name": field.name,
            "annotation": _annotation(field.type),
        }
        if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
            item["required"] = True
        elif field.default is not dataclasses.MISSING:
            item["default"] = repr(field.default)
        else:
            factory = field.default_factory
            item["default_factory"] = getattr(factory, "__qualname__", repr(factory))
        fields.append(item)
    return fields


def build_sdk_contract() -> dict[str, Any]:
    import ifc_console

    exports = sorted(ifc_console.__all__)
    models: dict[str, Any] = {}
    enums: dict[str, Any] = {}
    dataclasses_: dict[str, Any] = {}
    aliases: dict[str, str] = {}

    for name in exports:
        if name == "__version__":
            continue
        value = getattr(ifc_console, name)
        if inspect.isclass(value) and issubclass(value, BaseModel):
            models[name] = value.model_json_schema()
        elif inspect.isclass(value) and issubclass(value, enum.Enum):
            enums[name] = {member.name: member.value for member in value}
        elif inspect.isclass(value) and dataclasses.is_dataclass(value):
            dataclasses_[name] = _dataclass_contract(value)
        elif not inspect.isclass(value):
            aliases[name] = _annotation(value)

    method_owners = (
        ifc_console.Workbench,
        ifc_console.AsyncWorkbench,
        ifc_console.PluginAPI,
        ifc_console.OperationPlugin,
        ifc_console.LocalRuntime,
        ifc_console.ConsoleRuntime,
        ifc_console.WorkspaceClient,
        ifc_console.Toolset,
        ifc_console.FunctionToolSource,
        ifc_console.McpToolSource,
        ifc_console.Agent,
        ifc_console.AgentToolSource,
        ifc_console.ProviderModel,
        ifc_console.InMemoryThreadStore,
        ifc_console.JsonThreadStore,
    )
    return {
        "version": ifc_console.__version__,
        "exports": exports,
        "aliases": aliases,
        "dataclasses": dataclasses_,
        "enums": enums,
        "methods": {owner.__name__: _method_contract(owner) for owner in method_owners},
        "models": models,
    }


def dump_sdk_contract(contract: dict[str, Any]) -> str:
    return json.dumps(contract, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
