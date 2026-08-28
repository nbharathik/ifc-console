"""Persistent server token: configure clients once, reconnect forever."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ifc_console.app import AppCore
from ifc_console.cli import MCP_REMOTE_SPEC, build_config_snippet, main
from ifc_console.settings import SettingsStore

CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "codex")


def _store(home: Path) -> SettingsStore:
    return SettingsStore(home=home, project_dir=home, env={})


def test_token_persists_across_runs(tmp_path: Path) -> None:
    first = _store(tmp_path / "home").load_server_token()
    second = _store(tmp_path / "home").load_server_token()
    assert first == second
    assert len(first) == 32


def test_concurrent_first_use_returns_one_token(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _i: _store(home).load_server_token(), range(16)))
    assert len(set(tokens)) == 1
    assert _store(home).load_server_token() == tokens[0]


def test_legacy_token_lock_file_does_not_block_first_use(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "token.lock").write_text("stale legacy lock", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda _i: _store(home).load_server_token(), range(16)))

    assert len(set(tokens)) == 1
    assert _store(home).load_server_token() == tokens[0]


def test_cores_share_the_machine_token(tmp_path: Path) -> None:
    """Two ifc-console launches (same home) present the same bearer token."""
    core_a = AppCore(_store(tmp_path / "home"))
    core_b = AppCore(_store(tmp_path / "home"))
    try:
        assert core_a.token == core_b.token
    finally:
        core_a.shutdown()
        core_b.shutdown()


def test_rotate_invalidates_the_old_token(tmp_path: Path) -> None:
    store = _store(tmp_path / "home")
    old = store.load_server_token()
    new = store.rotate_server_token()
    assert new != old
    assert store.load_server_token() == new


def test_per_run_tokens_when_persistence_disabled(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _store(home)
    store.ensure_dirs()
    store.set_user("server.persistent_token", "false")
    core_a = AppCore(_store(home))
    core_b = AppCore(_store(home))
    try:
        assert core_a.token != core_b.token
        assert not store.token_file.exists()
    finally:
        core_a.shutdown()
        core_b.shutdown()


def test_corrupt_token_file_regenerates(tmp_path: Path) -> None:
    store = _store(tmp_path / "home")
    store.home.mkdir(parents=True)
    store.token_file.write_text("not a token!!\n", encoding="utf-8")
    token = store.load_server_token()
    assert len(token) == 32
    assert store.load_server_token() == token  # and it stuck


def test_mcp_config_hides_persistent_token_by_default(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    expected = _store(tmp_path / "home").load_server_token()
    assert main(["mcp-config", "--client", "claude-code", "--transport", "http"]) == 0
    captured = capsys.readouterr()
    assert expected not in captured.out
    assert "<TOKEN>" in captured.out
    assert "token_in_config_snippets" in captured.err


def test_default_mcp_config_keeps_the_token_out_of_client_files(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The bridge reads this machine's token itself, so no config holds it."""
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    token = _store(tmp_path / "home").load_server_token()
    assert main(["mcp-config", "--client", "claude-desktop"]) == 0
    out = capsys.readouterr().out
    assert token not in out
    assert "<TOKEN>" not in out
    assert "bridge" in out


def test_mcp_config_respects_explicit_token_setting(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    store = _store(home)
    expected = store.load_server_token()
    store.set_user("server.token_in_config_snippets", "true")
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(home))
    assert main(["mcp-config", "--client", "codex", "--transport", "http"]) == 0
    captured = capsys.readouterr()
    assert expected in captured.out
    assert "<TOKEN>" not in captured.out
    assert "token_in_config_snippets" not in captured.err


def test_per_run_token_defaults_mcp_config_to_stdio(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    store = _store(home)
    store.ensure_dirs()
    store.set_user("server.persistent_token", "false")
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(home))

    assert main(["mcp-config", "--client", "codex"]) == 0
    captured = capsys.readouterr()
    assert "serve" in captured.out and "--stdio" in captured.out
    assert "bridge" not in captured.out


