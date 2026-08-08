"""Transport-neutral operation registry and adapter conformance."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.app import AppCore
from ifc_console.application.operations import build_operations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope
from ifc_console.mcp.server import build_mcp
from ifc_console.settings import SettingsStore


def _core(home: Path) -> AppCore:
    return AppCore(SettingsStore(home=home, project_dir=home, env={}))


def test_registry_refuses_duplicate_operation_names() -> None:
    registry = OperationRegistry()

    async def first() -> Envelope:
        return Envelope(ok=True)

    async def second() -> Envelope:
        return Envelope(ok=True)

    registry.tool(name="same")(first)
    with pytest.raises(ValueError, match="already registered"):
        registry.tool(name="same")(second)


async def test_application_service_validates_arguments_before_execution(tmp_path: Path) -> None:
    core = _core(tmp_path)
    try:
        service = build_operations(core)
        result = await service.call("query_elements", {"query": "IfcWall", "limit": 0})
        assert result.ok is False
        assert result.error is not None
        assert result.error.code == "INVALID_INPUT"
    finally:
        core.shutdown()


async def test_mcp_is_a_projection_of_the_operation_registry(tmp_path: Path) -> None:
    core = _core(tmp_path)
    try:
        build_operations(core)
        definitions = {
            definition.name: definition for definition in core.operation_service.definitions()
        }
        mcp = build_mcp(core)
        tools = {tool.name: tool for tool in await mcp.list_tools()}

        assert set(tools) == set(definitions)
        for name, definition in definitions.items():
            tool = tools[name]
            assert tool.inputSchema == definition.input_schema
            assert tool.outputSchema == definition.output_schema
            assert tool.annotations.readOnlyHint == definition.annotations.readOnlyHint
            assert tool.annotations.destructiveHint == definition.annotations.destructiveHint
    finally:
        core.shutdown()


def test_query_and_validation_publish_operation_specific_data_schemas(
    tmp_path: Path,
) -> None:
    core = _core(tmp_path)
    try:
        build_operations(core)
        query = core.operations.require("query_elements")
        validation = core.operations.require("validate_model")

        assert query.data_schema is not None
        assert "rows" in query.data_schema["properties"]
        assert validation.data_schema is not None
        assert {"valid", "issue_count", "issues"} <= set(validation.data_schema["properties"])
    finally:
        core.shutdown()
