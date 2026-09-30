"""Which external agent runs behind a panel provider, and how it reaches us.

An engine is a command that speaks the Agent Client Protocol over stdio
(``opencode acp``, ``codex-acp``, ``goose acp``). The panel lists every
configured engine as a provider named ``harness:<name>``; the console's own
tools reach the engine as an MCP server injected into its ACP session.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

PROVIDER_PREFIX = "harness:"
MCP_SERVER_NAME = "ifc-console"
# The ACP transport passes a trimmed environment, so a provider key reaches an
# engine only when it is forwarded. Each engine gets just the keys it names,
# never the whole set. Stored (keyring) keys exist for these providers only.
KEYRING_PROVIDERS = {
    "OPENAI_API_KEY": "openai",
    "ANTHROPIC_API_KEY": "anthropic",
    "OPENROUTER_API_KEY": "openrouter",
}
# npx adapters are pinned: an unpinned `npx -y` would run whatever version the
# registry serves next. Update these on purpose, after reading the changelog.
CODEX_ACP_SPEC = "@agentclientprotocol/codex-acp@2.0.0"
CLAUDE_ACP_SPEC = "@agentclientprotocol/claude-agent-acp@0.84.0"

# Known engines. `ifc-console agents engines enable <name>` copies one into
# harness.engines; nothing is spawned until it is listed there. `keys` are the
# provider variables the engine gets by default; harness.engines[].forward_keys
# overrides them.
TEMPLATES: dict[str, dict[str, Any]] = {
    "opencode": {
        "label": "OpenCode",
        "command": "opencode",
        "args": ["acp"],
        "keys": [],
        "note": "Open-source agent (MIT). Models and permissions come from opencode.json.",
    },
    "codex": {
        "label": "Codex CLI",
        "command": "npx",
        "args": ["-y", CODEX_ACP_SPEC],
        "keys": ["OPENAI_API_KEY"],
        "note": "OpenAI Codex CLI (Apache-2.0) through the codex-acp adapter. Needs Node.",
    },
    "claude": {
        "label": "Claude Code",
        "command": "npx",
        "args": ["-y", CLAUDE_ACP_SPEC],
        "keys": ["ANTHROPIC_API_KEY"],
        "note": "Claude Code through the claude-agent-acp adapter. Needs Node.",
    },
    "goose": {
        "label": "Goose",
        "command": "goose",
        "args": ["acp"],
        "keys": [],
        "note": "Block's open-source agent (Apache-2.0).",
    },
    "gemini": {
        "label": "Gemini CLI",
        "command": "gemini",
        "args": ["--acp"],
        "keys": ["GEMINI_API_KEY"],
        "note": "Google's open-source CLI (Apache-2.0).",
    },
}


def is_harness_provider(provider_id: str) -> bool:
    return str(provider_id or "").startswith(PROVIDER_PREFIX)


def engine_name(provider_id: str) -> str:
    return str(provider_id or "")[len(PROVIDER_PREFIX) :]


@dataclass(frozen=True)
class EngineSpec:
    """One engine as configured, plus what the templates know about it."""

    name: str
    label: str
    command: str
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str = "run"
    mcp: str = "bridge"
    mode: str = ""
    instructions: str = "prompt"
    instructions_file: str = "AGENTS.md"
    local: bool = False
    idle_timeout_s: int = 600
    note: str = ""
    forward_keys: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, row: Any) -> EngineSpec:
        template = TEMPLATES.get(row.name, {})
        keys = getattr(row, "forward_keys", None)
        if keys is None:
            keys = template.get("keys", ())
        return cls(
            forward_keys=tuple(keys),
            name=row.name,
            label=row.label or template.get("label") or row.name,
            command=row.command,
            args=tuple(row.args),
            env=dict(row.env),
            cwd=row.cwd,
            mcp=row.mcp,
            mode=row.mode,
            instructions=row.instructions,
            instructions_file=row.instructions_file,
            local=row.local,
            idle_timeout_s=row.idle_timeout_s,
            note=str(template.get("note") or ""),
        )

    @property
    def provider_id(self) -> str:
        return PROVIDER_PREFIX + self.name

    def resolve_command(self) -> str | None:
        """The executable to spawn, or None when it is not installed.

        Windows launchers (`opencode.cmd`, `npx.cmd`) are only found through
        which(); CreateProcess does not search PATHEXT on its own.
        """
        found = shutil.which(self.command)
        if found:
            return found
        if os.path.isabs(self.command) and os.path.exists(self.command):
            return self.command
        return None

    def available(self) -> bool:
        return self.resolve_command() is not None

    def signature(self) -> str:
        return "|".join(
            (
                self.name,
                self.command,
                " ".join(self.args),
                self.mode,
                self.cwd,
                self.mcp,
                ",".join(self.forward_keys),
            )
        )


def template_settings(name: str) -> Any:
    """A HarnessEngineSettings row for one known engine."""
    from ifc_console.settings import HarnessEngineSettings

    template = TEMPLATES.get(name)
    if template is None:
        raise KeyError(f"unknown engine template {name!r}")
    return HarnessEngineSettings(
        name=name,
        label=template["label"],
        command=template["command"],
        args=list(template["args"]),
    )


class EngineRegistry:
    """The engines a console may spawn: those listed in harness.engines."""

    def __init__(self, settings: Any) -> None:
        harness = settings.harness
        self.enabled: bool = bool(harness.enabled)
        self.engines: dict[str, EngineSpec] = {
            row.name: EngineSpec.from_settings(row) for row in harness.engines
        }

    @classmethod
    def for_core(cls, core: Any) -> EngineRegistry:
        return cls(core.settings)

    def get(self, name: str) -> EngineSpec | None:
        return self.engines.get(name)

    def by_provider(self, provider_id: str) -> EngineSpec | None:
        if not is_harness_provider(provider_id):
            return None
        return self.engines.get(engine_name(provider_id))

    def provider_rows(self) -> list[dict[str, Any]]:
        """Rows shaped like /api/chat/providers, one per enabled engine."""
        if not self.enabled:
            return []
        rows = []
        for spec in self.engines.values():
            installed = spec.available()
            note = spec.note or "External agent engine over ACP."
            if not installed:
                note = f"{spec.command} was not found on PATH. {note}"
            rows.append(
                {
                    "id": spec.provider_id,
                    "label": f"{spec.label} (engine)",
                    "family": "harness",
                    "base_url": "",
                    "needs_key": False,
                    "key_env": [],
                    "key_from_env": None,
                    "has_key": True,
                    "suggested_model": "default",
                    "note": note,
                    "engine": spec.name,
                    "installed": installed,
                    "local": spec.local,
                }
            )
        return rows


def bridge_command() -> list[str]:
    """Launch this install's bridge, not whichever ifc-console is on PATH."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "bridge"]
    return [sys.executable, "-m", "ifc_console.cli", "bridge"]


