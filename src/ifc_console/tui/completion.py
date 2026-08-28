"""Inline completion for the console prompt.

Pure logic, no Textual imports: given the current input line, produce a
MenuState (the prefix to keep plus the candidates that can replace what
follows it). The console renders the state in an option list docked above
the prompt and applies the picked candidate back into the input, so all
choices happen in the main window instead of a modal.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ifc_console.tui import commands

if TYPE_CHECKING:
    from ifc_console.app import AppCore

# Lazily invoked so the filesystem is only scanned in /open context;
# returns (path, detail) pairs as produced by launcher.discover_ifc_files.
FilesProvider = Callable[[], Sequence[tuple[Path, str]]]


@dataclass(frozen=True)
class Candidate:
    """One row in the completion menu."""

    insert: str  # replaces everything after MenuState.prefix
    annotation: str = ""  # dim explanation shown next to the value
    display: str = ""  # what to show when it is not the insert text itself
    terminal: bool = False  # picking this yields a complete, runnable line
    advance: bool = False  # append a space so the next argument can complete
    disabled: bool = False  # informational row; cannot be picked

    @property
    def shown(self) -> str:
        return self.display or self.insert


@dataclass(frozen=True)
class MenuState:
    """What the completion menu should offer for one input line."""

    prefix: str = ""
    candidates: tuple[Candidate, ...] = ()
    context: str = ""  # provider key; the console scopes caches by it

    @property
    def empty(self) -> bool:
        return not self.candidates

    def apply(self, candidate: Candidate) -> str:
        value = self.prefix + candidate.insert
        return value + " " if candidate.advance else value


_EMPTY = MenuState()


def complete(line: str, core: AppCore, files: FilesProvider | None = None) -> MenuState:
    """Menu state for the given prompt line (cursor assumed at the end)."""
    if not line.startswith("/"):
        return _EMPTY
    name, sep, rest = line[1:].partition(" ")
    if not sep:
        return _command_menu(name)
    cmd = _resolve(name)
    if cmd is None:
        return _EMPTY
    provider = _ARG_PROVIDERS.get(cmd.name)
    if provider is None:
        return _EMPTY
    return provider(core, rest, files)


def _resolve(name: str) -> commands.Command | None:
    """Same lookup dispatch() uses: alias, exact name, unique prefix."""
    name = commands.ALIASES.get(name.lower(), name.lower())
    cmd = commands.REGISTRY.get(name)
    if cmd is not None:
        return cmd
    matches = commands.resolve_prefix(name)
    return commands.REGISTRY[matches.pop()] if len(matches) == 1 else None


# Commands that do something useful with no argument, so Enter runs them even
# though Tab can still complete an argument. /file opens the file picker.
_RUN_ON_ENTER = frozenset({"agent", "file", "tools"})


# ------------------------------------------------------------- command names
def _command_menu(token: str) -> MenuState:
    token = token.lower()
    matches = [n for n in sorted(commands.REGISTRY) if n.startswith(token)]
    # an exactly-typed name outranks the longer commands it prefixes
    matches.sort(key=lambda n: n != token)
    out = []
    for name in matches:
        cmd = commands.REGISTRY[name]
        # a command whose argument can be completed (or is required) is not
        # done yet: picking it advances to the argument instead of running
        has_more = (name in _ARG_PROVIDERS and name not in _RUN_ON_ENTER) or "<" in cmd.usage
        out.append(
            Candidate(
                insert=f"/{name}",
                annotation=cmd.help,
                terminal=not has_more,
                advance=has_more,
            )
        )
    return MenuState(prefix="", candidates=tuple(out), context="command")


# ---------------------------------------------------------------- arguments
def _choices(name: str, rest: str, rows: Sequence[tuple[str, str]], *, context: str) -> MenuState:
    token = rest.strip().lower()
    out = [
        Candidate(insert=value, annotation=note, terminal=True)
        for value, note in rows
        if value.startswith(token)
    ]
    return MenuState(prefix=f"/{name} ", candidates=tuple(out), context=context)


def _mode_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    current = core.policy.mode.value
    rows = [
        ("ask", "the AI can query; changing the model is blocked"),
        (
            "edit",
            (
                "the AI can change and save; backups are automatic"
                if core.policy.allow_ai_save
                else "the AI can change memory; only you can save"
            ),
        ),
    ]
    rows = [(v, note + (" · current" if v == current else "")) for v, note in rows]
    return _choices("mode", rest, rows, context="mode")


def _theme_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    current = core.ui_theme
    rows = [
        ("light", "bright neutral surfaces with blue accents"),
        ("dark", "neutral black and grey"),
        ("modern", "layered near-black with restrained indigo"),
        ("blue", "the original blue workstation theme"),
    ]
    rows = [(v, note + (" · current" if v == current else "")) for v, note in rows]
    return _choices("theme", rest, rows, context="theme")


def _viewer_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    state = _choices(
        "viewer",
        rest,
        [
            ("off", "disable the viewer and close its tabs"),
            ("url", "print the viewer link without opening a browser"),
        ],
        context="viewer",
    )
    if rest.strip():
        return state
    bare = Candidate(
        insert="", display="(open)", annotation="open the 3D viewer in your browser", terminal=True
    )
    return MenuState(
        prefix=state.prefix, candidates=(bare, *state.candidates), context=state.context
    )


def _agent_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    rows = [
        ("new", "compose a project agent from reviewed capability blocks"),
        ("list", "show built-in and project agents"),
        ("files", "refresh and list project references"),
    ]
    registry = getattr(core, "agent_packs", None)
    if registry is not None:
        rows.extend((info.name, info.description) for info in registry.active())
    return _choices("agent", rest, rows, context="agent")


def _sandbox_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    current = core.settings.sandbox.mode
    rows = [
        ("auto", "sandbox read-only code, fall back when it cannot"),
        ("strict", "refuse a read-only run that cannot be sandboxed"),
        ("off", "in-process guards only"),
        ("restart", "stop the worker; the next run starts a fresh one"),
    ]
    rows = [(v, note + (" · current" if v == current else "")) for v, note in rows]
    return _choices("sandbox", rest, rows, context="sandbox")


def _copy_args(_core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    return _choices(
        "copy",
        rest,
        [
            ("claude-code", "complete user-scoped setup · default for bare /copy"),
            ("claude-desktop", "complete HTTP bridge config"),
            ("cursor", "complete global MCP config"),
            ("vscode", "complete user-profile MCP config"),
            ("codex", "complete config.toml entry"),
            ("url", "the MCP endpoint URL"),
            ("viewer", "the 3D viewer URL"),
            ("token", "the bearer token"),
        ],
        context="copy",
    )


def _connect_args(_core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    return _choices(
        "connect",
        rest,
        [
            ("claude-code", "user-scoped HTTP command · default"),
            ("claude-desktop", "HTTP bridge config"),
            ("cursor", "global bridge config"),
            ("vscode", "user-profile bridge config"),
            ("codex", "shared bridge config.toml"),
            ("all", "show every client at once"),
        ],
        context="connect",
    )


def _open_args(core: AppCore, rest: str, files: FilesProvider | None) -> MenuState:
    entries = files() if files is not None else []
    if entries is None:
        # the scan runs on a thread; the console refreshes the menu when it lands
        scanning = Candidate(insert="", display="(scanning for IFC files…)", disabled=True)
        return MenuState(prefix="/file ", candidates=(scanning,), context="open")
    entries = list(entries)
    token = rest.strip().lower()
    root = core.launch_dir
    out = []
    for path, detail in entries:
        if token and token not in str(path).lower():
            continue
        try:
            shown = str(path.relative_to(root))
        except ValueError:
            shown = str(path)
        out.append(Candidate(insert=shown, annotation=detail, terminal=True))
    return MenuState(prefix="/file ", candidates=tuple(out), context="open")


def _port_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    if rest.strip():
        return _EMPTY
    hint = Candidate(
        insert="",
        display=f"(type a port number, 1024-65535; current {core.port})",
        disabled=True,
    )
    return MenuState(prefix="/port ", candidates=(hint,), context="port")


# Values for settings whose schema is a fixed choice (see settings.py).
_SETTING_VALUES = {
    "mode.default": ("ask", "edit"),
    "sandbox.mode": ("auto", "strict", "off"),
    "logging.level": ("debug", "info", "warning", "error"),
    "tui.theme": ("light", "dark", "modern", "blue"),
}


def _settings_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    store = core.store
    flat = store.flat()
    key, sep, value = rest.lstrip().partition(" ")
    if not sep:
        token = key.lower()
        out = [
            Candidate(
                insert=k,
                annotation=(
                    f"= {json.dumps(flat[k], default=str)} [{store.provenance.get(k, 'default')}]"
                ),
                advance=True,
            )
            for k in sorted(flat)
            if token in k.lower()
        ]
        return MenuState(prefix="/settings ", candidates=tuple(out), context="settings")
    if key not in flat:
        return _EMPTY
    current = flat[key]
    values = _SETTING_VALUES.get(key)
    if values is None and isinstance(current, bool):
        values = ("true", "false")
    prefix = f"/settings {key} "
    if values is None:
        if value.strip():
            return _EMPTY
        hint = Candidate(
            insert="",
            display=f"(type a value; current {json.dumps(current, default=str)})",
            disabled=True,
        )
        return MenuState(prefix=prefix, candidates=(hint,), context="settings")
    token = value.strip().lower()
    out = [
        Candidate(
            insert=v,
            annotation="current" if v == str(current).lower() else "",
            terminal=True,
        )
        for v in values
        if v.startswith(token)
    ]
    return MenuState(prefix=prefix, candidates=tuple(out), context="settings")


def _tools_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    from ifc_console.tui.tool_catalog import SECTION_HELP, SECTIONS

    section, separator, target = rest.lstrip().partition(" ")
    if not separator:
        token = section.lower()
        candidates = tuple(
            Candidate(
                insert=name,
                annotation=SECTION_HELP[name],
                terminal=True,
                advance=name not in {"all"},
            )
            for name in SECTIONS
            if name.startswith(token)
        )
        return MenuState(prefix="/tools ", candidates=candidates, context="tools")

    section = section.lower()
    token = target.strip().lower()
    rows: list[tuple[str, str]] = []
    if section == "slash":
        rows = [(item.name, item.help) for item in commands.REGISTRY.values()]
    elif section == "ai":
        rows = [(item.name, item.description) for item in core.operations.specs()]
    elif section == "settings":
        flat = core.store.flat()
        rows = [(key, f"current: {json.dumps(flat[key], default=str)}") for key in flat]
    elif section in {"prompts", "resources"}:
        rows = [
            (name, "registered in the live MCP catalog")
            for name in core.tool_catalog_names.get(section, ())
        ]
    elif section == "search":
        if token:
            return _EMPTY
        hint = Candidate(insert="", display="(type text to search the catalog)", disabled=True)
        return MenuState(prefix="/tools search ", candidates=(hint,), context="tools-search")
    else:
        return _EMPTY
    candidates = tuple(
        Candidate(insert=name, annotation=note, terminal=True)
        for name, note in sorted(rows)
        if token in name.lower()
    )
    return MenuState(
        prefix=f"/tools {section} ",
        candidates=candidates,
        context=f"tools-{section}",
    )


Provider = Callable[["AppCore", str, "FilesProvider | None"], MenuState]


def _loaded_args(core: AppCore, rest: str, _files: FilesProvider | None) -> MenuState:
    """Ids of what is already loaded: models first, then attached files."""
    rows: list[tuple[str, str]] = []
    for row in core.models.model_rows():
        note = "active" if row["active"] else "attached, read-only"
        rows.append((row["model_id"], f"{row['name']} ({note})"))
    for attachment in core.models.attachments.values():
        rows.append((attachment.alias, f"{attachment.path.name} ({attachment.kind})"))
    return _choices("detach", rest, rows, context="loaded")


def _use_args(core: AppCore, rest: str, files: FilesProvider | None) -> MenuState:
    state = _loaded_args(core, rest, files)
    # only models can become active; attachments are not models
    ids = {row["model_id"] for row in core.models.model_rows()}
    return MenuState(
        prefix="/use ",
        candidates=tuple(c for c in state.candidates if c.insert in ids),
        context="loaded",
    )


_ARG_PROVIDERS: dict[str, Provider] = {
    "agent": _agent_args,
    "tools": _tools_args,
    "mode": _mode_args,
    "detach": _loaded_args,
    "use": _use_args,
    "sandbox": _sandbox_args,
    "theme": _theme_args,
    "viewer": _viewer_args,
    "copy": _copy_args,
    "connect": _connect_args,
    "file": _open_args,
    "port": _port_args,
    "settings": _settings_args,
}
