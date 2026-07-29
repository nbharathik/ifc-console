"""File picker modal for the console's /file command.

Lists recent models plus every IFC file found near the working directory,
filterable as you type.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

if TYPE_CHECKING:
    from ifc_console.app import AppCore

_IFC_SUFFIXES = (".ifc", ".ifczip", ".ifcxml")
_SCAN_CAP = 400  # stop scanning after this many files; /open <path> always works


def discover_ifc_files(core: AppCore) -> list[tuple[Path, str]]:
    """(path, detail) pairs: recents first, then files near cwd and the
    allowed directories, newest first."""
    seen: set[Path] = set()
    out: list[tuple[Path, str]] = []

    for entry in core.recents.entries():
        path = Path(entry["path"])
        if path.exists() and path.resolve() not in seen:
            seen.add(path.resolve())
            size_mb = entry.get("size_bytes", 0) / 1_048_576
            out.append((path, f"recent · {size_mb:.1f} MB · {entry.get('schema', '?')}"))

    scan_roots: list[Path] = []
    for root in [Path.cwd(), *core.allowed_dirs]:
        if root not in scan_roots:
            scan_roots.append(root)

    found: list[Path] = []
    for root in scan_roots:
        try:
            # cwd itself, then one level of subdirectories: fast and predictable
            candidates = list(root.glob("*")) + list(root.glob("*/*"))
        except OSError:
            continue
        for path in candidates:
            if len(found) >= _SCAN_CAP:
                break
            if path.suffix.lower() not in _IFC_SUFFIXES or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)

    def mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    found.sort(key=mtime, reverse=True)
    for path in found:
        try:
            size_mb = path.stat().st_size / 1_048_576
            out.append((path, f"{size_mb:.1f} MB"))
        except OSError:
            continue
    return out


class FilePickerModal(ModalScreen["Path | None"]):
    """Type to filter, arrows to move, Enter to open, Escape to cancel."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
        Binding("up", "cursor_up", show=False, priority=True),
        Binding("down", "cursor_down", show=False, priority=True),
    ]

    DEFAULT_CSS = """
    FilePickerModal { align: center middle; }
    FilePickerModal > Vertical {
        width: 90%; max-width: 110; height: 80%;
        border: heavy $primary; background: $surface; padding: 1 2;
    }
    FilePickerModal OptionList { height: 1fr; border: round $panel; }
    FilePickerModal .hint { color: $text-muted; height: 1; }
    """

    def __init__(self, core: AppCore, initial_filter: str = "") -> None:
        super().__init__()
        self.core = core
        self.initial_filter = initial_filter
        self._entries: list[tuple[Path, str]] = []
        self._visible: list[Path] = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Open an IFC model")
            yield Input(
                value=self.initial_filter,
                placeholder="type to filter; Enter opens the highlighted file",
                id="filter",
            )
            yield OptionList(id="files")
            yield Static(
                "up/down select · Enter open · Esc cancel · /open <path> for anything else",
                classes="hint",
            )

    async def on_mount(self) -> None:
        self.query_one("#filter", Input).focus()
        options = self.query_one("#files", OptionList)
        options.add_option(Option("(scanning for IFC files…)", disabled=True))
        # the scan hits the filesystem; keep it off the UI thread
        self._entries = await asyncio.to_thread(discover_ifc_files, self.core)
        self._apply_filter(self.query_one("#filter", Input).value)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        options = self.query_one("#files", OptionList)
        options.clear_options()
        self._visible = []
        cwd = Path.cwd()
        for path, detail in self._entries:
            if needle and needle not in str(path).lower():
                continue
            try:
                shown = path.relative_to(cwd)
            except ValueError:
                shown = path
            self._visible.append(path)
            options.add_option(Option(f"{escape(str(shown))}  [dim]({escape(detail)})[/dim]"))
        if not self._visible:
            options.add_option(
                Option("(no matching IFC files; use /open <path>)", disabled=True)
            )
        else:
            options.highlighted = 0

    # -- events ------------------------------------------------------------------
    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "filter":
            return
        options = self.query_one("#files", OptionList)
        index = options.highlighted if options.highlighted is not None else 0
        if self._visible and 0 <= index < len(self._visible):
            self.dismiss(self._visible[index])
        else:
            self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._visible):
            self.dismiss(self._visible[event.option_index])

    def action_cursor_up(self) -> None:
        self.query_one("#files", OptionList).action_cursor_up()

    def action_cursor_down(self) -> None:
        self.query_one("#files", OptionList).action_cursor_down()

    def action_cancel(self) -> None:
        self.dismiss(None)
