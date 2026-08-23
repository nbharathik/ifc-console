"""The console: ifc-console's main screen, styled after coding-agent CLIs.

One surface: a status bar, a scrolling feed (server lifecycle, MCP tool
calls, viewer activity, command output), and a slash-command prompt at the
bottom. A completion menu opens above the prompt as you type: commands and
their argument values are picked right there (Tab, arrows, mouse, Enter)
instead of in extra windows. Only confirmations appear as modal cards.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Footer, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from ifc_console import branding
from ifc_console.tui import commands, completion
from ifc_console.tui.launcher import FilePickerModal, discover_ifc_files
from ifc_console.tui.modals import ConfirmModal
from ifc_console.tui.workspace import WorkspaceModal

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.tui.app import IfcConsoleApp

_MODE_COLORS = {"ask": "green", "edit": "red"}


class CommandInput(Input):
    """Prompt with a completion menu and shell-style history.

    While the menu is open, Up/Down move its highlight, Tab inserts the
    highlighted candidate, Enter picks it (and runs the line once the pick
    completes a command), Escape closes it. With the menu closed the same
    keys do history and plain submit.
    """

    BINDINGS = [
        Binding("up", "history_prev", show=False, priority=True),
        Binding("down", "history_next", show=False, priority=True),
        Binding("tab", "complete", show=False, priority=True),
        Binding("shift+tab", "menu_up", show=False, priority=True),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.history: list[str] = []
        self._cursor: int | None = None  # None = live line

    @property
    def console(self) -> ConsoleScreen:
        return cast("ConsoleScreen", self.screen)

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
        self._cursor = None

    def action_history_prev(self) -> None:
        if self.console.menu_move(-1):
            return
        if not self.history:
            return
        if self._cursor is None:
            self._cursor = len(self.history)
        self._cursor = max(0, self._cursor - 1)
        self._recall(self.history[self._cursor])

    def action_history_next(self) -> None:
        if self.console.menu_move(1):
            return
        if self._cursor is None:
            return
        self._cursor += 1
        if self._cursor >= len(self.history):
            self._cursor = None
            self._recall("")
        else:
            self._recall(self.history[self._cursor])

    def _recall(self, line: str) -> None:
        # recalled lines are for editing or resubmitting; popping the menu
        # over every history step would fight the browsing. Only arm the flag
        # when the value really changes, or the Input.Changed that clears it
        # never fires and the next completion is swallowed.
        if self.value != line:
            self.console.suppress_menu()
        self.value = line
        self.cursor_position = len(line)

    def action_complete(self) -> None:
        if not self.value:
            # Tab on an empty prompt starts the command menu
            self.value = "/"
            self.cursor_position = 1
            return
        if self.console.menu_apply(submit=False) == "none":
            self.console.refresh_menu()  # reopen after Escape

    def action_menu_up(self) -> None:
        self.console.menu_move(-1)

    async def action_submit(self) -> None:
        outcome = self.console.menu_apply(submit=True)
        if outcome == "applied":
            return  # the pick advanced the line; nothing to run yet
        await super().action_submit()


class ConsoleScreen(Screen):
    BINDINGS = [
        Binding("escape", "clear_prompt", "Clear", show=False),
        Binding("ctrl+l", "clear_log", "Clear log", show=False),
        Binding("pageup", "scroll_log(-1)", "Scroll log", show=False),
        Binding("pagedown", "scroll_log(1)", "Scroll log", show=False),
    ]

    DEFAULT_CSS = """
    ConsoleScreen #brand { height: 1; padding: 0 1; color: $text-muted; text-align: right; }
    ConsoleScreen #statusbar { height: 1; background: $panel; padding: 0 1; }
    ConsoleScreen #mcpbar { height: 1; padding: 0 1; color: $text-muted; }
    ConsoleScreen RichLog { padding: 0 1; }
    ConsoleScreen #completions {
        display: none;
        height: auto;
        max-height: 12;
        border: round $primary;
        background: $panel;
        padding: 0 1;
    }
    ConsoleScreen #prompt { border: tall $primary; }
    """

    @property
    def core(self) -> AppCore:
        app: IfcConsoleApp = self.app  # type: ignore[assignment]
        return app.core

    def compose(self) -> ComposeResult:
        yield Static(branding.top_right_markup(), id="brand")
        yield Static(id="statusbar")
        yield Static(id="mcpbar")
        yield RichLog(id="log", markup=True, wrap=True, auto_scroll=True, max_lines=5000)
        menu = OptionList(id="completions")
        menu.can_focus = False  # the prompt keeps focus; the menu is picked, not focused
        menu.border_subtitle = "Tab complete · Enter pick · Esc close"
        yield menu
        yield CommandInput(
            id="prompt",
            placeholder="type / to see every command  ·  a bare path to an .ifc opens it",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._menu_state = completion.MenuState()
        self._menu_suppressed = False
        self._files_cache: list[tuple[Path, str]] | None = None
        self._files_scanning = False
        self._loading: str | None = None
        self.refresh_status()
        self.query_one("#prompt", CommandInput).focus()
        app: IfcConsoleApp = self.app  # type: ignore[assignment]
        app.begin_startup()

    # -- output helpers -----------------------------------------------------------
    def print(self, markup: str) -> None:
        log = self.query_one("#log", RichLog)
        try:
            log.write(markup)
        except Exception:
            # Interpolated values (paths, exception text) can contain
            # accidental Rich markup; never let that take the console down.
            log.write(escape(markup))

    def clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    # -- status bars ---------------------------------------------------------------
    def refresh_status(self) -> None:
        core = self.core
        s = core.session
        mode = core.policy.mode.value
        color = _MODE_COLORS.get(mode, "white")
        dirty = " [red]* unsaved[/red]" if s.dirty else ""
        taint = " [red]! tainted[/red]" if s.tainted else ""
        if self._loading:
            model = f"[yellow]loading {escape(self._loading)}…[/yellow]"
        elif s.loaded:
            model = f"{escape(s.name or '')} · {s.schema} · {s.size_bytes / 1_048_576:.1f} MB"
        else:
            model = "no model (/file)"
        extra = ""
        attached = len(core.models.sessions) - 1 if core.models.sessions else 0
        if attached > 0:
            extra += f"  +{attached} model{'s' if attached > 1 else ''}"
        if core.models.attachments:
            extra += f"  +{len(core.models.attachments)} file(s)"
        self.query_one("#statusbar", Static).update(
            f" {model}{extra}   MODE: [{color}]{mode.upper()}[/{color}]{dirty}{taint}"
        )
        if core.server_running:
            server = f"MCP {core.mcp_url}"
        elif core.server_error:
            server = "[red]MCP not running (/port)[/red]"
        else:
            server = "MCP starting…"
        if not core.viewer.enabled:
            viewer = "viewer: off (/viewer)"
        elif core.viewer.connected:
            tabs = core.viewer.connected
            viewer = f"viewer: [green]{tabs} tab{'s' if tabs > 1 else ''}[/green]"
        else:
            viewer = "viewer: no tab yet (/viewer)"
        self.query_one("#mcpbar", Static).update(f" {server}   {viewer}")

    # -- core events (forwarded by IfcConsoleApp) ---------------------------------------
    def on_core_event(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "tool_called":
            status = "[green]ok[/green]" if event.get("ok") else "[red]err[/red]"
            detail = event.get("detail") or ""
            self.print(
                f"[dim]{event['ts'][11:19]}[/dim]  {status} [b]{event['tool']}[/b]  "
                f"[dim]{event.get('duration_ms', 0)}ms  {detail}[/dim]"
            )
        elif etype == "server_started":
            self.refresh_status()
        elif etype == "server_failed":
            self.refresh_status()
            self.print(f"[red]server failed to start:[/red] {escape(str(event.get('reason')))}")
            self.print("  [dim]try /port <number> to use another port[/dim]")
        elif etype == "server_moved":
            self.refresh_status()
            self.print(
                f"[green]second session moved to port {event.get('port')}[/green] "
                f"[dim](port {event.get('original')} stays with your other "
                "ifc-console session and its MCP clients)[/dim]"
            )
        elif etype == "model_loading":
            self._loading = str(event.get("name") or "model")
            size_mb = (event.get("size_bytes") or 0) / 1_048_576
            self.print(
                f"[dim]loading {escape(self._loading)} ({size_mb:.1f} MB), "
                "the console stays usable…[/dim]"
            )
            self.refresh_status()
        elif etype == "model_load_failed":
            self._loading = None
            self.refresh_status()
        elif etype in (
            "mode_changed",
            "model_saved",
            "model_loaded",
            "model_mutated",
            "session_tainted",
            "sandbox_contained",
            "viewer_enabled",
            "viewer_disabled",
        ):
            if etype == "model_loaded":
                self._loading = None
            self.refresh_status()
            if etype == "mode_changed":
                mode = event.get("mode", "?")
                self.print(
                    f"mode changed to [{_MODE_COLORS.get(mode, 'white')}]{mode}[/] "
                    f"[dim](by {event.get('by', '?')})[/dim]"
                )
            elif etype == "model_loaded":
                detail = str(event.get("schema"))
                if event.get("size_bytes"):
                    detail += f", {event['size_bytes'] / 1_048_576:.1f} MB"
                if event.get("duration_ms"):
                    detail += f", {event['duration_ms'] / 1000:.1f}s"
                self.print(
                    f"[green]loaded[/green] [b]{escape(str(event.get('name')))}[/b] ({detail})"
                )
            elif etype == "session_tainted":
                self.print(
                    "[red]session tainted[/red]: guarded code changed the in-memory "
                    "model; /reload restores a pristine copy"
                )
            elif etype == "sandbox_contained":
                self.print(
                    "[yellow]sandbox contained a change[/yellow]: read-only code "
                    "mutated the sandbox's throwaway copy. Your model is untouched"
                )
        elif etype in (
            "model_attached",
            "model_detached",
            "model_evicted",
            "active_model_changed",
            "file_attached",
            "file_detached",
        ):
            self._loading = None
            self.refresh_status()
            name = escape(str(event.get("name") or event.get("alias") or ""))
            model_id = escape(str(event.get("model_id") or event.get("alias") or ""))
            if etype == "model_attached":
                self.print(
                    f"[green]attached[/green] [b]{name}[/b] as [cyan]{model_id}[/cyan] "
                    "[dim](read-only; /use to make it active)[/dim]"
                )
            elif etype == "model_detached":
                self.print(f"detached [cyan]{model_id}[/cyan] [dim]({name})[/dim]")
            elif etype == "model_evicted":
                self.print(
                    f"[yellow]evicted[/yellow] [cyan]{model_id}[/cyan] [dim]({name}; "
                    "made room in the workspace budget, /attach brings it back)[/dim]"
                )
            elif etype == "active_model_changed":
                self.print(f"[green]active model[/green] is now [b]{name}[/b] ({model_id})")
            elif etype == "file_attached":
                kind = escape(str(event.get("kind", "?")).upper())
                self.print(
                    f"[green]attached[/green] {kind} [b]{model_id}[/b] "
                    "[dim](the LLM can now use its path)[/dim]"
                )
            else:
                self.print(f"detached [b]{model_id}[/b]")
        elif etype in ("viewer_connected", "viewer_disconnected"):
            self.refresh_status()
            tabs = event.get("tabs", "?")
            verb = "connected" if etype == "viewer_connected" else "closed"
            self.print(f"[dim]viewer tab {verb} ({tabs} open)[/dim]")
        elif etype == "viewer_selection":
            n = event.get("count", 0)
            shown = ", ".join(event.get("guids", [])[:3])
            more = ", …" if n > 3 else ""
            self.print(
                f"[dim]viewer selection: {n} element{'s' if n != 1 else ''} ({shown}{more})[/dim]"
            )

    # -- completion menu ---------------------------------------------------------------
    def suppress_menu(self) -> None:
        """Skip the next auto-refresh (history recall must not pop the menu)."""
        self._menu_suppressed = True

    def _files(self) -> list[tuple[Path, str]] | None:
        """Cached IFC scan for the /open menu, or None while it is running.

        The scan hits the filesystem, so it goes to a thread exactly as the
        file picker does; a synchronous call here freezes the prompt.
        """
        if self._files_cache is None and not self._files_scanning:
            self._files_scanning = True
            self.run_worker(self._scan_files(), exclusive=True, group="open-scan")
        return self._files_cache

    async def _scan_files(self) -> None:
        try:
            self._files_cache = await asyncio.to_thread(discover_ifc_files, self.core)
        finally:
            self._files_scanning = False
        self.refresh_menu()

    def refresh_menu(self) -> None:
        prompt = self.query_one("#prompt", CommandInput)
        menu = self.query_one("#completions", OptionList)
        if self._menu_suppressed or prompt.cursor_position < len(prompt.value):
            self._menu_suppressed = False
            state = completion.MenuState()
        else:
            state = completion.complete(prompt.value, self.core, self._files)
        self._menu_state = state
        if state.context != "open":
            self._files_cache = None
        menu.clear_options()
        if state.empty:
            menu.display = False
            return
        width = max(len(c.shown) for c in state.candidates)
        for cand in state.candidates:
            row = escape(cand.shown.ljust(width))
            if cand.annotation:
                row += f"  [dim]{escape(cand.annotation)}[/dim]"
            menu.add_option(Option(row, disabled=cand.disabled))
        menu.display = True
        count = len(state.candidates)
        menu.border_title = f"{count} option{'s' if count != 1 else ''}"
        menu.highlighted = next((i for i, c in enumerate(state.candidates) if not c.disabled), None)

    def hide_menu(self) -> bool:
        menu = self.query_one("#completions", OptionList)
        was_open = menu.display
        menu.display = False
        self._menu_state = completion.MenuState()
        return was_open

    def menu_move(self, delta: int) -> bool:
        """Move the menu highlight; False when the menu is closed."""
        menu = self.query_one("#completions", OptionList)
        if not menu.display:
            return False
        if delta < 0:
            menu.action_cursor_up()
        else:
            menu.action_cursor_down()
        return True

    def menu_apply(self, *, submit: bool) -> str:
        """Insert the highlighted candidate into the prompt.

        Returns "none" (menu closed or nothing pickable: caller handles the
        key), "applied" (line advanced, keep typing), or "submit" (the pick
        completed a runnable line and, on Enter, the caller submits it).
        """
        menu = self.query_one("#completions", OptionList)
        state = self._menu_state
        index = menu.highlighted if menu.display else None
        if index is None or index >= len(state.candidates):
            return "none"
        cand = state.candidates[index]
        if cand.disabled:
            return "none"
        prompt = self.query_one("#prompt", CommandInput)
        if submit and cand.terminal:
            prompt.value = state.apply(cand).strip()
            prompt.cursor_position = len(prompt.value)
            self.hide_menu()
            return "submit"
        prompt.value = state.apply(cand)
        prompt.cursor_position = len(prompt.value)
        return "applied"

    # -- input ------------------------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "prompt":
            self.refresh_menu()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "prompt":
            return
        line = event.value.strip()
        prompt = self.query_one("#prompt", CommandInput)
        prompt.value = ""
        if not line:
            return
        prompt.remember(line)
        self.print(f"[b cyan]>[/b cyan] {escape(line)}")
        self.run_worker(commands.dispatch(self, line), exclusive=False)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "completions":
            return
        # a mouse pick behaves exactly like highlighting the row and
        # pressing Enter, including running a completed line
        event.option_list.highlighted = event.option_index
        prompt = self.query_one("#prompt", CommandInput)
        if self.menu_apply(submit=True) == "submit":
            self.call_next(prompt.action_submit)
        prompt.focus()

    def action_clear_prompt(self) -> None:
        if self.hide_menu():
            return
        self.query_one("#prompt", CommandInput).value = ""

    def action_clear_log(self) -> None:
        self.clear_log()

    def action_scroll_log(self, direction: int) -> None:
        log = self.query_one("#log", RichLog)
        if direction < 0:
            log.scroll_page_up()
        else:
            log.scroll_page_down()

    # -- modal helpers used by commands ------------------------------------------------
    async def confirm(self, title: str, text: str = "") -> bool:
        return bool(await self.app.push_screen_wait(ConfirmModal(title, text)))

    async def open_file_picker(self, initial_filter: str = "") -> None:
        picked = await self.app.push_screen_wait(
            FilePickerModal(self.core, initial_filter=initial_filter)
        )
        if picked is not None:
            await commands._open_path(self, picked)

    async def open_workspace_panel(self) -> bool:
        """True when a panel choice was applied, False on cancel."""
        # The panel scans on mount, which raises when indexing is off.
        if not self.core.settings.workspace.enabled:
            self.print(
                "[red]workspace indexing is disabled[/red]; /settings "
                "workspace.enabled true turns it on"
            )
            return False
        choice = await self.app.push_screen_wait(WorkspaceModal(self.core))
        if choice:
            await commands.apply_workspace_choice(self, choice)
            return True
        return False
