"""AppCore: owns the session singletons and wires them together.

Nothing reaches the model except through this object. The MCP layer, TUI,
CLI, and viewer are all thin faces over it.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ifc_console import __version__
from ifc_console.audit import AuditLog
from ifc_console.events import EventBus
from ifc_console.mcp.envelope import ToolError
from ifc_console.policy.modes import Mode, PolicyEngine
from ifc_console.recents import RecentsStore
from ifc_console.session.backups import BackupStore
from ifc_console.session.model import ModelSession
from ifc_console.settings import SettingsStore
from ifc_console.viewer.hub import ViewerHub


@dataclass
class ViewerState:
    enabled: bool = False
    connected: int = 0
    url: str | None = None
    selection: list[str] = field(default_factory=list)


class AppCore:
    def __init__(
        self,
        store: SettingsStore,
        *,
        mode: Mode | None = None,
        port: int | None = None,
        extra_allowed_dirs: tuple[Path, ...] = (),
        transport: str = "http",
        viewer: bool | None = None,
    ) -> None:
        store.ensure_dirs()
        self.store = store
        s = store.settings
        self.events = EventBus()
        self.audit = AuditLog(store.sessions_dir, s.sessions.retention)
        self.recents = RecentsStore(store.recents_file, s.recents.max)
        self.backups = BackupStore(store.backups_dir, s.files.backup_retention)
        self.policy = PolicyEngine(
            mode or Mode(s.mode.default),
            allow_system_access=s.exec.allow_system_access,
            events=self.events,
            audit=self.audit,
        )
        self.session = ModelSession()
        # Persistent by default: clients get configured once and reconnect
        # across restarts. server.persistent_token=false restores per-run
        # tokens for the stricter threat model.
        self.token = (
            store.load_server_token() if s.server.persistent_token else secrets.token_hex(16)
        )
        self.port = port or s.server.port
        self.transport = transport
        self.viewer = ViewerState()
        self.viewer_hub = ViewerHub(self)
        self.server_running = False
        self._mcp = None  # set by attach_mcp once the tool server exists
        self._viewer_tools_registered = False
        self._read_cache: dict[tuple, Any] = {}
        self.ui_theme = s.tui.theme

        # viewer=True/False comes from the --viewer flag; None defers to
        # settings.
        want_viewer = s.viewer.enabled_default if viewer is None else viewer
        if want_viewer:
            self.enable_viewer()

        self.allowed_dirs: list[Path] = []
        for d in s.files.allowed_dirs:
            self.add_allowed_dir(Path(d))
        for d in extra_allowed_dirs:
            self.add_allowed_dir(d)
        self.add_allowed_dir(Path.cwd())

    # -- basics ---------------------------------------------------------------
    @property
    def settings(self):
        return self.store.settings

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def viewer_url(self) -> str:
        """Tokenized URL a browser needs to open the viewer.

        The token rides the fragment: it never reaches the server, its logs,
        or a referrer, and the SPA scrubs it from the address bar on load.
        """
        return f"http://127.0.0.1:{self.port}/viewer#t={self.token}"

    # -- viewer lifecycle -------------------------------------------------------
    def attach_mcp(self, mcp) -> None:
        """Called by build_mcp: core tools are registered unconditionally,
        the viewer tool category only while the viewer is enabled."""
        self._mcp = mcp
        # A fresh FastMCP never carries the category yet, whatever the old
        # instance had (matters when /port rebuilds the server).
        self._viewer_tools_registered = False
        self._sync_viewer_tools()

    def _sync_viewer_tools(self) -> None:
        """Keep the registered tool surface in step with viewer.enabled.

        FastMCP evaluates tools/list per request, so added or removed tools
        are visible to clients on their next listing without a restart.
        """
        if self._mcp is None:
            return
        from ifc_console.mcp import tools_viewer

        if self.viewer.enabled and not self._viewer_tools_registered:
            tools_viewer.register(self._mcp, self)
            self._viewer_tools_registered = True
        elif not self.viewer.enabled and self._viewer_tools_registered:
            for name in tools_viewer.TOOL_NAMES:
                self._mcp.remove_tool(name)
            self._viewer_tools_registered = False

    def enable_viewer(self) -> None:
        """Turn the viewer surface on (idempotent): routes answer, viewer
        tools join the MCP tool list."""
        if self.viewer.enabled:
            return
        self.viewer.enabled = True
        self.viewer.url = self.viewer_url
        self._sync_viewer_tools()
        self.audit.record("viewer_enabled", url=self.viewer.url)
        self.events.emit("viewer_enabled", url=self.viewer.url)

    def disable_viewer(self) -> None:
        """Turn the viewer surface off: routes 404 again and the viewer tools
        leave the MCP tool list. Open tabs are closed by the caller (async)."""
        if not self.viewer.enabled:
            return
        self.viewer.enabled = False
        self.viewer.url = None
        self._sync_viewer_tools()
        self.audit.record("viewer_disabled")
        self.events.emit("viewer_disabled")

    def session_meta(self) -> dict:
        return self.session.meta(self.policy.mode.value)

    # -- fingerprint-keyed read cache ----------------------------------------------
    async def cached_read(
        self,
        name: str,
        build: Callable[[], Any],
        *,
        key: tuple = (),
        timeout: float | None = 120,
    ) -> tuple[Any, bool]:
        """Run a read builder on the model worker, cached per fingerprint+revision.

        Returns (value, was_cached). The revision bumps on load, save, and
        every mutation, so a hit can never be stale; entries from dead
        revisions are pruned lazily to bound memory.
        """
        s = self.session
        cache_key = (name, s.fingerprint, s.revision, *key)
        if cache_key in self._read_cache:
            return self._read_cache[cache_key], True
        value = await s.run(build, timeout=timeout)
        live = (s.fingerprint, s.revision)
        if len(self._read_cache) >= 64:
            for stale in [k for k in self._read_cache if k[1:3] != live]:
                del self._read_cache[stale]
        self._read_cache[cache_key] = value
        return value, False

    # -- allowed directories ------------------------------------------------------
    def add_allowed_dir(self, directory: Path) -> None:
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if resolved.is_file():
            resolved = resolved.parent
        if resolved not in self.allowed_dirs:
            self.allowed_dirs.append(resolved)

    def path_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self.allowed_dirs:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def require_path_allowed(self, path: Path) -> Path:
        resolved = path.resolve()
        if not self.path_allowed(resolved):
            allowed = ", ".join(str(d) for d in self.allowed_dirs) or "(none)"
            raise ToolError(
                "PATH_NOT_ALLOWED",
                f"{resolved} is outside the allowed directories.",
                f"Allowed roots: {allowed}. Ask the user to launch with --allow-dir "
                "or to pick the file in the ifc-console terminal.",
            )
        return resolved

    # -- lifecycle ----------------------------------------------------------------
    def start_audit(self) -> str:
        return self.audit.start(
            {
                "version": __version__,
                "mode": self.policy.mode.value,
                "transport": self.transport,
                "port": self.port,
                "settings": self.store.flat(),
            }
        )

    async def open_model(self, path: Path) -> None:
        await self.session.open(path, max_mb=self.settings.files.max_open_mb)
        assert self.session.path is not None
        self.add_allowed_dir(self.session.path.parent)
        self.recents.touch(
            self.session.path,
            size_bytes=self.session.size_bytes,
            schema=self.session.schema or "?",
            mode=self.policy.mode.value,
        )
        self.audit.record(
            "model_open",
            path=str(self.session.path),
            schema=self.session.schema,
            size_bytes=self.session.size_bytes,
            fingerprint=self.session.fingerprint,
        )
        self.events.emit(
            "model_loaded",
            path=str(self.session.path),
            name=self.session.name,
            schema=self.session.schema,
            fingerprint=self.session.fingerprint,
        )

    def set_mode(self, new_mode: Mode, *, by: str) -> None:
        if new_mode is self.policy.mode:
            return
        self.policy.set_mode(new_mode, by=by)

    def set_ui_theme(self, name: str, *, persist: bool = False) -> str:
        """Record the theme choice and tell every surface (auto resolves dark).

        Returns the resolved value the viewer should render ("dark"/"light").
        """
        self.ui_theme = name
        if persist:
            self.store.set_user("tui.theme", name)
        resolved = "light" if name == "light" else "dark"
        self.events.emit("theme_changed", theme=resolved)
        return resolved

    def shutdown(self) -> None:
        self.audit.end()
        self.session.close()

    # -- logging helper used by the tool wrapper ------------------------------------
    def tool_event(self, tool: str, *, ok: bool, duration_ms: int, detail: str = "") -> None:
        self.audit.record("tool_call", tool=tool, ok=ok, duration_ms=duration_ms, detail=detail)
        self.events.emit("tool_called", tool=tool, ok=ok, duration_ms=duration_ms, detail=detail)
