"""Modal screens, reserved for the user's own decisions: confirmations, quit.

Routine choices (commands, modes, files, settings) never modal; they go
through the console's inline completion menu. AI-side permissioning lives
in the AI client, not here.
"""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, OptionList, Static
from textual.widgets.option_list import Option


class ConfirmModal(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "Yes"),
        Binding("n", "no", "No"),
        Binding("escape", "no", "No", show=False),
    ]

    DEFAULT_CSS = """
    ConfirmModal { align: center middle; }
    ConfirmModal > Vertical {
        width: 70%; max-width: 90; border: heavy $primary;
        background: $surface; padding: 1 2;
    }
    ConfirmModal .buttons { height: 3; align-horizontal: center; }
    ConfirmModal Button { margin: 0 1; }
    """

    def __init__(self, title: str, text: str = "") -> None:
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title)
            if self._text:
                yield Static(self._text)
            with Horizontal(classes="buttons"):
                yield Button("[y] Yes", id="yes", variant="primary")
                yield Button("[n] No", id="no")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class AgentPickerModal(ModalScreen["str | None"]):
    """Arrows to move, Enter to open the agent in the chat panel, Esc cancels."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    AgentPickerModal { align: center middle; }
    AgentPickerModal > Vertical {
        width: 80%; max-width: 100; border: heavy $primary;
        background: $surface; padding: 1 2;
    }
    AgentPickerModal OptionList { height: auto; max-height: 14; border: round $panel; }
    AgentPickerModal .hint { color: $text-muted; height: 1; }
    """

    def __init__(self, agents: list[tuple[str, str, str]]) -> None:
        """agents: (name, title, description) per row."""
        super().__init__()
        self._agents = agents

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("Open an agent in the chat panel")
            yield OptionList(id="agents")
            yield Static("up/down select · Enter open · Esc cancel", classes="hint")

    def on_mount(self) -> None:
        options = self.query_one("#agents", OptionList)
        for name, title, description in self._agents:
            options.add_option(
                Option(
                    f"[b]{escape(title)}[/b] [dim]({escape(name)})[/dim]\n"
                    f"  [dim]{escape(description)}[/dim]"
                )
            )
        if self._agents:
            options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if 0 <= event.option_index < len(self._agents):
            self.dismiss(self._agents[event.option_index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class QuitModal(ModalScreen[str]):
    """Unsaved-changes prompt; returns 'save' | 'discard' | 'cancel'."""

    BINDINGS = [
        Binding("s", "save", "Save"),
        Binding("d", "discard", "Discard"),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    QuitModal { align: center middle; }
    QuitModal > Vertical {
        width: 70%; max-width: 80; border: heavy $error;
        background: $surface; padding: 1 2;
    }
    QuitModal .buttons { height: 3; align-horizontal: center; }
    QuitModal Button { margin: 0 1; }
    """

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label("The model has unsaved changes.")
            with Horizontal(classes="buttons"):
                yield Button("[s] Save & quit", id="save", variant="primary")
                yield Button("[d] Discard & quit", id="discard", variant="error")
                yield Button("Cancel", id="cancel")

    def action_save(self) -> None:
        self.dismiss("save")

    def action_discard(self) -> None:
        self.dismiss("discard")

    def action_cancel(self) -> None:
        self.dismiss("cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "cancel")
