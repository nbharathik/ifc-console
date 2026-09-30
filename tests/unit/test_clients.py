"""The console knows which clients are connected, and can change what they list."""

from __future__ import annotations

import time

import pytest
from starlette.testclient import TestClient

from ifc_console.mcp import clients
from ifc_console.tui import commands
from tests.tui.test_commands import FakeConsole


def _scope(**headers: str) -> dict:
    return {"headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}


def test_a_bridge_names_itself_with_the_client_it_speaks_for() -> None:
    assert clients.label_for(_scope(**{"X-IFC-Client": "claude-code/2.1.4"})) == (
        "claude-code/2.1.4",
        "bridge",
    )


def test_other_clients_are_known_by_the_product_in_their_user_agent() -> None:
    assert clients.label_for(_scope(**{"User-Agent": "Cursor/0.50 (win32)"})) == (
        "Cursor/0.50",
        "http",
    )


def test_a_client_that_says_nothing_is_unknown() -> None:
    assert clients.label_for(_scope()) == ("unknown", "http")


def test_labels_cannot_carry_markup_or_control_characters() -> None:
    label, _ = clients.label_for(_scope(**{"X-IFC-Client": "[red]x[/red]\r\nevil"}))

    assert "[" not in label and "\n" not in label and "\r" not in label
    assert len(label) <= 48


def test_the_registry_counts_calls_and_lists_the_newest_first() -> None:
    registry = clients.ClientRegistry()
    registry.seen("a", "http")
    time.sleep(0.01)
    registry.seen("b", "bridge", "lean")
    registry.called("a", "orient", True)
    registry.called("a", "get_element", False)
    registry.called("nobody", "orient", True)

    rows = registry.active()

    assert [r.label for r in rows][0] == "a"  # a was called last
    a = registry.active()[0]
    assert (a.calls, a.errors, a.last_tool) == (2, 1, "get_element")
    assert {r.label: r.profile for r in rows} == {"a": None, "b": "lean"}


def test_a_quiet_client_drops_off_the_list() -> None:
    registry = clients.ClientRegistry()
    registry.seen("old", "http").last_seen = time.time() - 3600
    registry.seen("new", "http")

    assert [r.label for r in registry.active()] == ["new"]
    assert registry.count() == 1


async def test_an_authorized_mcp_request_registers_its_client(core, work_model) -> None:
    from ifc_console.mcp.server import build_http_app, build_mcp

    await core.open_model(work_model)
    app = build_http_app(core, build_mcp(core))
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": "test-client/1.0",
    }

    with TestClient(app, base_url="http://127.0.0.1:8383") as client:
        client.post("/mcp", json=body, headers=headers)
        assert core.clients.count() == 0  # no token, no registration

        response = client.post(
            "/mcp", json=body, headers={**headers, "Authorization": f"Bearer {core.token}"}
        )

    assert [r.label for r in core.clients.active()] == ["test-client/1.0"]
    assert response.headers["x-ifc-console-catalog"] == str(core.catalog_epoch)


async def test_a_tool_call_is_attributed_to_the_client(core, work_model) -> None:
    from ifc_console.mcp.server import build_http_app, build_mcp

    await core.open_model(work_model)
    seen: list[dict] = []
    core.events.subscribe(seen.append)
    app = build_http_app(core, build_mcp(core))
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "orient", "arguments": {}},
    }

    with TestClient(app, base_url="http://127.0.0.1:8383") as client:
        client.post(
            "/mcp",
            json=body,
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {core.token}",
                "User-Agent": "test-client/1.0",
            },
        )

    calls = [e for e in seen if e["type"] == "tool_called" and e["tool"] == "orient"]
    assert calls and calls[-1]["client"] == "test-client/1.0"
    record = core.clients.active()[0]
    assert (record.calls, record.last_tool) == (1, "orient")


@pytest.fixture
def console(core) -> FakeConsole:
    core.start_audit()
    return FakeConsole(core)


async def test_clients_lists_who_connected(console) -> None:
    await commands.dispatch(console, "/clients")
    assert "no client has connected" in console.text

    console.core.clients.seen("claude-code/2.1", "bridge", "lean")
    console.core.clients.called("claude-code/2.1", "get_element", True)
    console.lines.clear()
    await commands.dispatch(console, "/clients")

    assert "claude-code/2.1" in console.text
    assert "lean" in console.text and "get_element" in console.text


async def test_tools_profile_shows_and_changes_the_default(console) -> None:
    core = console.core
    epoch = core.catalog_epoch

    await commands.dispatch(console, "/tools profile")
    assert "full" in console.text

    await commands.dispatch(console, "/tools profile lean")

    assert core.settings.mcp.tool_profile == "lean"
    assert core.catalog_epoch == epoch + 1
    assert "profile is now" in console.text and "lean" in console.text


async def test_tools_profile_refuses_an_unknown_name(console) -> None:
    await commands.dispatch(console, "/tools profile huge")

    assert "usage" in console.text
    assert console.core.settings.mcp.tool_profile == "full"


async def test_setting_the_same_profile_again_changes_nothing(console) -> None:
    epoch = console.core.catalog_epoch

    await commands.dispatch(console, "/tools profile full")

    assert console.core.catalog_epoch == epoch
    assert "already full" in console.text
