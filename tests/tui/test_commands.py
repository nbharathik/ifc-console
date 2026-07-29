"""Slash-command dispatch against a fake console (no Textual needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.cli import build_config_snippet
from ifc_console.policy.modes import Mode
from ifc_console.tui import commands

pytestmark = pytest.mark.asyncio

CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "codex")


class FakeConsole:
    """Just enough surface for command handlers: core + captured output."""

    def __init__(self, core) -> None:
        self.core = core
        self.lines: list[str] = []
        self.confirm_answer = True
        self.picker_opened = 0
        self.clipboard = ""
        self.app = self

    # -- console protocol -------------------------------------------------
    def print(self, markup: str) -> None:
        self.lines.append(markup)

    def clear_log(self) -> None:
        self.lines.clear()

    def refresh_status(self) -> None:
        pass

    async def confirm(self, _title: str, _text: str = "") -> bool:
        return self.confirm_answer

    async def open_file_picker(self, initial_filter: str = "") -> None:
        self.picker_opened += 1

    def copy_to_clipboard(self, value: str) -> None:
        self.clipboard = value

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def console(core) -> FakeConsole:
    core.start_audit()
    return FakeConsole(core)


async def test_unknown_command_suggests(console: FakeConsole) -> None:
    await commands.dispatch(console, "/nope")
    assert "unknown command /nope" in console.text


async def test_unique_prefix_resolves(console: FakeConsole) -> None:
    await commands.dispatch(console, "/hel")
    assert "commands" in console.text  # /help ran


async def test_non_slash_text_gets_a_hint(console: FakeConsole) -> None:
    await commands.dispatch(console, "hello there")
    assert "commands start with /" in console.text


async def test_bare_ifc_path_opens_model(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, str(work_model))
    assert console.core.session.loaded
    assert "loaded" in console.text


async def test_quoted_path_opens_model(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, f'"{work_model}"')
    assert console.core.session.loaded


async def test_exit_is_a_quit_alias() -> None:
    assert commands.ALIASES["exit"] == "quit"
    assert "quit" in commands.REGISTRY


async def test_command_crash_is_reported_not_fatal(
    console: FakeConsole, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_console, _args: str) -> None:
        raise RuntimeError("kaboom [with markup-hostile text]")

    monkeypatch.setitem(
        commands.REGISTRY,
        "boom",
        commands.Command(name="boom", usage="/boom", help="x", group="console", handler=boom),
    )
    await commands.dispatch(console, "/boom")
    assert "kaboom" in console.text  # reported to the user, not raised


async def test_help_lists_every_command(console: FakeConsole) -> None:
    await commands.dispatch(console, "/help")
    for name in commands.REGISTRY:
        assert f"/{name}" in console.text


async def test_mode_set_and_query(console: FakeConsole) -> None:
    await commands.dispatch(console, "/mode")
    assert "ask" in console.text
    await commands.dispatch(console, "/mode edit")  # confirm_answer=True approves
    assert console.core.policy.mode is Mode.EDIT
    console.confirm_answer = False
    await commands.dispatch(console, "/mode ask")  # de-escalation, no confirm
    assert console.core.policy.mode is Mode.ASK


async def test_mode_escalation_denied_by_confirm(console: FakeConsole) -> None:
    console.confirm_answer = False
    await commands.dispatch(console, "/mode edit")
    assert console.core.policy.mode is Mode.ASK
    assert "unchanged" in console.text


async def test_connect_includes_reusable_http_configs(
    console: FakeConsole, work_model: Path
) -> None:
    await commands.dispatch(console, f"/open {work_model}")
    console.clear_log()
    await commands.dispatch(console, "/connect")
    assert console.core.token in console.text
    assert "http://127.0.0.1:" in console.text
    assert "shared HTTP console" in console.text
    assert work_model.name not in console.text
    assert console.clipboard.startswith("claude mcp add")
    assert "setup copied to clipboard" in console.text
    console.clear_log()
    console.clipboard = "leave-me-alone"
    await commands.dispatch(console, "/connect all")
    for client in CLIENTS:
        assert client in console.text
    assert console.clipboard == "leave-me-alone"
    assert "/copy codex" in console.text
    assert work_model.name not in console.text
    assert "model paths are intentionally omitted" in console.text
    for target in (
        "claude_desktop_config.json",
        "~/.cursor/mcp.json",
        "MCP: Open User Configuration",
        "~/.codex/config.toml",
    ):
        assert target in console.text
    assert "do not replace unrelated server entries" in console.text


async def test_copy_command_is_user_scoped_and_reusable(console: FakeConsole) -> None:
    await commands.dispatch(console, "/copy cmd")
    legacy = console.clipboard
    assert "--transport http" in console.clipboard
    assert "--scope user" in console.clipboard
    assert console.core.token in console.clipboard
    assert "--file" not in console.clipboard
    await commands.dispatch(console, "/copy claude-code")
    assert console.clipboard == legacy


@pytest.mark.parametrize("client", CLIENTS)
async def test_copy_supports_every_client(
    console: FakeConsole, work_model: Path, client: str
) -> None:
    await commands.dispatch(console, f"/open {work_model}")
    console.clear_log()
    await commands.dispatch(console, f"/copy {client}")
    expected = build_config_snippet(
        client,
        None,
        port=console.core.port,
        file=None,
        mode=console.core.policy.mode.value,
        token=console.core.token,
    )
    assert console.clipboard == expected
    assert console.core.token in console.clipboard
    assert work_model.name not in console.clipboard
    assert f"{client} setup copied to clipboard" in console.text


async def test_connect_respects_hidden_token_setting(console: FakeConsole) -> None:
    console.core.settings.server.token_in_config_snippets = False
    await commands.dispatch(console, "/connect codex")
    assert console.core.token not in console.text
    assert console.core.token not in console.clipboard
    assert "<TOKEN>" in console.clipboard
    assert "&lt;TOKEN&gt;" in console.text or "<TOKEN>" in console.text
    assert "token is hidden" in console.text


async def test_copy_warns_when_setup_contains_placeholder(console: FakeConsole) -> None:
    console.core.settings.server.token_in_config_snippets = False
    await commands.dispatch(console, "/copy codex")
    assert console.core.token not in console.clipboard
    assert "<TOKEN>" in console.clipboard
    assert "copied setup contains <TOKEN>" in console.text


async def test_connect_warns_when_token_is_not_persistent(console: FakeConsole) -> None:
    console.core.settings.server.persistent_token = False
    await commands.dispatch(console, "/connect")
    assert "refresh these client configs after every ifc-console restart" in console.text


async def test_status_reports_model_and_mode(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, f"/open {work_model}")
    console.clear_log()
    await commands.dispatch(console, "/status")
    assert "work.ifc" in console.text
    assert "ask" in console.text


async def test_model_counts(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, f"/open {work_model}")
    console.clear_log()
    await commands.dispatch(console, "/model")
    assert "IfcWall" in console.text


async def test_save_works_in_ask_mode(console: FakeConsole, work_model: Path) -> None:
    """The mode gates the AI, not the user: /save is always available."""
    await commands.dispatch(console, f"/open {work_model}")
    console.clear_log()
    await commands.dispatch(console, "/save")
    assert "saved" in console.text


async def test_save_writes_in_edit_mode(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, f"/open {work_model}")
    await commands.dispatch(console, "/mode edit")
    console.core.session.mark_dirty()
    console.clear_log()
    await commands.dispatch(console, "/save")
    assert "saved" in console.text
    assert console.core.session.dirty is False


async def test_open_missing_path(console: FakeConsole) -> None:
    await commands.dispatch(console, "/open C:/definitely/not/here.ifc")
    assert "does not exist" in console.text


async def test_file_opens_picker(console: FakeConsole) -> None:
    await commands.dispatch(console, "/file")
    assert console.picker_opened == 1


async def test_audit_shows_records(console: FakeConsole, work_model: Path) -> None:
    await commands.dispatch(console, f"/open {work_model}")
    console.clear_log()
    await commands.dispatch(console, "/audit 5")
    assert "model_open" in console.text


async def test_settings_list_and_set(console: FakeConsole) -> None:
    await commands.dispatch(console, "/settings")
    assert "mode.default" in console.text
    console.clear_log()
    await commands.dispatch(console, "/settings viewer.max_model_mb 64")
    assert "viewer.max_model_mb = 64" in console.text
    assert console.core.store.settings.viewer.max_model_mb == 64
    console.clear_log()
    await commands.dispatch(console, "/settings not.a.key 1")
    assert "unknown setting" in console.text


async def test_viewer_url_requires_server(console: FakeConsole) -> None:
    await commands.dispatch(console, "/viewer url")
    assert "server is not running" in console.text
    console.core.server_running = True
    console.clear_log()
    await commands.dispatch(console, "/viewer url")
    assert "/viewer#t=" in console.text
