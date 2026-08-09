"""Trusted plugin discovery, allowlisting, and atomic registration."""

from __future__ import annotations

from types import SimpleNamespace

from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationRegistry
from ifc_console.plugins import PluginManager


class FakeAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields):
        self.events.append((event, fields))


class FakeEntryPoint:
    def __init__(self, name: str, value: str, loaded) -> None:
        self.name = name
        self.value = value
        self._loaded = loaded
        self.loads = 0
        self.dist = None

    def load(self):
        self.loads += 1
        return self._loaded


def _core(*, enabled: bool, allow: list[str]):
    return SimpleNamespace(
        settings=SimpleNamespace(plugins=SimpleNamespace(enabled=enabled, allow=allow)),
        audit=FakeAudit(),
    )


class ExamplePlugin:
    manifest = {
        "api_version": "1",
        "name": "example",
        "version": "1.2.3",
        "description": "Example checks.",
    }

    def register(self, api) -> None:
        @api.registry.tool(
            name="example_status",
            description="Return the example status.",
            required_capabilities=[Capability.MODEL_READ],
        )
        async def example_status() -> dict:
            return {"ok": True}


def test_disabled_plugins_are_discovered_without_importing() -> None:
    entry = FakeEntryPoint("example", "example_plugin:plugin", ExamplePlugin)
    manager = PluginManager([entry])
    records = manager.load_configured(_core(enabled=False, allow=["example"]), OperationRegistry())
    assert records[0].status == "disabled"
    assert entry.loads == 0


def test_allowlisted_inventory_reports_configured_without_importing() -> None:
    entry = FakeEntryPoint("example", "example_plugin:plugin", ExamplePlugin)
    manager = PluginManager([entry])
    records = manager.inventory(enabled=True, allow={"example"})
    assert records[0].status == "configured"
    assert entry.loads == 0


def test_duplicate_allowed_entry_point_names_fail_closed_without_importing() -> None:
    first = FakeEntryPoint("example", "first:plugin", ExamplePlugin)
    second = FakeEntryPoint("EXAMPLE", "second:plugin", ExamplePlugin)
    records = PluginManager([first, second]).load_configured(
        _core(enabled=True, allow=["example"]), OperationRegistry()
    )

    assert [record.status for record in records] == ["error", "error"]
    assert all("multiple installed" in (record.error or "") for record in records)
    assert first.loads == second.loads == 0


def test_unlisted_plugin_is_never_imported() -> None:
    entry = FakeEntryPoint("example", "example_plugin:plugin", ExamplePlugin)
    manager = PluginManager([entry])
    records = manager.load_configured(_core(enabled=True, allow=[]), OperationRegistry())
    assert records[0].status == "not_allowed"
    assert entry.loads == 0


def test_allowlisted_plugin_registers_typed_capability_operations() -> None:
    entry = FakeEntryPoint("example", "example_plugin:plugin", ExamplePlugin)
    registry = OperationRegistry()
    manager = PluginManager([entry])
    core = _core(enabled=True, allow=["EXAMPLE"])
    records = manager.load_configured(core, registry)

    assert records[0].status == "loaded"
    assert records[0].operations == ("example_status",)
    assert registry.require("example_status").required_capabilities == (
        Capability.MODEL_READ,
    )
    assert core.audit.events[0][0] == "plugin_loaded"


def test_failed_registration_rolls_back_partial_operations() -> None:
    class BrokenPlugin(ExamplePlugin):
        def register(self, api) -> None:
            super().register(api)
            raise RuntimeError("broken setup")

    entry = FakeEntryPoint("example", "broken:plugin", BrokenPlugin)
    registry = OperationRegistry()
    records = PluginManager([entry]).load_configured(
        _core(enabled=True, allow=["example"]), registry
    )
    assert records[0].status == "error"
    assert "example_status" not in registry


def test_manifest_name_must_match_entry_point() -> None:
    entry = FakeEntryPoint("different", "example_plugin:plugin", ExamplePlugin)
    records = PluginManager([entry]).load_configured(
        _core(enabled=True, allow=["different"]), OperationRegistry()
    )
    assert records[0].status == "error"
    assert "does not match" in (records[0].error or "")


def test_allowed_but_missing_plugin_is_reported() -> None:
    records = PluginManager([]).load_configured(
        _core(enabled=True, allow=["missing"]), OperationRegistry()
    )
    assert records[0].status == "missing"
