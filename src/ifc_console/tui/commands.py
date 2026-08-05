"""Slash-command registry for the console TUI.

Each command is a small async handler that receives the console screen and
the raw argument string. Keeping them in a registry (instead of an if-chain)
gives /help, completion, and the docs one source of truth.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.markup import escape

from ifc_console.mcp.envelope import ToolError
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


REGISTRY: dict[str, Command] = {}
ALIASES = {"exit": "quit"}

# Display order for /help.
_GROUPS = ("model", "session", "server & clients", "console")


def command(name: str, usage: str, help_text: str, group: str) -> Callable[[Handler], Handler]:
    def wrap(fn: Handler) -> Handler:
        REGISTRY[name] = Command(name=name, usage=usage, help=help_text, group=group, handler=fn)
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
            else (
                core.token
                if core.settings.server.token_in_config_snippets
                else "<TOKEN>"
            )
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
    name = ALIASES.get(name.lower(), name.lower())
    cmd = REGISTRY.get(name)
    if cmd is None:
        matches = [c for c in REGISTRY if c.startswith(name)]
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
async def _open_path(console: ConsoleScreen, path: Path) -> bool:
    core = console.core
    path = path.expanduser().resolve()
    if not path.exists():
        console.print(f"[red]{escape(str(path))} does not exist[/red]")
        return False
    discard_dirty = core.session.dirty
    if discard_dirty and not await console.confirm("Discard unsaved changes and open another model?"):
        return False
    core.add_allowed_dir(path.parent)
    try:
        # progress and the loaded line arrive via model_loading/model_loaded events
        await core.open_model(path, discard_dirty=discard_dirty)
    except ToolError as exc:
        console.print(
            f"[red]could not load {escape(path.name)}: {escape(exc.message)}[/red] "
            f"[dim]{escape(exc.hint)}[/dim]"
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
            f"[dim]{escape(exc.hint)}[/dim]"
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
            "[dim]attached files stay read-only; the LLM sees them through "
            "list_models[/dim]"
        )


def _mode_color(mode: str) -> str:
    return {"ask": "green", "edit": "red"}.get(mode, "white")


# -------------------------------------------------------------------- commands
@command("help", "/help", "list all commands", "console")
async def _help(console: ConsoleScreen, _args: str) -> None:
    width = max(len(c.usage) for c in REGISTRY.values())
    lines = ["[b]commands[/b]"]
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


@command("file", "/file [filter]", "pick an IFC file from this folder and recents", "model")
async def _file(console: ConsoleScreen, args: str) -> None:
    await console.open_file_picker(initial_filter=args)


@command("open", "/open <path>", "open an IFC file by path", "model")
async def _open(console: ConsoleScreen, args: str) -> None:
    if not args:
        await console.open_file_picker()
        return
    await _open_path(console, Path(_strip_quotes(args)))


@command(
    "workspace",
    "/workspace [dir]",
    "browse a folder and pick several files (dir sets the root)",
    "model",
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


@command("models", "/models", "list loaded models and attached files", "model")
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


@command("attach", "/attach <path>", "load a file alongside the active model", "model")
async def _attach(console: ConsoleScreen, args: str) -> None:
    if not args:
        await console.open_workspace_panel()
        return
    await _attach_path(console, Path(_strip_quotes(args)))


@command("detach", "/detach <id>", "release an attached model or file", "model")
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
        console.print(f"[red]{escape(exc.message)}[/red] [dim]{escape(exc.hint)}[/dim]")
    except Exception as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")


@command("use", "/use <id>", "make a loaded model the active one", "model")
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
        console.print(f"[red]{escape(exc.message)}[/red] [dim]{escape(exc.hint)}[/dim]")
    except Exception as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")


@command("recent", "/recent", "list recently opened models", "model")
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


@command("mode", "/mode [ask|edit]", "show or change what the AI may do", "session")
async def _mode(console: ConsoleScreen, args: str) -> None:
    core = console.core
    if not args:
        mode = core.policy.mode.value
        console.print(
            f"mode: [{_mode_color(mode)}]{mode}[/{_mode_color(mode)}] "
            "(ask = AI queries only; edit = AI may change the model; /mode edit to switch)"
        )
        return
    try:
        new_mode = Mode(args)
    except ValueError:
        console.print(f"[red]unknown mode {escape(args)!r}[/red]; use ask or edit")
        return
    if new_mode is core.policy.mode:
        console.print(f"already in {new_mode.value} mode")
        return
    if core.policy.is_escalation(new_mode):
        detail = "This lets the AI change and save the model through MCP tools."
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
    "server & clients",
)
async def _viewer(console: ConsoleScreen, args: str) -> None:
    core = console.core
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
    if not core.server_running:
        console.print("[red]server is not running[/red]")
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


@command(
    "connect", "/connect [client|all]", "show how to connect an MCP client", "server & clients"
)
async def _connect(console: ConsoleScreen, args: str) -> None:
    core = console.core
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
    if not core.settings.server.persistent_token:
        console.print(
            "[yellow]server.persistent_token is off: these setups embed the "
            "current run's token and stop working when this console exits[/yellow]"
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
    "server & clients",
)
async def _copy(console: ConsoleScreen, args: str) -> None:
    core = console.core
    what = args or "cmd"
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
    lines.append(
        f"  server   {core.mcp_url}" if core.server_running else "  server   (not running)"
    )
    if core.viewer.enabled:
        lines.append(f"  viewer   {core.viewer.connected} tab(s)  {core.viewer.url}")
    else:
        lines.append("  viewer   off (/viewer to start)")
    console.print("\n".join(lines))


@command("model", "/model", "entity counts for the loaded model", "model")
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


@command("save", "/save [path]", "save the model (path = save-as)", "model")
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


@command("reload", "/reload", "reload the model from disk (discards unsaved changes)", "model")
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


@command("port", "/port <number>", "move the MCP server to another port", "server & clients")
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


@command("settings", "/settings [key value]", "list settings, or set a user setting", "session")
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
    try:
        parsed: Any = store.set_user(key, value.strip())
    except KeyError:
        console.print(f"[red]unknown setting {escape(key)!r}[/red]")
        return
    except Exception as exc:
        console.print(f"[red]invalid value: {escape(str(exc))}[/red]")
        return
    console.print(
        f"{key} = {json.dumps(parsed, default=str)}  "
        f"[dim](written to {escape(str(store.user_file))})[/dim]"
    )
    console.print("[dim]note: mode/port changes need /mode or /port to affect this session[/dim]")


@command("clear", "/clear", "clear the log", "console")
async def _clear(console: ConsoleScreen, _args: str) -> None:
    console.clear_log()


@command("quit", "/quit", "quit ifc-console (asks about unsaved changes)", "console")
async def _quit(console: ConsoleScreen, _args: str) -> None:
    console.app.action_request_quit()
