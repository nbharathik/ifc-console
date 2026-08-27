"""Textual App shell: console screen and uvicorn co-hosting.

The app owns the server lifecycle; the console owns all interaction.
Startup never blocks on a model: the MCP server comes up immediately and
the user picks a file with /file whenever they like.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

from textual.app import App
from textual.binding import Binding
from textual.theme import Theme

from ifc_console.app import AppCore
from ifc_console.themes import resolve_theme
from ifc_console.tui.console import ConsoleScreen
from ifc_console.tui.modals import QuitModal

log = logging.getLogger("ifc-console.tui")

_SERVER_START_TIMEOUT_S = 5.0
_SERVER_POLL_S = 0.02

# Four restrained palettes. Each uses neutral structure plus one accent family;
# status remains explicit in its label/icon instead of changing hue.
THEMES = {
    "light": Theme(
        name="ifc-light",
        primary="#2f6fa3",
        secondary="#596875",
        accent="#1d5c90",
        foreground="#1f2831",
        background="#f5f8fb",
        surface="#eaeff5",
        panel="#dfe6ee",
        success="#1d5c90",
        warning="#1d5c90",
        error="#1d5c90",
        dark=False,
    ),
    "dark": Theme(
        name="ifc-dark",
        primary="#d9dce2",
        secondary="#8f959f",
        accent="#d9dce2",
        foreground="#f0f1f3",
        background="#0d0e10",
        surface="#121316",
        panel="#181a1e",
        success="#d9dce2",
        warning="#d9dce2",
        error="#d9dce2",
        dark=True,
    ),
    "modern": Theme(
        name="ifc-modern",
        primary="#b0b4bb",
        secondary="#8d8d8d",
        accent="#b0b4bb",
        foreground="#f5f5f5",
        background="#080808",
        surface="#0e0e0e",
        panel="#151515",
        success="#b0b4bb",
        warning="#b0b4bb",
        error="#b0b4bb",
        dark=True,
    ),
    "blue": Theme(
        name="ifc-blue",
        primary="#9ac7eb",
        secondary="#83909c",
        accent="#9ac7eb",
        foreground="#e4e9ee",
        background="#101720",
        surface="#171e27",
        panel="#1c2530",
        success="#9ac7eb",
        warning="#9ac7eb",
        error="#9ac7eb",
        dark=True,
    ),
}


class IfcConsoleApp(App):
    TITLE = "ifc-console"
    BINDINGS = [
        # Ctrl+C copies a mouse selection when one exists; otherwise it is
        # the standard terminal exit. Priority keeps both paths consistent
        # regardless of which widget currently has focus.
        Binding("ctrl+c", "request_quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        core: AppCore,
        initial_file: Path | None = None,
        autostart: bool = True,
    ) -> None:
        super().__init__()
        self.core = core
        self.initial_file = initial_file
        self.autostart = autostart  # tests disable this to keep ports untouched
        self._server = None
        self._server_task: asyncio.Task | None = None
        self._last_conflict_kind: str | None = None
        self._unsubscribe = None
        # Direct embedders and tests construct the TUI without going through
        # the CLI's warm-up path. Start the same import warm-up as soon as the
        # fully constructed core reaches the app, before Textual mounts.
        from ifc_console import preload

        preload.start()
        preload.release()

    # -- lifecycle ----------------------------------------------------------------
    async def on_mount(self) -> None:
        for theme in THEMES.values():
            self.register_theme(theme)
        self.apply_theme(self.core.ui_theme)
        self.core.start_audit()
        self.core.start_knowledge()
        self._unsubscribe = self.core.events.subscribe(self._on_event)
        await self.push_screen(ConsoleScreen())

    def apply_theme(self, name: str, *, persist: bool = False) -> str:
        """Apply a named palette; legacy auto follows Default Blue."""
        resolved = resolve_theme(name)
        self.theme = THEMES[resolved].name
        self.core.set_ui_theme(name, persist=persist)
        return resolved

    def begin_startup(self) -> None:
        """Kicked off by the console once it is mounted and can show output."""
        if self.autostart:
            self.run_worker(self._startup(), exclusive=False)

    async def _startup(self) -> None:
        if self.initial_file is not None:
            try:
                await self.core.open_model(self.initial_file)
            except Exception as exc:
                self.core.events.emit(
                    "server_failed",  # reuse the console's error styling
                    reason=f"could not load {self.initial_file}: {exc}",
                )
            self.initial_file = None
        await self.ensure_server_with_fallback()

    def _on_event(self, event: dict) -> None:
        # Events fire on the loop thread (tool handlers run there); forward to
        # any mounted console screen.
        for screen in self.screen_stack:
            if isinstance(screen, ConsoleScreen):
                self.call_later(screen.on_core_event, event)

    # -- server co-hosting --------------------------------------------------------
    async def ensure_server_with_fallback(self) -> bool:
        """Start on the configured port, or move beside a sibling session.

        When the port is held by another ifc-console with this same token,
        that session already serves the pinned MCP clients, so this one can
        safely take the next free port instead of dead-ending.
        """
        if self.core.server_running:
            return True
        original = self.core.port
        if await self.ensure_server(original):
            return True
        from ifc_console.portcheck import IFC_CONSOLE, find_free_port

        if self._last_conflict_kind != IFC_CONSOLE:
            return False
        fallback = await asyncio.to_thread(find_free_port, original)
        if fallback is None or not await self.ensure_server(fallback):
            return False
        self.core.events.emit("server_moved", port=fallback, original=original)
        return True

    async def ensure_server(self, port: int) -> bool:
        if self._server is not None:
            return True
        # heavy imports run off the event loop so the UI never freezes
        from ifc_console import preload

        await asyncio.to_thread(preload.wait)
        from ifc_console.mcp.server import build_http_app, build_mcp, make_uvicorn_server

        self.core.port = port
        if self.core.viewer.enabled:
            self.core.viewer.url = self.core.viewer_url
        mcp = build_mcp(self.core)
        app = build_http_app(self.core, mcp)
        server = make_uvicorn_server(app, port)
        self._server = server
        task = asyncio.create_task(self._serve(server))
        self._server_task = task
        deadline = asyncio.get_running_loop().time() + _SERVER_START_TIMEOUT_S
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(_SERVER_POLL_S)
            if task.done():
                break
            if getattr(server, "started", False):
                self.core.server_running = True
                self.core.server_error = None
                self.core.events.emit("server_started", url=self.core.mcp_url, port=port)
                return True
        task_reason = task.result() if task.done() and not task.cancelled() else None
        self._last_conflict_kind = None
        if task_reason:
            reason = task_reason
        else:
            # identify the occupant so the console error says who owns the port
            from ifc_console.portcheck import FREE, conflict_hint, port_status

            kind, detail = await asyncio.to_thread(port_status, port, self.core.token)
            if kind == FREE:
                reason = f"port {port} did not come up"
            else:
                self._last_conflict_kind = kind
                reason = f"port {port} is in use by {detail}; {conflict_hint(kind, port)}"
        server.should_exit = True
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._server = None
        self._server_task = None
        self.core.server_running = False
        self.core.server_error = reason
        self.core.events.emit("server_failed", reason=reason, port=port)
        return False

    async def _serve(self, server) -> str | None:
        reason = None
        try:
            await server.serve()
        except asyncio.CancelledError:
            raise
        except SystemExit as exc:
            # uvicorn aborts a failed startup (e.g. bind error) this way; it
            # must not propagate, or it kills the whole event loop.
            log.error("server startup aborted (exit code %s)", exc.code)
        except Exception as exc:
            log.exception("uvicorn server crashed")
            reason = str(exc) or type(exc).__name__
        finally:
            if (
                self._server is server
                and self.core.server_running
                and not server.should_exit
            ):
                reason = reason or f"server on port {self.core.port} stopped unexpectedly"
                self.core.server_running = False
                self.core.server_error = reason
                self.core.events.emit("server_failed", reason=reason, port=self.core.port)
        return reason

    async def stop_server(self) -> None:
        if self._server is None:
            return
        server = self._server
        task = self._server_task
        server.should_exit = True
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=5)
            except TimeoutError:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._server = None
        self._server_task = None
        self.core.server_running = False

    async def restart_server(self, port: int) -> bool:
        """Move the MCP endpoint to another port (console /port)."""
        await self.stop_server()
        return await self.ensure_server(port)

    # -- quit ---------------------------------------------------------------------
    async def action_quit(self) -> None:
        """Textual's built-in quit binding (ctrl+q) must also go through the
        unsaved-changes check and server teardown, not App.exit directly."""
        self.action_request_quit()

    def action_request_quit(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.screen.clear_selection()
            self.notify("selection copied to clipboard")
            return
        # an impatient second ctrl+c must not stack another quit dialog
        if any(isinstance(screen, QuitModal) for screen in self.screen_stack):
            return

        async def go() -> None:
            if self.core.session.dirty:
                choice = await self.push_screen_wait(QuitModal())
                if choice == "cancel":
                    return
                if choice == "save" and self.core.session.path is not None:
                    try:
                        await self.core.session.save(self.core.session.path, self.core.backups)
                    except Exception as exc:
                        self.notify(f"save failed, not quitting: {exc}", severity="error")
                        return
            await self._teardown()
            self.exit(0)

        self.run_worker(go(), exclusive=False)

    async def _teardown(self) -> None:
        await self.stop_server()
        if self._unsubscribe:
            self._unsubscribe()
        await self.core.ashutdown()


def run_tui(core: AppCore, initial_file: Path | None = None) -> int:
    app = IfcConsoleApp(core, initial_file=initial_file)
    result = app.run()
    return int(result) if isinstance(result, int) else 0
