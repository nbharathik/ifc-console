"""Which MCP clients this machine has, and whether each already points here.

Reads each client's user-level config file and reports one of: not found,
installed (no ifc-console entry), configured, or stale (an entry for another
port). Nothing is written; /connect prints the setup to merge by hand.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "codex")
LABELS = {
    "claude-code": "Claude Code",
    "claude-desktop": "Claude Desktop",
    "cursor": "Cursor",
    "vscode": "VS Code",
    "codex": "Codex",
}
# Claude Code searches tools itself, so it does not need the lean profile.
DEFAULT_PROFILE = {
    "claude-code": "full",
    "claude-desktop": "lean",
    "cursor": "lean",
    "vscode": "lean",
    "codex": "lean",
}
_PORT = re.compile(r"127\.0\.0\.1:(\d+)|--port[\"',\s=]+(\d+)")
MAX_CONFIG_BYTES = 2_000_000


@dataclass(frozen=True)
class ClientStatus:
    client: str
    state: str  # "not found" | "installed" | "configured" | "stale"
    path: Path | None = None

    @property
    def label(self) -> str:
        return LABELS[self.client]


def _app_data(system: str, home: Path) -> Path:
    if system == "Windows":
        return Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    if system == "Darwin":
        return home / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")


def config_paths(home: Path | None = None, system: str | None = None) -> dict[str, Path]:
    home = home or Path.home()
    system = system or platform.system()
    data = _app_data(system, home)
    return {
        "claude-code": home / ".claude.json",
        "claude-desktop": data / "Claude" / "claude_desktop_config.json",
        "cursor": home / ".cursor" / "mcp.json",
        "vscode": data / "Code" / "User" / "mcp.json",
        "codex": home / ".codex" / "config.toml",
    }


def _state(client: str, path: Path, port: int) -> ClientStatus:
    # Claude Code keeps its config in the home directory itself, which always
    # exists, so its presence is the command on the PATH.
    installed = (
        bool(shutil.which("claude")) if client == "claude-code" else path.parent.is_dir()
    )
    if not path.is_file():
        return ClientStatus(client, "installed" if installed else "not found", path)
    try:
        if path.stat().st_size > MAX_CONFIG_BYTES:
            return ClientStatus(client, "installed", path)
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ClientStatus(client, "installed", path)
    if "ifc-console" not in text:
        return ClientStatus(client, "installed", path)
    ports = {int(a or b) for a, b in _PORT.findall(text)}
    current = ports == {port} or (not ports and port == 8383) or port in ports
    return ClientStatus(client, "configured" if current else "stale", path)


def scan(port: int, *, home: Path | None = None, system: str | None = None) -> list[ClientStatus]:
    """One status per known client. Blocking file reads: call from a thread."""
    paths = config_paths(home, system)
    return [_state(client, paths[client], port) for client in CLIENTS]
