"""The workspace panel: the optional multi-file surface for /workspace.

One model is the default and /file still opens the single picker. This panel
is the second option: it lists every supported BIM file under the allowed
folders, checkboxes to pick several, and applies them as one active model
plus read-only companions.
"""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, SelectionList, Static
from textual.widgets.selection_list import Selection

from ifc_console.workspace.index import FileEntry, WorkspaceIndex
from ifc_console.workspace.kinds import KINDS, kind

if TYPE_CHECKING:
    from ifc_console.app import AppCore


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
        Binding("ctrl+l", "clear_filter", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    WorkspaceModal { align: center middle; }
    WorkspaceModal > Vertical {
        width: 92%; max-width: 120; height: 95%;
        border: heavy $primary; background: $surface; padding: 1 2;
    }
    WorkspaceModal SelectionList { height: 1fr; border: round $panel; }
    WorkspaceModal .hint { color: $text-muted; height: auto; }
    WorkspaceModal .controls { color: $text-muted; height: auto; margin-top: 1; }
    WorkspaceModal .lede { color: $text-muted; height: auto; }
    WorkspaceModal #roots { color: $text-muted; height: 1; }
    WorkspaceModal .buttons { height: 3; align-horizontal: right; }
    WorkspaceModal Button { margin: 0 1; }
    """

    def __init__(self, core: AppCore, *, root: Path | None = None) -> None:
        super().__init__()
        self.core = core
        self.root = root
        # A preview must not alter the live MCP index, browse root or access
        # scope. The console commits the chosen root only after acceptance.
        roots = [root] if root is not None else list(core.allowed_dirs)
        self._index = WorkspaceIndex(
            lambda: roots,
            cap=core.workspace.cap,
            depth=core.workspace.depth,
            enabled=lambda: core.settings.workspace.enabled,
        )
        self._useful_kinds = {"ifc"} | {spec.name for spec in KINDS if spec.consumed_by}
        self._validation_installed = importlib.util.find_spec("ifctester") is not None
        self._entries: list[FileEntry] = []
        self._visible: list[FileEntry] = []
        self._checked: set[Path] = set()
        self._show_all = False

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Workspace"),
            Static(
                "Extra IFCs attach read-only; with no active model, the first opens. "
                "Companion paths are registered only, not analysed.",
                classes="lede",
            ),
            Static(
                Text(
                    "Folder(s): "
                    + (
                        str(self.root)
                        if self.root is not None
                        else ", ".join(str(path) for path in self.core.allowed_dirs)
                    ),
                    no_wrap=True,
                    overflow="ellipsis",
                ),
                id="roots",
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
                "[b]Tab[/b] check · [b]Enter/Ctrl+O[/b] open · [b]Esc[/b] cancel\n"
                "[b]Ctrl+A/D[/b] all/none · [b]Ctrl+U[/b] other types · [b]Ctrl+L[/b] clear filter",
                classes="controls",
            ),
        )

    def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()
        self.query_one("#roots", Static).tooltip = (
            str(self.root)
            if self.root is not None
            else "\n".join(str(path) for path in self.core.allowed_dirs)
        )
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
        return list(self._index.scan())

    # -- rendering -----------------------------------------------------------
    def _row(self, entry: FileEntry) -> str:
        try:
            shown = entry.path.relative_to(self.root or self.core.launch_dir)
        except ValueError:
            shown = entry.path
        role = ""
        active = self.core.session
        attached = any(
            entry.path in (session.path, session.origin_path)
            for session in self.core.models.sessions.values()
        ) or any(a.path == entry.path for a in self.core.models.attachments.values())
        if active.loaded and entry.path in (active.path, active.origin_path):
            role = "  [green]active IFC[/green]"
        elif attached:
            role = (
                "  [cyan]attached IFC[/cyan]"
                if entry.kind == "ifc"
                else "  [cyan]attached companion[/cyan]"
            )
        elif entry.kind == "ifc":
            role = "  [dim]IFC model[/dim]"
        if entry.kind != "ifc":
            spec = kind(entry.kind)
            if entry.kind in self._useful_kinds and spec and spec.consumed_by:
                role += f"  [dim]companion · {escape(spec.consumed_by)}[/dim]"
                if entry.kind == "ids" and not self._validation_installed:
                    role += "  [yellow]requires validation extra[/yellow]"
            elif spec and spec.opens_as == "document":
                role += "  [dim]document path only · attach in Codex or Agent Content[/dim]"
            else:
                role += "  [dim]reference only · no available consumer[/dim]"
        return (
            f"[b]{entry.label:<4}[/b] {escape(str(shown))}  "
            f"[dim]{escape(_detail(entry))}[/dim]{role}"
        )

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        options = self.query_one("#files", SelectionList)
        options.clear_options()
        self._visible = []
        self.query_one("#filter", Input).border_title = "Filter (Ctrl+L clears)"
        for entry in self._entries:
            if not self._show_all and entry.kind not in self._useful_kinds:
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
        stats = self._index.stats()
        shown = f"{len(self._visible)} of {stats['files']} indexed"
        if not self._visible:
            shown = 'no matching files; Ctrl+L clears the filter; Esc then /file "<path>"'
        picked = f"checked: {models} model(s), {others} companion file(s)"
        resident = f"resident {len(self.core.models.sessions)}/{self.core.models.max_resident}"
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

    def action_clear_filter(self) -> None:
        field = self.query_one("#filter", Input)
        field.value = ""
        field.focus()

    def action_apply(self) -> None:
        checked = [entry.path for entry in self._entries if entry.path in self._checked]
        if not checked:
            # nothing checked behaves exactly like the single-file picker
            options = self.query_one("#files", SelectionList)
            index = options.highlighted
            if index is None or not self._visible:
                self._refresh_footer()
                self.query_one("#filter", Input).focus()
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