def mcp_servers_for(core: Any, engine: EngineSpec, *, http_supported: bool = False) -> list[Any]:
    """The `mcpServers` entry that hands the engine this console's tools.

    The bridge is stdio, which every ACP agent supports. It runs with the
    engine's environment, so IFC_CONSOLE_HOME tells it which token file and
    settings to read; with the default persistent token nothing secret is on
    the command line.
    """
    from acp.schema import EnvVariable, HttpHeader, HttpMcpServer, McpServerStdio

    if engine.mcp == "http" and http_supported:
        return [
            HttpMcpServer(
                type="http",
                name=MCP_SERVER_NAME,
                url=core.mcp_url,
                headers=[HttpHeader(name="Authorization", value=f"Bearer {core.token}")],
            )
        ]
    argv = [*bridge_command(), "--port", str(core.port)]
    if not core.settings.server.persistent_token:
        argv += ["--token", core.token]
    return [
        McpServerStdio(
            name=MCP_SERVER_NAME,
            command=argv[0],
            args=argv[1:],
            env=[EnvVariable(name="IFC_CONSOLE_HOME", value=str(core.store.home))],
        )
    ]


def engine_environment(core: Any, engine: EngineSpec) -> dict[str, str]:
    """Extra environment for the engine process: its own keys, the home, its env."""
    env: dict[str, str] = {"IFC_CONSOLE_HOME": str(core.store.home)}
    for name in engine.forward_keys:
        value = os.environ.get(name)
        if not value and name in KEYRING_PROVIDERS:
            try:
                from ifc_console.agents.credentials import get_api_key

                value = get_api_key(KEYRING_PROVIDERS[name])
            except Exception:  # keyring trouble must not stop an engine
                value = None
        if value:
            env[name] = value
    env.update({str(k): str(v) for k, v in engine.env.items()})
    return env


__all__ = [
    "CLAUDE_ACP_SPEC",
    "CODEX_ACP_SPEC",
    "KEYRING_PROVIDERS",
    "MCP_SERVER_NAME",
    "PROVIDER_PREFIX",
    "TEMPLATES",
    "EngineRegistry",
    "EngineSpec",
    "bridge_command",
    "engine_environment",
    "engine_name",
    "is_harness_provider",
    "mcp_servers_for",
    "template_settings",
]