def test_per_run_token_rejects_unkeyed_bridge_config(tmp_path: Path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    store = _store(home)
    store.ensure_dirs()
    store.set_user("server.persistent_token", "false")
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(home))

    assert main(["mcp-config", "--transport", "bridge"]) == 2
    assert "persistent_token=true" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["--port", "0", "--no-tui"],
        ["serve", "--http", "--port", "-1"],
        ["bridge", "--port", "70000"],
        ["mcp-config", "--port", "0"],
        ["mcp-config", "--port", "not-a-port"],
    ],
)
def test_every_cli_port_rejects_invalid_values(argv: list[str], capsys) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv)

    assert raised.value.code == 2
    assert "port" in capsys.readouterr().err


def test_token_cli_show_and_rotate(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    assert main(["token", "show"]) == 0
    first = capsys.readouterr().out.strip()
    assert main(["token", "rotate"]) == 0
    rotated = capsys.readouterr().out.strip()
    assert rotated != first
    assert main(["token", "show"]) == 0
    assert capsys.readouterr().out.strip() == rotated
    assert main(["token", "path"]) == 0
    assert capsys.readouterr().out.strip().endswith("token")


def test_snippet_placeholder_without_token() -> None:
    snippet = build_config_snippet(
        "claude-code", "http", port=8383, file=None, mode="ask", token=None
    )
    assert "<TOKEN>" in snippet


@pytest.mark.parametrize("client", CLIENTS)
def test_default_snippets_launch_the_bridge(client: str) -> None:
    """The default wiring survives the client starting before the console."""
    model = r"C:\models\active.ifc"
    snippet = build_config_snippet(client, None, port=8383, file=model, mode="edit", token="abc123")
    assert "bridge" in snippet
    assert "abc123" not in snippet  # no secret written into a client config
    assert "active.ifc" not in snippet
    assert "--file" not in snippet
    assert "--mode" not in snippet


@pytest.mark.parametrize("client", CLIENTS)
def test_http_snippets_share_the_session_and_ignore_file(client: str) -> None:
    model = r"C:\models\active.ifc"
    snippet = build_config_snippet(
        client, "http", port=8383, file=model, mode="edit", token="abc123"
    )
    assert "http://127.0.0.1:8383/mcp" in snippet
    assert "abc123" in snippet
    assert "active.ifc" not in snippet
    assert "--file" not in snippet
    assert "--mode" not in snippet


@pytest.mark.parametrize("client", CLIENTS)
def test_explicit_stdio_snippets_may_pin_file(client: str) -> None:
    model = "C:/My Models/pinned.ifc"
    snippet = build_config_snippet(
        client, "stdio", port=8383, file=model, mode="ask", token="abc123"
    )
    assert model in snippet
    assert "http://127.0.0.1:8383/mcp" not in snippet
    assert "abc123" not in snippet


def test_claude_code_http_config_is_user_scoped() -> None:
    snippet = build_config_snippet(
        "claude-code", "http", port=8383, file=None, mode="ask", token="abc123"
    )
    assert "--transport http" in snippet
    assert "--scope user" in snippet


def test_claude_desktop_http_config_uses_authenticated_bridge() -> None:
    snippet = build_config_snippet(
        "claude-desktop", "http", port=8383, file=None, mode="ask", token="abc123"
    )
    server = json.loads(snippet)["mcpServers"]["ifc-console"]
    assert server["command"] == "npx"
    # pinned: an uncached "latest" lookup on launch can exceed Claude
    # Desktop's 60 second startup timeout
    assert "@" in MCP_REMOTE_SPEC
    assert server["args"][:3] == ["-y", MCP_REMOTE_SPEC, "http://127.0.0.1:8383/mcp"]
    assert "--allow-http" in server["args"]
    assert "http-only" in server["args"]
    assert "Authorization:${IFC_CONSOLE_AUTH_HEADER}" in server["args"]
    assert server["env"] == {"IFC_CONSOLE_AUTH_HEADER": "Bearer abc123"}


def test_codex_http_config_has_static_authorization_header() -> None:
    snippet = build_config_snippet(
        "codex", "http", port=8383, file=None, mode="ask", token="abc123"
    )
    assert "[mcp_servers.ifc-console]" in snippet
    assert 'url = "http://127.0.0.1:8383/mcp"' in snippet
    assert 'http_headers = { Authorization = "Bearer abc123" }' in snippet
    assert "bearer_token_env_var" not in snippet
    assert "command =" not in snippet
