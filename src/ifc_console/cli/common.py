"""Helpers shared by every command group."""

from __future__ import annotations

import argparse
import logging
import platform
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ifc_console import __version__
from ifc_console.policy.modes import Mode

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.settings import SettingsStore


def _new_store(**kwargs) -> SettingsStore:
    # Deferred import: pydantic costs ~0.3 s and --help must not pay it.
    from ifc_console.settings import SettingsStore

    return SettingsStore(**kwargs)


_MODES = [m.value for m in Mode]


class _VersionAction(argparse.Action):
    def __call__(self, parser, namespace, values, _option_string=None):
        print(_version_line())
        parser.exit()


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("port must be an integer") from None
    if not 1024 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1024 and 65535")
    return port


def _add_run_flags(parser: argparse.ArgumentParser, *, suppress_defaults: bool = False) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    flag_default = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument("--file", default=default, help="IFC file to load at startup.")
    parser.add_argument("--mode", choices=_MODES, default=default, help="Session mode.")
    parser.add_argument("--port", type=_port, default=default, help="MCP HTTP port (default 8383).")
    parser.add_argument(
        "--viewer",
        action="store_true",
        default=flag_default,
        help="Enable the local 3D web viewer (needs the HTTP server: TUI or --http).",
    )
    parser.add_argument(
        "--agent",
        action="store_true",
        default=flag_default,
        help="Enable the browser Agent workspace.",
    )
    # One-release command-line compatibility. The product surface and help use
    # --agent; /chat is intentionally not a console command anymore.
    parser.add_argument(
        "--chat",
        dest="agent",
        action="store_true",
        default=flag_default,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--allow-dir",
        action="append",
        default=default,
        metavar="PATH",
        help="Extra directory the LLM may open models in, and save in when AI saving is enabled.",
    )
    parser.add_argument(
        "--log-level",
        choices=["debug", "info", "warning", "error"],
        default=default,
    )


def _version_line() -> str:
    # metadata only: importing ifcopenshell itself costs seconds per launch
    try:
        from importlib.metadata import version

        ios = version("ifcopenshell")
    except Exception:
        ios = "missing"
    return f"ifc-console {__version__} (ifcopenshell {ios}, python {platform.python_version()})"


def _agent_module(name: str):
    """Import one agent module on demand, so normal CLI startup never pays for it."""
    import importlib

    return importlib.import_module(f"ifc_console.agents.{name}")


def _make_store(args: argparse.Namespace, *, include_project: bool = True) -> SettingsStore:
    overrides: dict[str, Any] = {}
    if getattr(args, "port", None) is not None:
        overrides["server.port"] = args.port
    if getattr(args, "mode", None):
        overrides["mode.default"] = args.mode
    if getattr(args, "log_level", None):
        overrides["logging.level"] = args.log_level
    return _new_store(flag_overrides=overrides, include_project=include_project)


def _make_core(args: argparse.Namespace, store: SettingsStore, transport: str) -> AppCore:
    extra = tuple(Path(d) for d in (getattr(args, "allow_dir", None) or []))
    mode = Mode(args.mode) if getattr(args, "mode", None) else None
    # --viewer forces the viewer on; without the flag the settings default rules.
    viewer_flag = True if getattr(args, "viewer", False) else None
    if transport == "stdio":
        if viewer_flag:
            print(
                "note: the web viewer needs the HTTP server; it is unavailable under "
                "`serve --stdio`. Run `ifc-console` (console) or `ifc-console --no-tui` instead.",
                file=sys.stderr,
            )
        viewer_flag = False
    # The Agent workspace attaches to the same viewer component and HTTP server.
    chat_flag = True if getattr(args, "agent", False) else None
    if transport == "stdio":
        chat_flag = False
    from ifc_console.app import AppCore

    return AppCore(
        store,
        mode=mode,
        port=getattr(args, "port", None),
        extra_allowed_dirs=extra,
        transport=transport,
        viewer=viewer_flag,
        chat=chat_flag,
    )


def _ensure_home(store: SettingsStore) -> None:
    """Fail with a friendly message when the state directory is unwritable."""
    try:
        store.ensure_dirs()
    except OSError as exc:
        print(
            f"error: cannot create the ifc-console home directory at {store.home}: {exc}",
            file=sys.stderr,
        )
        print(
            "hint: set IFC_CONSOLE_HOME to a writable location.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc


def _setup_logging(store: SettingsStore, *, level: str, to_file: bool = True) -> None:
    _ensure_home(store)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file and store.settings.logging.file_enabled:
        handlers.append(
            RotatingFileHandler(
                store.logs_dir / "ifc-console.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="[ifc-console] %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
