"""The workspace panel: the optional multi-file surface for /workspace.

One model is the default and /file still opens the single picker. This panel
is the second option: it lists every supported BIM file under the allowed
folders, checkboxes to pick several, and applies them as one active model
plus read-only companions.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

from ifc_console.workspace.index import FileEntry

if TYPE_CHECKING:
    from ifc_console.app import AppCore

# Kinds ifc-console can actually do something with today; the rest is behind
# ctrl+u so the panel does not pretend to support what it cannot read.
_USEFUL_KINDS = ("ifc", "ids")


@dataclass
class WorkspaceChoice:
    """What the user checked, split by role."""

    models: list[Path] = field(default_factory=list)
    files: list[Path] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.models or self.files)


def _detail(entry: FileEntry) -> str:
    bits = [f"{entry.size_bytes / 1_048_576:.1f} MB"]
    if entry.kind == "ifc":
        bits.append(str(entry.detail.get("schema") or "?"))
    if entry.kind == "ids":
        specs = entry.detail.get("specifications")
        bits.append(f"{specs} spec{'s' if specs != 1 else ''}" if specs is not None else "IDS")
    if entry.kind == "bcf":
        topics = entry.detail.get("topics")
        bits.append(f"{topics} topic{'s' if topics != 1 else ''}" if topics is not None else "BCF")
    if entry.discipline:
        bits.insert(0, entry.discipline)
    if entry.revision:
        bits.append(entry.revision)
    return " · ".join(bits)


class WorkspaceModal(ModalScreen["WorkspaceChoice | None"]):
    """Tab checks files, Enter opens them, and Esc cancels."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "cursor_up", show=False, priority=True),
        Binding("down", "cursor_down", show=False, priority=True),
        # Tab checks a file, Enter opens what is checked, from either focus:
        # without priority the SelectionList would swallow Enter as a toggle.
        # Modified keys such as shift+enter never reach the app in most
        # terminals, so ctrl+o is the only alias and the button always works.
        Binding("enter", "apply", show=False, priority=True),
        Binding("ctrl+o", "apply", show=False, priority=True),
        Binding("tab", "toggle", show=False, priority=True),
        Binding("ctrl+a", "select_all", show=False, priority=True),
        Binding("ctrl+d", "select_none", show=False, priority=True),
        Binding("ctrl+u", "toggle_unsupported", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    WorkspaceModal { align: center middle; }
    WorkspaceModal > Vertical {
        width: 92%; max-width: 120; height: 85%;
        border: heavy $primary; background: $surface; padding: 1 2;
    }
    WorkspaceModal SelectionList { height: 1fr; border: round $panel; }
    WorkspaceModal .hint { color: $text-muted; height: 1; }
    WorkspaceModal .controls { color: $text-muted; height: auto; margin-top: 1; }
    WorkspaceModal .lede { color: $text-muted; height: auto; }
    WorkspaceModal .buttons { height: 3; align-horizontal: right; }
    WorkspaceModal Button { margin: 0 1; }
    """

    def __init__(self, core: AppCore) -> None:
        super().__init__()
        self.core = core
        self._entries: list[FileEntry] = []
        self._visible: list[FileEntry] = []
        self._checked: set[Path] = set()
        self._show_all = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Workspace"),
            Static(
                "One model is the default. Check extra files to load them "
                "alongside it: IFC models attach read-only, an IDS is handed to "
                "the LLM for validate_ids.",
                classes="lede",
            ),
            Input(placeholder="type to filter", id="filter"),
            SelectionList(id="files"),
            Static("", classes="hint", id="footer"),
            Horizontal(
                Button("Check all", id="select-all"),
                Button("Clear checks", id="select-none"),
                Button("Open selection", id="apply", variant="primary"),
                Button("Cancel", id="cancel"),
                classes="buttons",
            ),
            Static(
                "[b]Tab[/b] check/uncheck · [b]Enter[/b] open selection "
                "(or Ctrl+O) · [b]Up/Down[/b] move\n"
                "[b]Ctrl+A[/b] check all · [b]Ctrl+D[/b] clear · "
                "[b]Ctrl+U[/b] show other types · [b]Esc[/b] cancel",
                classes="controls",
            ),
        )

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()
        self.query_one("#footer", Static).update("[dim]scanning…[/dim]")
        # a worker keeps Esc and the buttons responsive during a long scan
        self.run_worker(self._load_entries, exclusive=True)

    async def _load_entries(self) -> None:
        entries = await asyncio.to_thread(self._scan)
        if not self.is_attached:
            return  # cancelled while scanning
        self._entries = entries
        self._apply_filter(self.query_one("#filter", Input).value)

    def _scan(self) -> list[FileEntry]:
        self.core.workspace.scan()
        return list(self.core.workspace.entries)

    # -- rendering -----------------------------------------------------------
    def _row(self, entry: FileEntry) -> str:
        try:
            shown = entry.path.relative_to(Path.cwd())
        except ValueError:
            shown = entry.path
        role = ""
        active = self.core.session
        attached = self.core.models.id_for_path(entry.path) or any(
            a.path == entry.path for a in self.core.models.attachments.values()
        )
        if active.loaded and active.path == entry.path:
            role = "  [green]active[/green]"
        elif attached:
            role = "  [cyan]attached[/cyan]"
        return (
            f"[b]{entry.label:<4}[/b] {escape(str(shown))}  "
            f"[dim]{escape(_detail(entry))}[/dim]{role}"
        )

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        options = self.query_one("#files", SelectionList)
        options.clear_options()
        self._visible = []
        for entry in self._entries:
            if not self._show_all and entry.kind not in _USEFUL_KINDS:
                continue
            if needle and needle not in str(entry.path).lower() and needle != entry.kind:
                continue
            self._visible.append(entry)
        if self._visible:
            options.add_options(
                Selection(self._row(e), i, e.path in self._checked)
                for i, e in enumerate(self._visible)
            )
            options.highlighted = 0
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        models = sum(1 for p in self._checked if self._kind_of(p) == "ifc")
        others = len(self._checked) - models
        stats = self.core.workspace.stats()
        shown = f"{len(self._visible)} of {stats['files']} indexed"
        if not self._visible:
            shown = "no matching files; /open <path> always works"
        picked = f"checked: {models} model(s), {others} companion file(s)"
        resident = (
            f"resident {len(self.core.models.sessions)}/{self.core.models.max_resident}"
        )
        self.query_one("#footer", Static).update(f"[dim]{shown} · {picked} · {resident}[/dim]")

    def _kind_of(self, path: Path) -> str | None:
        for entry in self._entries:
            if entry.path == path:
                return entry.kind
        return None

    # -- events --------------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter opens from the filter box too, so filter then Enter is the
        # single-file path; Tab is what builds a bigger selection.
        if event.input.id == "filter":
            self.action_apply()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "apply":
            self.action_apply()
        elif event.button.id == "select-all":
            self.action_select_all()
        elif event.button.id == "select-none":
            self.action_select_none()
        else:
            self.action_cancel()

    def on_selection_list_selected_changed(self, event: SelectionList.SelectedChanged) -> None:
        selected = {self._visible[i].path for i in event.selection_list.selected}
        visible = {e.path for e in self._visible}
        # keep checks made under a different filter
        self._checked = (self._checked - visible) | selected
        self._refresh_footer()

    # -- actions -------------------------------------------------------------
    def action_cursor_up(self) -> None:
        self.query_one("#files", SelectionList).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#files", SelectionList).action_cursor_down()

    def action_toggle(self) -> None:
        options = self.query_one("#files", SelectionList)
        if options.highlighted is None:
            return
        options.toggle(options.get_option_at_index(options.highlighted).value)
        # step down so a run of files can be checked with repeated Tab
        if options.highlighted < options.option_count - 1:
            options.action_cursor_down()

    def action_select_all(self) -> None:
        self.query_one("#files", SelectionList).select_all()

    def action_select_none(self) -> None:
        self.query_one("#files", SelectionList).deselect_all()

    def action_toggle_unsupported(self) -> None:
        self._show_all = not self._show_all
        self._apply_filter(self.query_one("#filter", Input).value)

    def action_apply(self) -> None:
        checked = [entry.path for entry in self._entries if entry.path in self._checked]
        if not checked:
            # nothing checked behaves exactly like the single-file picker
            options = self.query_one("#files", SelectionList)
            index = options.highlighted
            if index is None or not self._visible:
                self.dismiss(None)
                return
            checked = [self._visible[index].path]
        kinds = {p: self._kind_of(p) for p in checked}
        choice = WorkspaceChoice(
            models=[p for p in checked if kinds[p] == "ifc"],
            files=[p for p in checked if kinds[p] != "ifc"],
        )
        self.dismiss(choice)

    def action_cancel(self) -> None:
        self.dismiss(None)
