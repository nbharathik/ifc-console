"""Slash-command registry for the console TUI.

Each command is a small async handler that receives the console screen and
the raw argument string. Keeping them in a registry (instead of an if-chain)
gives /help, completion, and the docs one source of truth.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from rich.markup import escape

from ifc_console.core.results import ToolError
from ifc_console.policy.modes import Mode
from ifc_console.workspace.kinds import detect_kind

if TYPE_CHECKING:
    from ifc_console.tui.console import ConsoleScreen

Handler = Callable[["ConsoleScreen", str], Awaitable[None]]

_IFC_SUFFIXES = (".ifc", ".ifczip", ".ifcxml")
_CONNECT_CLIENTS = ("claude-code", "claude-desktop", "cursor", "vscode", "codex")
_CONNECT_TARGETS = {
    "claude-code": "run this command once; it is saved at user scope",
    "claude-desktop": "merge into claude_desktop_config.json; then restart Claude Desktop",
    "cursor": "merge into the global ~/.cursor/mcp.json; then reload MCP servers",
    "vscode": "merge into MCP: Open User Configuration; then start the MCP server",
    "codex": "merge into ~/.codex/config.toml; then restart Codex",
}


@dataclass(frozen=True)
class Command:
    name: str
    usage: str
    help: str
    group: str
    handler: Handler
    examples: tuple[str, ...] = ()


REGISTRY: dict[str, Command] = {}
# Names that moved in 0.1.4. They keep working; typing one prints where it went.
RENAMED = {"open": "file", "model": "info"}
ALIASES = {"exit": "quit", **RENAMED}


def resolve_prefix(prefix: str) -> set[str]:
    """Real command names a typed prefix could mean, aliases included.

    Aliases resolve to the command they name, so /ex finds /quit instead of
    reporting an unknown command.
    """
    names = {name: name for name in REGISTRY}
    names.update(ALIASES)
    return {names[n] for n in names if n.startswith(prefix)}


# Display order for /help. Plain words, one per thing you might be doing.
_GROUPS = ("files", "models", "session", "connect", "console")


def command(
    name: str,
    usage: str,
    help_text: str,
    group: str,
    examples: tuple[str, ...] = (),
) -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        REGISTRY[name] = Command(
            name=name,
            usage=usage,
            help=help_text,
            group=group,
            handler=fn,
            examples=examples,
        )
        return fn

    return wrap


def _strip_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _client_config(core: Any, client: str) -> str:
    """Build one copy-ready configuration for the shared console."""
    from ifc_console.cli import build_config_snippet

    return build_config_snippet(
        client,
        None,
        port=core.port,
        # The active model is session state selected with /file, never config.
        file=None,
        mode=core.policy.mode.value,
        token=(core.token if core.settings.server.token_in_config_snippets else None),
        bridge_token=(
            None
            if core.settings.server.persistent_token
            else (core.token if core.settings.server.token_in_config_snippets else "<TOKEN>")
        ),
    )


async def dispatch(console: ConsoleScreen, line: str) -> None:
    """Parse one input line and run the matching command."""
    line = line.strip()
    if not line:
        return
    # Convenience: a bare path to an IFC file just opens it. POSIX absolute
    # paths start with "/" like commands do, so an existing .ifc path wins
    # over command parsing; "/open model.ifc" has no existing file behind it
    # and falls through to the registry.
    candidate = Path(_strip_quotes(line)).expanduser()
    if candidate.suffix.lower() in _IFC_SUFFIXES and (
        not line.startswith("/") or candidate.exists()
    ):
        await _open_path(console, candidate)
        return
    if not line.startswith("/"):
        console.print(
            "[dim]commands start with / (try [b]/help[/b]); "
            "prompts belong in your MCP client, not here[/dim]"
        )
        return
    name, _, args = line[1:].partition(" ")
    typed = name.lower()
    name = ALIASES.get(typed, typed)
    if typed in RENAMED:
        console.print(f"[dim]/{typed} is now /{RENAMED[typed]} (the old name still works)[/dim]")
    cmd = REGISTRY.get(name)
    if cmd is None:
        matches = sorted(resolve_prefix(name))
        if len(matches) == 1:
            cmd = REGISTRY[matches[0]]
        else:
            hint = f" (did you mean {', '.join('/' + m for m in matches)}?)" if matches else ""
            console.print(f"[red]unknown command /{escape(name)}[/red]{hint}; try /help")
            return
    try:
        await cmd.handler(console, args.strip())
    except Exception as exc:  # a broken command must not kill the console
        console.print(f"[red]/{cmd.name} failed: {escape(f'{type(exc).__name__}: {exc}')}[/red]")


# --------------------------------------------------------------------- helpers

# ToolError hints are written for the LLM and name MCP tools a person in the
# terminal cannot call. These are the same instructions as slash commands.
_TUI_HINTS = {
    "MODEL_NOT_FOUND": "/models lists what is loaded",
    "MODEL_READ_ONLY": "/use <id> makes a model writable first",
    "UNSAVED_CHANGES": "/save first, or /reload to discard the changes",
    "INVALID_INPUT": "supported companions: .ids, .bcf/.bcfzip, .csv",
    "WORKSPACE_DISABLED": "/settings workspace.enabled true turns indexing on",
}


def _console_hint(exc: ToolError) -> str:
    return _TUI_HINTS.get(exc.code, exc.hint)


def _require_server(console: ConsoleScreen) -> bool:
    """Browser features need the HTTP server. Say why it is missing."""
    core = console.core
    if core.server_running:
        return True
    if core.server_error:
        console.print(
            f"[red]the MCP server is not running:[/red] {escape(core.server_error)}\n"
            f"[dim]this needs the server. Free port {core.port} (quit the other "
            f"ifc-console) or move this one with /port {core.port + 1}[/dim]"
        )
    else:
        console.print(
            "[red]the MCP server is still starting[/red] [dim]try again in a "
            "moment; /status shows when it is up[/dim]"
        )
    return False


async def _open_path(console: ConsoleScreen, path: Path) -> bool:
    core = console.core
    path = path.expanduser().resolve()
    if not path.exists():
        console.print(f"[red]{escape(str(path))} does not exist[/red]")
        return False
    discard_dirty = core.session.dirty
    if discard_dirty and not await console.confirm(
        "Discard unsaved changes and open another model?"
    ):
        return False
    core.add_allowed_dir(path.parent)
    try:
        # progress and the loaded line arrive via model_loading/model_loaded events
        await core.open_model(path, discard_dirty=discard_dirty)
    except ToolError as exc:
        console.print(
            f"[red]could not load {escape(path.name)}: {escape(exc.message)}[/red] "
            f"[dim]{escape(_console_hint(exc))}[/dim]"
        )
        return False
    except Exception as exc:
        console.print(f"[red]could not load {escape(path.name)}: {escape(str(exc))}[/red]")
        return False
    return True


async def _attach_path(console: ConsoleScreen, path: Path) -> bool:
    """Load one extra file alongside the active model. True when it landed."""
    core = console.core
    path = path.expanduser().resolve()
    if not path.exists():
        console.print(f"[red]{escape(str(path))} does not exist[/red]")
        return False
    core.add_allowed_dir(path.parent)
    try:
        if await asyncio.to_thread(detect_kind, path) == "ifc":
            # "auto" attaches only if a model is active, decided under the lock
            await core.open_model(path, attach="auto")
        else:
            await core.attach_file(path)
    except ToolError as exc:
        console.print(
            f"[red]could not attach {escape(path.name)}: {escape(exc.message)}[/red] "
            f"[dim]{escape(_console_hint(exc))}[/dim]"
        )
        return False
    except Exception as exc:
        console.print(f"[red]could not attach {escape(path.name)}: {escape(str(exc))}[/red]")
        return False
    return True


async def apply_workspace_choice(console: ConsoleScreen, choice) -> None:
    """Apply a workspace panel selection: one active model, the rest attached."""
    core = console.core
    models = list(choice.models)
    landed = 0
    if models and not core.session.loaded:
        while models and not core.session.loaded:
            first = models.pop(0)
            if await _open_path(console, first):
                landed += 1
    for path in models:
        if core.session.path == path:
            continue
        if await _attach_path(console, path):
            landed += 1
    for path in choice.files:
        if await _attach_path(console, path):
            landed += 1
    if landed:
        console.print(
            "[dim]attached files stay read-only; the LLM sees them through list_models[/dim]"
        )


def _mode_color(mode: str) -> str:
    return {"ask": "green", "edit": "red"}.get(mode, "white")


# -------------------------------------------------------------------- commands
@command(
    "help",
    "/help [command]",
    "list all commands, or explain one",
    "console",
    examples=("/help", "/help file"),
)
async def _help(console: ConsoleScreen, args: str) -> None:
    wanted = _strip_quotes(args).lstrip("/").lower()
    if wanted:
        cmd = REGISTRY.get(ALIASES.get(wanted, wanted))
        if cmd is None:
            matches = sorted(resolve_prefix(wanted))
            if len(matches) != 1:
                hint = f" (did you mean {', '.join('/' + m for m in matches)}?)" if matches else ""
                console.print(f"[red]no command /{escape(wanted)}[/red]{hint}")
                return
            cmd = REGISTRY[matches[0]]
        lines = [f"[b][cyan]{cmd.usage}[/cyan][/b]  [dim]({cmd.group})[/dim]", f"  {cmd.help}"]
        if cmd.examples:
            lines.append("  [dim]examples[/dim]")
            lines += [f"    [cyan]{escape(example)}[/cyan]" for example in cmd.examples]
        alias = [old for old, new in RENAMED.items() if new == cmd.name]
        if alias:
            lines.append(f"  [dim]also answers to /{alias[0]}[/dim]")
        console.print("\n".join(lines))
        return

    width = max(len(c.usage) for c in REGISTRY.values())
    lines = ["[b]commands[/b]  [dim](/help <command> explains one)[/dim]"]
    for group in _GROUPS:
        members = [c for c in REGISTRY.values() if c.group == group]
        if not members:
            continue
        lines.append(f"  [dim]{group}[/dim]")
        for cmd in members:
            lines.append(f"    [cyan]{cmd.usage:<{width}}[/cyan]  {cmd.help}")
    lines.append("")
    lines.append(
        "  typing / opens the command menu: Tab completes, Up/Down or the mouse "
        "pick, Enter selects, Esc closes"
    )
    lines.append(
        "  a bare path to an .ifc file opens it · Up/Down recall history · "
        "PgUp/PgDn scroll the feed"
    )
    console.print("\n".join(lines))


@command(
    "tools",
    "/tools [all|slash|ai|prompts|resources|settings|search]",
    "explore every TUI command, AI function, prompt, resource, and setting",
    "console",
    examples=(
        "/tools",
        "/tools ai",
        "/tools ai query_elements",
        "/tools prompts",
        "/tools search save",
    ),
)
async def _tools(console: ConsoleScreen, args: str) -> None:
    from ifc_console.tui.tool_catalog import render_catalog

    console.print(await render_catalog(console.core, REGISTRY, args))


@command(
    "file",
    "/file [path|filter]",
    "open a model: no argument picks from this folder, a path opens it",
    "files",
    examples=("/file", "/file tower", "/file C:/models/tower.ifc"),
)
async def _file(console: ConsoleScreen, args: str) -> None:
    args = _strip_quotes(args)
    if not args:
        await console.open_file_picker()
        return
    candidate = Path(args).expanduser()
    if candidate.suffix.lower() in _IFC_SUFFIXES or candidate.exists():
        await _open_path(console, candidate)
        return
    await console.open_file_picker(initial_filter=args)


@command(
    "workspace",
    "/workspace [dir]",
    "browse a folder and pick several files (dir sets the root)",
    "files",
    examples=("/workspace", "/workspace ./project"),
)
async def _workspace(console: ConsoleScreen, args: str) -> None:
    core = console.core
    if not core.settings.workspace.enabled:
        console.print(
            "[red]workspace indexing is disabled[/red]; /settings "
            "workspace.enabled true turns it on"
        )
        return
    previous_root = core.workspace.primary_root
    if args:
        root = Path(_strip_quotes(args)).expanduser().resolve()
        if not root.is_dir():
            console.print(f"[red]{escape(str(root))} is not a directory[/red]")
            return
        core.add_allowed_dir(root)
        core.workspace.primary_root = root
        console.print(
            f"workspace root: {escape(str(root))} [dim](added to the allowed "
            "directories; the AI can read it, not widen it)[/dim]"
        )
    else:
        core.workspace.primary_root = None
    applied = await console.open_workspace_panel()
    if not applied:
        # a cancelled panel must not leave find_files scoped to another root
        core.workspace.primary_root = previous_root


@command(
    "models",
    "/models",
    "list loaded models and attached files",
    "models",
)
async def _models(console: ConsoleScreen, _args: str) -> None:
    core = console.core
    rows = core.models.model_rows()
    if not rows and not core.models.attachments:
        console.print("no model loaded; /file to pick one, /workspace to browse a folder")
        return
    lines = ["[b]models[/b]"]
    for row in rows:
        marker = "[green]active[/green]" if row["active"] else "[dim]read-only[/dim]"
        dirty = " [red]* unsaved[/red]" if row["dirty"] else ""
        size_mb = (row["size_bytes"] or 0) / 1_048_576
        lines.append(
            f"  [cyan]{row['model_id']:<16}[/cyan] {escape(str(row['name']))}  "
            f"[dim]({row['schema']}, {size_mb:.1f} MB)[/dim]  {marker}{dirty}"
        )
    if core.models.attachments:
        lines.append("[b]attached files[/b]")
        for attachment in core.models.attachments.values():
            used = attachment.consumed_by or "no tool reads this kind yet"
            lines.append(
                f"  [cyan]{attachment.alias:<16}[/cyan] {escape(attachment.path.name)}  "
                f"[dim]({attachment.kind}, {used})[/dim]"
            )
    lines.append(
        f"[dim]resident {len(core.models.sessions)}/{core.models.max_resident} · "
        "/use <id> switches the active model · /detach <id> frees one[/dim]"
    )
    console.print("\n".join(lines))


@command(
    "attach",
    "/attach <path>",
    "load a file alongside the active model",
    "models",
    examples=("/attach structural.ifc", "/attach requirements.ids"),
)
async def _attach(console: ConsoleScreen, args: str) -> None:
    if not args:
        await console.open_workspace_panel()
        return
    await _attach_path(console, Path(_strip_quotes(args)))


@command("detach", "/detach <id>", "release an attached model or file", "models")
async def _detach(console: ConsoleScreen, args: str) -> None:
    core = console.core
    key = _strip_quotes(args)
    if not key:
        console.print("[red]usage: /detach <model_id|alias>[/red]; /models lists them")
        return
    try:
        if key in core.models.attachments:
            core.detach_file(key)
        else:
            await core.detach_model(key)
    except ToolError as exc:
        console.print(f"[red]{escape(exc.message)}[/red] [dim]{escape(_console_hint(exc))}[/dim]")
    except Exception as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")


@command(
    "use",
    "/use <id>",
    "make a loaded model the active one",
    "models",
    examples=("/use structural",),
)
async def _use(console: ConsoleScreen, args: str) -> None:
    core = console.core
    key = _strip_quotes(args)
    if not key:
        console.print("[red]usage: /use <model_id>[/red]; /models lists them")
        return
    try:
        already_active = core.models.active_id == key
        await core.set_active_model(key)
        if already_active:
            console.print(f"[dim]{escape(key)} is already the active model[/dim]")
    except ToolError as exc:
        console.print(f"[red]{escape(exc.message)}[/red] [dim]{escape(_console_hint(exc))}[/dim]")
    except Exception as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")


@command("recent", "/recent", "list recently opened models", "files")
async def _recent(console: ConsoleScreen, _args: str) -> None:
    entries = console.core.recents.entries()
    if not entries:
        console.print("[dim](no recent models)[/dim]")
        return
    lines = ["[b]recent models[/b] (open with /open <path> or /file)"]
    for entry in entries[:10]:
        size_mb = entry.get("size_bytes", 0) / 1_048_576
        lines.append(
            f"  {escape(entry['path'])}  [dim]({size_mb:.1f} MB, {entry.get('schema', '?')})[/dim]"
        )
    console.print("\n".join(lines))


@command(
    "mode",
    "/mode [ask|edit]",
    "show or change what the AI may do",
    "session",
    examples=("/mode", "/mode edit"),
)
async def _mode(console: ConsoleScreen, args: str) -> None:
    core = console.core
    if not args:
        mode = core.policy.mode.value
        saving = "AI saves enabled" if core.policy.allow_ai_save else "only you can save"
        console.print(
            f"mode: [{_mode_color(mode)}]{mode}[/{_mode_color(mode)}] "
            f"(ask = AI queries only; edit = AI may change the model; {saving})"
        )
        return
    try:
        new_mode = Mode(args.strip().lower())
    except ValueError:
        console.print(f"[red]unknown mode {escape(args)!r}[/red]; use ask or edit")
        return
    if new_mode is core.policy.mode:
        console.print(f"already in {new_mode.value} mode")
        return
    if core.policy.is_escalation(new_mode):
        detail = (
            "This lets the AI change the in-memory model. Only you can save it."
            if not core.policy.allow_ai_save
            else "This lets the AI change and save the model through MCP tools."
        )
        if not await console.confirm(f"Switch to {new_mode.value.upper()} mode?", detail):
            console.print("[dim]mode unchanged[/dim]")
            return
    core.set_mode(new_mode, by="tui")


@command("theme", "/theme [dark|light|auto]", "show or switch the console theme", "console")
async def _theme(console: ConsoleScreen, args: str) -> None:
    core = console.core
    if not args:
        console.print(f"theme: {core.ui_theme} (dark, light, or auto; /theme light to switch)")
        return
    value = args.strip().lower()
    if value not in ("dark", "light", "auto"):
        console.print(f"[red]unknown theme {escape(args)!r}[/red]; use dark, light, or auto")
        return
    apply = getattr(console.app, "apply_theme", None)
    if apply is not None:
        apply(value, persist=True)
    else:
        core.set_ui_theme(value, persist=True)
    console.print(f"theme set to {value} (saved; open viewer tabs follow)")


@command(
    "viewer",
    "/viewer [off|url]",
    "open the 3D viewer (off disables, url prints the link)",
    "connect",
    examples=("/viewer", "/viewer url", "/viewer off"),
)
async def _viewer(console: ConsoleScreen, args: str) -> None:
    core = console.core
    args = args.strip().lower()
    if args == "off":
        if not core.viewer.enabled:
            console.print("viewer is already off")
            return
        core.disable_viewer()
        closed = await core.viewer_hub.close_all()
        console.print(
            f"viewer disabled ({closed} tab{'s' if closed != 1 else ''} closed); "
            "the 4 viewer tools left the MCP tool list"
        )
        console.refresh_status()
        return
    if not _require_server(console):
        return
    from ifc_console.viewer import assets

    if not assets.available():
        console.print(f"[red]{escape(assets.INSTALL_HINT)}[/red]")
        return
    newly_enabled = not core.viewer.enabled
    core.enable_viewer()
    if newly_enabled:
        console.print(
            "[dim]viewer tools joined the MCP tool list; clients that connected "
            "earlier pick them up on their next tool refresh or reconnect[/dim]"
        )
    url = core.viewer.url or core.viewer_url
    if args == "url":
        console.print(f"viewer: {url}")
        return
    console.app.copy_to_clipboard(url)
    import webbrowser

    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if opened:
        console.print(f"[green]viewer opened in your browser[/green] (URL copied): {url}")
    else:
        console.print(f"viewer URL copied: {url}")


_CHAT_PROVIDERS = ("openai", "anthropic", "openrouter", "local")


@command(
    "chat",
    "/chat [solo|off|provider]",
    "open the 3D view with the chat panel beside it (solo drops the 3D view)",
    "connect",
    examples=("/chat", "/chat solo", "/chat anthropic", "/chat off"),
)
async def _chat(console: ConsoleScreen, args: str) -> None:
    core = console.core
    arg = args.strip().lower()

    if arg == "off":
        if not core.chat.enabled:
            console.print("chat is already off")
            return
        core.disable_chat()
        console.print("chat panel disabled; any API key held for this run is gone")
        console.refresh_status()
        return
    if arg in _CHAT_PROVIDERS:
        core.chat.provider = arg
        console.print(f"chat provider set to [b]{arg}[/b] for this session")
        arg = ""
    elif arg == "split":
        arg = ""  # what the split used to be called; it is the default now
    elif arg and arg != "solo":
        console.print(
            f"[red]unknown option {escape(arg)}[/red]; use /chat, /chat solo, "
            f"/chat off, or one of: {', '.join(_CHAT_PROVIDERS)}"
        )
        return

    if not _require_server(console):
        return
    from ifc_console.viewer import assets

    if not assets.available():
        console.print(
            f"[red]{escape(assets.INSTALL_HINT)}[/red] [dim](the chat panel ships with it)[/dim]"
        )
        return

    solo = arg == "solo"
    if not solo:
        core.enable_viewer()
    newly_enabled = not core.chat.enabled
    core.enable_chat()
    url = core.chat_solo_url if solo else core.chat_url
    if newly_enabled:
        provider = core.chat.provider
        console.print(
            f"[b]chat panel on[/b] ([cyan]{provider}[/cyan]) [dim]/chat off turns it back off[/dim]\n"
            "[yellow]this is the one part of ifc-console that talks to the internet: your "
            "prompts and whatever the tools read from the model go to the provider you "
            "choose. Keys come from the environment or the panel and are never written to "
            "disk.[/yellow]"
        )
    console.app.copy_to_clipboard(url)
    import webbrowser

    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    what = "chat" if solo else "3D view with the chat beside it"
    console.print(
        f"[green]{what} opened in your browser[/green] (URL copied): {url}"
        if opened
        else f"{what}: URL copied: {url}"
    )
    console.refresh_status()


@command(
    "connect",
    "/connect [client|all]",
    "show how to connect an MCP client",
    "connect",
    examples=("/connect all", "/connect codex"),
)
async def _connect(console: ConsoleScreen, args: str) -> None:
    core = console.core
    args = args.strip().lower()
    wanted = (
        [args]
        if args and args != "all"
        else (list(_CONNECT_CLIENTS) if args == "all" else ["claude-code"])
    )
    unknown = [w for w in wanted if w not in _CONNECT_CLIENTS]
    if unknown:
        console.print(
            f"[red]unknown client {escape(unknown[0])!r}[/red]; one of: "
            f"{', '.join(_CONNECT_CLIENTS)}"
        )
        return
    snippets: dict[str, str] = {}
    for client in wanted:
        snippet = snippets[client] = _client_config(core, client)
        console.print(
            f"[b]{client} (shared console via stdio bridge)[/b]\n"
            f"[dim]{escape(_CONNECT_TARGETS[client])}[/dim]\n{escape(snippet)}"
        )
    if len(wanted) == 1:
        client = wanted[0]
        console.app.copy_to_clipboard(snippets[client])
        console.print(
            f"[green]{client} setup copied to clipboard[/green] "
            f"([dim]/copy {client} copies it again[/dim])"
        )
    else:
        console.print(
            "[dim]copy one complete setup with /copy <client>, for example /copy codex[/dim]"
        )
    console.print(
        "[dim]this setup launches a small stdio bridge, so the client may start "
        "before ifc-console does: it connects on its own once the console is "
        "up, with no client restart.[/dim]"
    )
    console.print(
        "[dim]model paths are intentionally omitted. Start ifc-console and use "
        "/file to open or switch models without changing the client setup.[/dim]"
    )
    console.print(
        "[dim]Merge the ifc-console entry with any existing config; do not replace "
        "unrelated server entries.[/dim]"
    )
    if not core.settings.server.persistent_token:
        if core.settings.server.token_in_config_snippets:
            console.print(
                "[yellow]server.persistent_token is off, so this setup includes the "
                "current run's token and must be copied again after every restart. "
                "Turn persistence on for a one-time setup.[/yellow]"
            )
        else:
            console.print(
                "[yellow]replace <TOKEN> with the current run's token. This setup "
                "must be refreshed after every restart.[/yellow]"
            )


@command(
    "copy",
    "/copy [client|url|viewer|token]",
    "copy a complete client setup, URL, or token",
    "connect",
    examples=("/copy codex", "/copy viewer"),
)
async def _copy(console: ConsoleScreen, args: str) -> None:
    core = console.core
    what = args.strip().lower() or "cmd"
    copied_client: str | None = None
    if what == "url":
        value, label = core.mcp_url, "MCP URL"
    elif what == "viewer":
        value, label = core.viewer.url or core.viewer_url, "viewer URL"
    elif what == "token":
        value, label = core.token, "bearer token"
    elif what == "cmd" or what in _CONNECT_CLIENTS:
        copied_client = "claude-code" if what == "cmd" else what
        value = _client_config(core, copied_client)
        label = f"{copied_client} setup"
    else:
        valid = ", ".join((*_CONNECT_CLIENTS, "url", "viewer", "token"))
        console.print(f"[red]unknown target {escape(what)!r}[/red]; use one of: {valid}")
        return
    console.app.copy_to_clipboard(value)
    console.print(f"{label} copied to clipboard")
    if copied_client and "<TOKEN>" in value:
        console.print(
            "[yellow]the copied setup contains <TOKEN>; replace it manually or "
            "use /copy token[/yellow]"
        )
    elif copied_client and not core.settings.server.persistent_token:
        console.print(
            "[yellow]server.persistent_token is off: this setup embeds the current "
            "run's token and stops working when this console exits[/yellow]"
        )


@command("status", "/status", "session summary: model, mode, server, viewer", "session")
async def _status(console: ConsoleScreen, _args: str) -> None:
    core = console.core
    s = core.session
    lines = ["[b]session[/b]"]
    if s.loaded:
        lines.append(
            f"  model    {escape(s.name or '')}  ({s.schema}, {s.size_bytes / 1_048_576:.1f} MB)"
        )
        lines.append(f"  path     {escape(str(s.path))}")
        lines.append(f"  dirty    {'yes' if s.dirty else 'no'}    fingerprint {s.fingerprint}")
    else:
        lines.append("  model    (none; /file to pick one)")
    mode = core.policy.mode.value
    lines.append(f"  mode     [{_mode_color(mode)}]{mode}[/{_mode_color(mode)}]")
    if core.server_running:
        lines.append(f"  server   {core.mcp_url}")
    elif core.server_error:
        lines.append(f"  server   [red]not running[/red]: {escape(core.server_error)}")
    else:
        lines.append("  server   (starting)")
    if core.viewer.enabled:
        lines.append(f"  viewer   {core.viewer.connected} tab(s)  {core.viewer.url}")
    else:
        lines.append("  viewer   off (/viewer to start)")
    if core.chat.enabled:
        model = core.chat.model or "no model chosen"
        lines.append(f"  chat     on  {core.chat.provider} · {model}")
    else:
        lines.append("  chat     off (/chat to start)")
    sandbox = core.sandbox.status()
    where = "sandboxed" if sandbox["would_sandbox"] else "in-process"
    lines.append(f"  sandbox  {sandbox['mode']}; next code run {where} (/sandbox)")
    console.print("\n".join(lines))


_SANDBOX_MODES = {
    "auto": "sandbox read-only code; fall back to in-process guards when it cannot",
    "strict": "refuse a read-only run that cannot be sandboxed",
    "off": "never sandbox; in-process guards only",
}


@command(
    "sandbox",
    "/sandbox [auto|strict|off|restart]",
    "where AI-generated code runs, and what it is allowed to do",
    "session",
    examples=("/sandbox", "/sandbox strict"),
)
async def _sandbox(console: ConsoleScreen, args: str) -> None:
    core = console.core
    arg = args.strip().lower()

    if arg == "restart":
        await core.sandbox.aclose()
        console.print("sandbox worker stopped; the next code run starts a fresh one")
        return
    if arg in _SANDBOX_MODES:
        core.store.set_user("sandbox.mode", arg)
        await core.sandbox.aclose()
        console.print(f"sandbox mode [b]{arg}[/b] - {_SANDBOX_MODES[arg]} [dim](saved)[/dim]")
        return
    if arg:
        console.print(
            f"[red]unknown option {escape(arg)}[/red]; use /sandbox [auto|strict|off|restart]"
        )
        return

    info = core.sandbox.status()
    lines = ["[b]sandbox[/b]"]
    lines.append(f"  mode     {info['mode']} - {_SANDBOX_MODES[info['mode']]}")
    if info["running"]:
        lines.append(f"  worker   running (pid {info['pid']})")
        controls = ", ".join(info["controls"]) or "none"
        lines.append(f"  controls {escape(controls)}")
        if info["limits"]:
            lines.append(f"  limits   {escape(', '.join(info['limits']))}")
    else:
        lines.append("  worker   not running (starts on the next code run)")
    lines.append(f"  memory   {info['memory_mb']} MB cap")
    if info["would_sandbox"]:
        lines.append("  next run [green]sandboxed[/green]")
    else:
        reason = info["reason"] or "not applicable"
        colour = "red" if info["mode"] == "strict" else "yellow"
        lines.append(f"  next run [{colour}]in-process[/{colour}] - {escape(reason)}")
    if info["last_error"]:
        lines.append(f"  [red]last error {escape(info['last_error'])}[/red]")
    lines.append(
        "[dim]  mutating code always runs in-process; the sandbox holds a read-only copy[/dim]"
    )
    console.print("\n".join(lines))


@command("info", "/info", "entity counts for the active model", "models")
async def _model(console: ConsoleScreen, _args: str) -> None:
    core = console.core
    if not core.session.loaded:
        console.print("no model loaded; /file to pick one")
        return

    def job() -> list[tuple[str, int]]:
        ifc = core.session.ifc
        rows = []
        for cls in (
            "IfcProject",
            "IfcSite",
            "IfcBuilding",
            "IfcBuildingStorey",
            "IfcSpace",
            "IfcWall",
            "IfcSlab",
            "IfcDoor",
            "IfcWindow",
            "IfcColumn",
            "IfcBeam",
        ):
            n = len(ifc.by_type(cls))
            if n:
                rows.append((cls, n))
        rows.append(("IfcProduct (total)", len(ifc.by_type("IfcProduct"))))
        return rows

    console.print("[dim]counting entities…[/dim]")
    try:
        rows = await core.session.run(job, timeout=60)
    except Exception as exc:
        console.print(f"[red]could not read model: {escape(str(exc))}[/red]")
        return
    width = max(len(name) for name, _ in rows)
    lines = [f"[b]{escape(core.session.name or '')}[/b]"]
    lines += [f"  {name:<{width}}  {count}" for name, count in rows]
    console.print("\n".join(lines))


@command(
    "save",
    "/save [path]",
    "save the model (path = save-as)",
    "files",
    examples=("/save", "/save reviewed.ifc"),
)
async def _save(console: ConsoleScreen, args: str) -> None:
    core = console.core
    if not core.session.loaded:
        console.print("no model loaded")
        return
    # the mode gates the AI, not you: /save works in ask mode too
    target = Path(_strip_quotes(args)).expanduser().resolve() if args else core.session.path
    assert target is not None
    in_place = target == core.session.path
    if (
        not in_place
        and target.exists()
        and not await console.confirm(
            f"{target.name} exists. Overwrite it?", "A timestamped backup is made first."
        )
    ):
        return
    console.print(f"[dim]saving {escape(target.name)}…[/dim]")
    try:
        result = await core.session.save(target, core.backups)
    except Exception as exc:
        console.print(f"[red]save failed: {escape(str(exc))}[/red]")
        return
    core.recents.touch(
        Path(result["path"]),
        size_bytes=result["size_bytes"],
        schema=core.session.schema or "?",
        mode=core.policy.mode.value,
    )
    core.audit.record("save", **result)
    core.events.emit("model_saved", **result)
    backup = f" (backup: {escape(str(result['backup_path']))})" if result.get("backup_path") else ""
    console.print(f"[green]saved[/green] {escape(str(result['path']))}{backup}")


@command(
    "reload",
    "/reload",
    "reload the model from disk (discards unsaved changes)",
    "files",
)
async def _reload(console: ConsoleScreen, _args: str) -> None:
    core = console.core
    if not core.session.loaded and not core.session.poisoned:
        console.print("no model loaded")
        return
    if core.session.dirty and not await console.confirm("Reload and discard unsaved changes?"):
        return
    console.print(f"[dim]reloading {escape(core.session.name or 'model')} from disk…[/dim]")
    try:
        if core.session.poisoned:
            await core.session.recover()
        else:
            await core.session.reload()
    except Exception as exc:
        console.print(f"[red]reload failed: {escape(str(exc))}[/red]")
        return
    core.events.emit(
        "model_loaded",
        path=str(core.session.path),
        name=core.session.name,
        schema=core.session.schema,
        fingerprint=core.session.fingerprint,
        size_bytes=core.session.size_bytes,
    )


@command("port", "/port <number>", "move the MCP server to another port", "connect")
async def _port(console: ConsoleScreen, args: str) -> None:
    try:
        port = int(args)
        if not 1024 <= port <= 65535:
            raise ValueError
    except ValueError:
        console.print("[red]usage: /port <1024-65535>[/red]")
        return
    if port == console.core.port and console.core.server_running:
        console.print(f"server already on port {port}")
        return
    await console.app.restart_server(port)


@command("audit", "/audit [n]", "show the last n audit records (default 10)", "session")
async def _audit(console: ConsoleScreen, args: str) -> None:
    try:
        count = max(1, min(int(args), 100)) if args else 10
    except ValueError:
        console.print("[red]usage: /audit [count][/red]")
        return
    records = console.core.audit.tail(count)
    if not records:
        console.print("[dim](no audit records yet)[/dim]")
        return
    lines = [f"[b]audit (last {len(records)})[/b]"]
    for record in records:
        ts = str(record.get("ts", ""))[11:19]
        kind = record.get("ev", "?")
        rest = {k: v for k, v in record.items() if k not in ("ts", "ev")}
        lines.append(
            f"  {ts}  [cyan]{kind}[/cyan]  [dim]{escape(json.dumps(rest, default=str)[:120])}[/dim]"
        )
    console.print("\n".join(lines))


@command(
    "settings",
    "/settings [key value]",
    "list settings, or set a user setting",
    "session",
    examples=("/settings", "/settings workspace.enabled true"),
)
async def _settings(console: ConsoleScreen, args: str) -> None:
    store = console.core.store
    if not args:
        flat = store.flat()
        width = max(len(k) for k in flat)
        lines = ["[b]settings[/b]  (value  \\[source]; /settings <key> <value> to change)"]
        for key, value in sorted(flat.items()):
            source = store.provenance.get(key, "default")
            lines.append(
                f"  {key:<{width}}  {json.dumps(value, default=str):<14} [dim]\\[{source}][/dim]"
            )
        console.print("\n".join(lines))
        return
    key, _, value = args.partition(" ")
    if not value:
        try:
            console.print(f"{key} = {json.dumps(store.get(key), default=str)}")
        except KeyError:
            console.print(f"[red]unknown setting {escape(key)!r}[/red]")
        return
    enabling_ai_save = (
        key == "files.allow_ai_save"
        and value.strip().lower() in {"true", "1", "yes", "on"}
        and not bool(store.settings.files.allow_ai_save)
    )
    if enabling_ai_save and not await console.confirm(
        "Allow the AI to save IFC files?",
        "This lets AI tools persist in-memory changes and replace IFC files. "
        "Automatic backups still apply.",
    ):
        console.print("[dim]files.allow_ai_save remains false[/dim]")
        return
    try:
        parsed: Any = store.set_user(key, value.strip())
    except KeyError:
        console.print(f"[red]unknown setting {escape(key)!r}[/red]")
        return
    except ValidationError as exc:
        detail = exc.errors()[0].get("msg", str(exc))
        console.print(f"[red]invalid value for {escape(key)}: {escape(str(detail))}[/red]")
        return
    except Exception as exc:
        console.print(f"[red]invalid value: {escape(str(exc))}[/red]")
        return
    _apply_live_setting(console.core, key)
    console.print(
        f"{key} = {json.dumps(parsed, default=str)}  "
        f"[dim](written to {escape(str(store.user_file))})[/dim]"
    )
    if key not in _LIVE_SETTINGS:
        console.print(
            "[dim]note: mode and port need /mode or /port; anything not listed here "
            "applies on the next start[/dim]"
        )


# Settings held in a constructor-captured attribute rather than read per use.
# Writing the attribute back keeps /settings honest for the keys an LLM hint
# is most likely to name.
_LIVE_SETTINGS: dict[str, Callable[[Any, Any], None]] = {
    "workspace.max_resident": lambda core, v: setattr(core.models, "max_resident", max(1, v)),
    "workspace.max_total_mb": lambda core, v: setattr(core.models, "max_total_mb", max(0, v)),
    "workspace.scan_cap": lambda core, v: setattr(core.workspace, "cap", v),
    "workspace.scan_depth": lambda core, v: setattr(core.workspace, "depth", v),
    "exec.allow_system_access": lambda core, v: setattr(core.policy, "allow_system_access", v),
    "files.allow_ai_save": lambda core, v: setattr(core.policy, "allow_ai_save", v),
}


def _apply_live_setting(core, key: str) -> None:
    apply = _LIVE_SETTINGS.get(key)
    if apply is None:
        return
    with contextlib.suppress(Exception):
        apply(core, core.store.get(key))


@command(
    "kb",
    "/kb [query]",
    "search the offline IFC reference (no query shows the index status)",
    "session",
    examples=("/kb fire rating", "/kb assign material", "/kb Pset_WallCommon"),
)
async def _kb(console: ConsoleScreen, args: str) -> None:
    core = console.core
    query = _strip_quotes(args)
    if not query:
        stats = core.knowledge.stats()
        if not stats["ready"]:
            state = "building now" if stats.get("building") else "not built"
            console.print(
                f"knowledge index: {state} [dim]({escape(str(core.knowledge.path))})[/dim]\n"
                "[dim]it builds itself in the background; /kb <query> once it is ready[/dim]"
            )
            core.start_knowledge()
            return
        counts = ", ".join(f"{k} {v}" for k, v in sorted(stats["counts"].items()))
        console.print(
            f"[b]knowledge index[/b]\n  records  {stats['total']} ({counts})\n"
            f"  search   {stats['search']}   ifcopenshell {stats.get('ifcopenshell', '?')}"
        )
        return
    if not core.knowledge.ready:
        console.print("[dim]the knowledge index is still building; try again shortly[/dim]")
        core.start_knowledge()
        return
    schema = core.session.schema if core.session.loaded else None
    hits = await asyncio.to_thread(core.knowledge.search, query, schema=None, limit=8)
    if not hits:
        console.print(f"[dim]nothing found for {escape(query)}[/dim]")
        return
    lines = [f"[b]{len(hits)} result(s)[/b] [dim]for {escape(query)}[/dim]"]
    for hit in hits:
        where = f" [dim]{hit['schema']}[/dim]" if hit.get("schema") else ""
        lines.append(f"  [cyan]{hit['kind']:8}[/cyan] {escape(hit['name'])}{where}")
        lines.append(f"      [dim]{escape(hit['summary'][:110])}[/dim]")
    if schema:
        lines.append(f"[dim]the loaded model is {escape(schema)}[/dim]")
    console.print("\n".join(lines))


@command(
    "extensions",
    "/extensions [query]",
    "browse the extension store (agents installed via `ifc-console extensions install`)",
    "console",
    examples=("/extensions", "/extensions measure"),
)
async def _extensions(console: ConsoleScreen, args: str) -> None:
    from ifc_console import extensions as ext

    query = _strip_quotes(args)
    if query:
        hits, source = await asyncio.to_thread(ext.search, query)
    else:
        catalog, source = await asyncio.to_thread(ext.fetch_catalog)
        hits = catalog.extensions
    installed = ext.InstallRecord(console.core.store.home).load()
    lines = [f"[b]extension store[/b] [dim]({escape(source)})[/dim]"]
    if not hits:
        lines.append(f"  [dim]nothing found for {escape(query)}[/dim]")
    for entry in hits:
        mark = "[green]installed[/green]" if entry.name in installed else f"[dim]{entry.kind}[/dim]"
        lines.append(f"  [cyan]{escape(entry.name):16}[/cyan] {mark} {escape(entry.description)}")
        if entry.command:
            lines.append(f"      [dim]run: {escape(entry.command)}[/dim]")
    lines.append(
        "[dim]install with: ifc-console extensions install <name> (each agent gets "
        "its own environment)[/dim]"
    )
    console.print("\n".join(lines))


@command("clear", "/clear", "clear the log", "console")
async def _clear(console: ConsoleScreen, _args: str) -> None:
    console.clear_log()


@command("quit", "/quit", "quit ifc-console (asks about unsaved changes)", "console")
async def _quit(console: ConsoleScreen, _args: str) -> None:
    console.app.action_request_quit()
