"""Installed product-extension discovery and lifecycle boundaries."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from ifc_console import extensions as extension_module
from ifc_console.core.operations import OperationRegistry
from ifc_console.extensions import BrowserPanel, ExtensionManager


class FakeEntryPoint:
    def __init__(
        self,
        name: str,
        value: str,
        exported: Any,
        *,
        distribution: str | None = None,
        distribution_version: str | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self.exported = exported
        self.loads = 0
        self.dist = (
            SimpleNamespace(name=distribution, version=distribution_version)
            if distribution is not None
            else None
        )

    def load(self) -> Any:
        self.loads += 1
        if isinstance(self.exported, BaseException):
            raise self.exported
        return self.exported


class RecordingExtension:
    def __init__(
        self,
        name: str,
        events: list[str] | None = None,
        *,
        manifest: dict[str, Any] | None = None,
        routes: list[Any] | None = None,
        status: dict[str, Any] | None = None,
        panel: BrowserPanel | dict[str, Any] | None = None,
        attach_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.manifest = manifest or {
            "api_version": "1",
            "name": name,
            "version": "1.2.3",
            "description": f"The {name} extension.",
        }
        self.name = name
        self.events = events if events is not None else []
        self.routes = routes or []
        self.status_value = status or {}
        self.panel = panel
        self.attach_error = attach_error
        self.close_error = close_error
        self.attach_calls = 0
        self.register_calls = 0
        self.close_calls = 0

    def attach(self, core: Any) -> object:
        self.attach_calls += 1
        self.events.append(f"attach:{self.name}")
        if self.attach_error is not None:
            raise self.attach_error
        return {"extension": self.name, "core": core}

    def register_operations(
        self, core: Any, registry: OperationRegistry, state: object
    ) -> None:
        self.register_calls += 1
        self.events.append(f"register:{self.name}")

        @registry.tool(name=f"{self.name}_status")
        async def extension_status() -> dict[str, Any]:
            return {"core": core, "state": state}

    def http_routes(self, core: Any, state: object) -> list[Any]:
        return list(self.routes)

    def status(self, core: Any, state: object) -> dict[str, Any]:
        return dict(self.status_value)

    def browser_panel(
        self, core: Any, state: object
    ) -> BrowserPanel | dict[str, Any] | None:
        return self.panel

    def close(self, core: Any, state: object) -> None:
        self.close_calls += 1
        self.events.append(f"close:{self.name}")
        if self.close_error is not None:
            raise self.close_error


def _entry(extension: RecordingExtension, *, value: str | None = None) -> FakeEntryPoint:
    return FakeEntryPoint(
        extension.name,
        value or f"example_{extension.name}:extension",
        extension,
        distribution=f"ifc-console-{extension.name}",
        distribution_version="4.5.6",
    )


def test_discovery_uses_the_extension_group_and_is_deterministic(monkeypatch) -> None:
    entries = [
        FakeEntryPoint("zeta", "zeta:extension", object()),
        FakeEntryPoint("alpha", "zulu:extension", object()),
        FakeEntryPoint("alpha", "alpha:extension", object()),
    ]
    calls: list[dict[str, str]] = []

    def installed_entry_points(**kwargs: str) -> list[FakeEntryPoint]:
        calls.append(kwargs)
        return entries

    monkeypatch.setattr(extension_module.metadata, "entry_points", installed_entry_points)

    discovered = ExtensionManager().discover()

    assert calls == [{"group": "ifc_console.extensions"}]
    assert [(entry.name, entry.value) for entry in discovered] == [
        ("alpha", "alpha:extension"),
        ("alpha", "zulu:extension"),
        ("zeta", "zeta:extension"),
    ]
    assert all(entry.loads == 0 for entry in entries)


@pytest.mark.parametrize(
    ("entry_name", "manifest", "error_type"),
    [
        (
            "invalid_api",
            {"api_version": "2", "name": "invalid_api", "version": "1.0.0"},
            "ValidationError",
        ),
        (
            "unexpected",
            {
                "api_version": "1",
                "name": "unexpected",
                "version": "1.0.0",
                "unknown": True,
            },
            "ValidationError",
        ),
        (
            "entry_name",
            {"api_version": "1", "name": "manifest_name", "version": "1.0.0"},
            "ValueError",
        ),
    ],
)
def test_invalid_manifests_fail_before_attach(
    entry_name: str, manifest: dict[str, Any], error_type: str
) -> None:
    extension = RecordingExtension(entry_name, manifest=manifest)
    entry = FakeEntryPoint(entry_name, "broken:extension", extension)

    records = ExtensionManager([entry]).attach(SimpleNamespace())

    assert len(records) == 1
    assert records[0].status == "error"
    assert records[0].manifest is None
    assert records[0].error is not None and records[0].error.startswith(error_type)
    assert extension.attach_calls == 0


def test_attach_and_operation_registration_are_idempotent() -> None:
    core = SimpleNamespace(name="core")
    extension = RecordingExtension("example")
    entry = _entry(extension)
    manager = ExtensionManager([entry])
    registry = OperationRegistry()

    with pytest.raises(RuntimeError, match="attach before registering"):
        manager.register_operations(registry)

    first = manager.attach(core)
    second = manager.attach(core)
    manager.register_operations(registry)
    manager.register_operations(registry)

    assert first == second
    assert entry.loads == 1
    assert extension.attach_calls == 1
    assert extension.register_calls == 1
    assert manager.available("example") is True
    assert manager.state("example") == {"extension": "example", "core": core}
    assert manager.require("example") is manager.state("example")
    assert "example_status" in registry
    assert first[0].distribution == "ifc-console-example"
    assert first[0].distribution_version == "4.5.6"


def test_route_collisions_are_rejected_across_extensions() -> None:
    route_one = SimpleNamespace(path="/api/shared", methods={"GET", "HEAD"})
    route_two = SimpleNamespace(path="/api/shared", methods={"GET"})
    manager = ExtensionManager(
        [
            _entry(RecordingExtension("alpha", routes=[route_one])),
            _entry(RecordingExtension("beta", routes=[route_two])),
        ]
    )
    manager.attach(SimpleNamespace())

    with pytest.raises(RuntimeError, match=r"extension route collision: GET /api/shared"):
        manager.http_routes()


def test_status_payloads_must_be_json_safe() -> None:
    good = RecordingExtension(
        "good",
        status={"ready": True, "models": ["one", "two"], "limits": {"runs": 4}},
    )
    manager = ExtensionManager([_entry(good)])
    manager.attach(SimpleNamespace())

    status = manager.status()

    assert status == {
        "good": {
            "ready": True,
            "models": ["one", "two"],
            "limits": {"runs": 4},
        }
    }
    assert json.loads(json.dumps(status)) == status

    unsafe = RecordingExtension("unsafe", status={"path": Path("secret.txt")})
    unsafe_manager = ExtensionManager([_entry(unsafe)])
    unsafe_manager.attach(SimpleNamespace())
    with pytest.raises(TypeError, match="not JSON serializable"):
        unsafe_manager.status()


def test_browser_panels_are_validated_and_serialized() -> None:
    extension = RecordingExtension(
        "agents",
        panel={
            "name": "agents",
            "label": "Agents",
            "module_url": "/agents/static/panel.js",
            "stylesheet_url": "/agents/static/panel.css",
            "standalone_url": "/agents?mode=standalone",
        },
    )
    manager = ExtensionManager([_entry(extension)])
    manager.attach(SimpleNamespace())

    assert manager.browser_panels() == [
        {
            "name": "agents",
            "label": "Agents",
            "module_url": "/agents/static/panel.js",
            "stylesheet_url": "/agents/static/panel.css",
            "standalone_url": "/agents?mode=standalone",
        }
    ]

    extension.panel = {"name": "agents", "label": "Agents", "module_url": "https://bad"}
    with pytest.raises(ValidationError):
        manager.browser_panels()


def test_one_failed_extension_does_not_block_the_next() -> None:
    broken = RecordingExtension(
        "broken", attach_error=RuntimeError("extension setup failed")
    )
    healthy = RecordingExtension("healthy")
    manager = ExtensionManager([_entry(healthy), _entry(broken)])

    records = manager.attach(SimpleNamespace())

    assert [(record.name, record.status) for record in records] == [
        ("broken", "error"),
        ("healthy", "loaded"),
    ]
    assert manager.available("broken") is False
    assert manager.available("healthy") is True
    assert healthy.attach_calls == 1


def test_close_is_reverse_order_once_and_isolates_failures() -> None:
    events: list[str] = []
    first = RecordingExtension("first", events)
    second = RecordingExtension(
        "second", events, close_error=RuntimeError("close failed")
    )
    manager = ExtensionManager([_entry(first), _entry(second)])
    manager.attach(SimpleNamespace())
    events.clear()

    manager.close()
    manager.close()

    assert events == ["close:second", "close:first"]
    assert first.close_calls == 1
    assert second.close_calls == 1
    assert manager.available("first") is False
    assert manager.available("second") is False
