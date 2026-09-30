"""/connect knows which clients exist and whether each one already points here."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.cli import build_config_snippet
from ifc_console.tui import client_scan, commands
from tests.tui.test_commands import FakeConsole


def _states(home: Path, port: int = 8383) -> dict[str, str]:
    found = client_scan.scan(port, home=home, system="Linux")
    return {status.client: status.state for status in found}


def test_a_machine_with_no_clients_reports_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client_scan.shutil, "which", lambda _name: None)

    assert set(_states(tmp_path).values()) == {"not found"}


def test_claude_code_on_the_path_counts_as_installed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(client_scan.shutil, "which", lambda _name: "/usr/bin/claude")

    assert _states(tmp_path)["claude-code"] == "installed"


def test_an_installed_client_without_an_entry_is_installed(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()

    assert _states(tmp_path)["cursor"] == "installed"


def test_a_config_that_names_ifc_console_is_configured(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        '{"mcpServers": {"ifc-console": {"command": "ifc-console", "args": ["bridge"]}}}',
        encoding="utf-8",
    )

    assert _states(tmp_path)["cursor"] == "configured"


def test_an_entry_for_another_port_is_stale(tmp_path: Path) -> None:
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text(
        '[mcp_servers.ifc-console]\nurl = "http://127.0.0.1:9000/mcp"\n', encoding="utf-8"
    )

    assert _states(tmp_path, port=8383)["codex"] == "stale"
    assert _states(tmp_path, port=9000)["codex"] == "configured"


def test_a_bridge_entry_with_a_port_flag_is_checked_against_it(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_text(
        '{"mcpServers": {"ifc-console": {"command": "x", "args": ["bridge", "--port", "9100"]}}}',
        encoding="utf-8",
    )

    assert _states(tmp_path, port=9100)["cursor"] == "configured"
    assert _states(tmp_path, port=8383)["cursor"] == "stale"


def test_an_unreadable_or_huge_config_never_breaks_the_scan(tmp_path: Path) -> None:
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor" / "mcp.json").write_bytes(b"\xff\xfe" * 100)

    assert _states(tmp_path)["cursor"] == "installed"


@pytest.mark.parametrize("system", ["Windows", "Darwin", "Linux"])
def test_every_platform_gets_a_path_for_every_client(tmp_path: Path, system: str) -> None:
    paths = client_scan.config_paths(tmp_path, system)

    assert set(paths) == set(client_scan.CLIENTS)


def test_the_bridge_setup_carries_the_profile_only_when_asked() -> None:
    plain = build_config_snippet("cursor", "bridge", port=8383, file=None, mode="ask", token=None)
    lean = build_config_snippet(
        "cursor", "bridge", port=8383, file=None, mode="ask", token=None, tools="lean"
    )

    assert "--tools" not in plain
    assert '"--tools"' in lean and '"lean"' in lean


def test_the_http_setup_puts_the_profile_in_the_path() -> None:
    lean = build_config_snippet(
        "claude-code", "http", port=8383, file=None, mode="ask", token="t", tools="lean"
    )

    assert "http://127.0.0.1:8383/mcp/lean" in lean


@pytest.fixture
def console(core) -> FakeConsole:
    core.start_audit()
    return FakeConsole(core)


async def test_connect_asks_claude_code_for_everything_and_others_for_lean(console) -> None:
    await commands.dispatch(console, "/connect claude-code")
    claude = console.text
    console.lines.clear()
    await commands.dispatch(console, "/connect cursor")
    cursor = console.text

    assert "--tools" not in claude
    assert "--tools" in cursor and "lean" in cursor
    assert "/tools profile full" in cursor


async def test_connect_without_an_argument_lists_what_was_found(console, monkeypatch) -> None:
    monkeypatch.setattr(
        client_scan,
        "scan",
        lambda port: [client_scan.ClientStatus("cursor", "configured", None)],
    )

    await commands.dispatch(console, "/connect")

    assert "Cursor" in console.text and "configured" in console.text


async def test_a_default_of_lean_leaves_the_flag_out(console) -> None:
    console.core.settings.mcp.tool_profile = "lean"

    await commands.dispatch(console, "/connect cursor")

    assert "--tools" not in console.text
