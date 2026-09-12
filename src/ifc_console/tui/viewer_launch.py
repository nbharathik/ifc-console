"""Present one existing viewer URL without creating another model session."""

from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING

from rich.markup import escape

if TYPE_CHECKING:
    from ifc_console.tui.console import ConsoleScreen


def present_viewer_url(console: ConsoleScreen, url: str, target: str) -> None:
    try:
        copied = console.app.copy_to_clipboard(url) is not False
    except Exception:
        copied = False
    clipboard = "URL copied" if copied else "copy the full URL below"
    console.print(f"viewer link ready ({clipboard}):")
    console.print(f"[link={escape(url)}]{escape(url)}[/link]")
    if target == "vscode":
        console.print(
            "In VS Code, Ctrl+click the link (Cmd+click on macOS) with "
            "workbench.browser.openLocalhostLinks enabled."
        )
        console.print(
            "Or run [b]Browser: Open Integrated Browser[/b] from the Command Palette "
            "and paste the full URL. If unavailable, use /viewer browser."
        )
        return
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if opened:
        console.print("[green]browser launch requested[/green]; waiting for a viewer connection")
    else:
        console.print("[yellow]browser did not open[/yellow]; paste the URL into your browser")
