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

from ifc_console import branding
from ifc_console.app import AppCore
from ifc_console.tui.console import ConsoleScreen
from ifc_console.tui.modals import QuitModal

log = logging.getLogger("ifc-console.tui")

# Brand themes built from the grayscale kit; status hues match the viewer and
# keep the ask/edit convention legible on both backgrounds.
THEMES = {
    "dark": Theme(
        name="ifc-dark",
        primary=branding.GRAY_LIGHT,
        secondary=branding.GRAY_MID,
        accent=branding.GRAY_LIGHT,
        foreground=branding.GRAY_LIGHT,
        background=branding.BG_DARK,
        surface="#1c1a19",
        panel="#262322",
        success="#72bd91",
        warning="#d8ad64",
        error="#df7378",
        dark=True,
    ),
    "light": Theme(
        name="ifc-light",
        primary=branding.GRAY_DARK,
        secondary=branding.GRAY_MID,
        accent=branding.GRAY_DARK,
        foreground=branding.GRAY_DARK,
        background=branding.PAPER,
        surface="#e9edf2",
        panel="#dde3ea",
        success="#1c7c4d",
        warning="#9a6b1a",
        error="#b3383f",
        dark=False,
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
        self._unsubscribe = None

    # -- lifecycle ----------------------------------------------------------------
    async def on_mount(self) -> None:
        for theme in THEMES.values():
            self.register_theme(theme)
        self.apply_theme(self.core.ui_theme)
        self.core.start_audit()
        self._unsubscribe = self.core.events.subscribe(self._on_event)
        await self.push_screen(ConsoleScreen())

    def apply_theme(self, name: str, *, persist: bool = False) -> str:
        """Apply dark/light/auto; auto follows the app default (dark)."""
        resolved = "light" if name == "light" else "dark"
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
        await self.ensure_server(self.core.port)

    def _on_event(self, event: dict) -> None:
        # Events fire on the loop thread (tool handlers run there); forward to
        # any mounted console screen.
        for screen in self.screen_stack:
            if isinstance(screen, ConsoleScreen):
                self.call_later(screen.on_core_event, event)

    # -- server co-hosting --------------------------------------------------------
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
        self._server = make_uvicorn_server(app, port)
        self._server_task = asyncio.create_task(self._serve())
        # ~5 s budget. The first ticks are short because a loopback server is
        # usually up in about 10 ms, and create_task means the first check runs
        # before _serve has executed a single line.
        for attempt in range(280):
            await asyncio.sleep(0.002 if attempt < 50 else 0.02)
            if getattr(self._server, "started", False):
                self.core.server_running = True
                self.core.events.emit("server_started", url=self.core.mcp_url, port=port)
                return True
            if self._server_task.done():
                break
        exc = self._server_task.exception() if self._server_task.done() else None
        if exc is not None:
            reason = str(exc)
        else:
            # identify the occupant so the console error says who owns the port
            from ifc_console.portcheck import FREE, conflict_hint, port_status

            kind, detail = await asyncio.to_thread(port_status, port, self.core.token)
            if kind == FREE:
                reason = f"port {port} did not come up"
            else:
                reason = f"port {port} is in use by {detail}; {conflict_hint(kind, port)}"
        self._server = None
        self._server_task = None
        self.core.events.emit("server_failed", reason=reason, port=port)
        return False

    async def _serve(self) -> None:
        try:
            await self._server.serve()
        except SystemExit as exc:
            # uvicorn aborts a failed startup (e.g. bind error) this way; it
            # must not propagate, or it kills the whole event loop.
            log.error("server startup aborted (exit code %s)", exc.code)
        except Exception:
            log.exception("uvicorn server crashed")

    async def stop_server(self) -> None:
        if self._server is None:
            return
        self._server.should_exit = True
        if self._server_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server_task, timeout=5)
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
        self.core.shutdown()


def run_tui(core: AppCore, initial_file: Path | None = None) -> int:
    app = IfcConsoleApp(core, initial_file=initial_file)
    result = app.run()
    return int(result) if isinstance(result, int) else 0
