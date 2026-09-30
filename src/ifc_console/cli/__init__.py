"""Command-line interface.

Exit codes: 0 ok · 1 runtime error · 2 environment problem · 3 bad usage/no
TTY · 4 file not found/unparseable · 5 check failed.

Each command group lives in its own module with its parser and handlers
together; this package only assembles them and dispatches. Nothing heavy is
imported here: handlers pull in the backend when they run.
"""

from __future__ import annotations

import argparse

from ifc_console.cli import admin, agents, automation, changes, connect, doctor, run
from ifc_console.cli.common import _add_run_flags, _VersionAction
from ifc_console.cli.connect import MCP_REMOTE_SPEC, build_config_snippet

__all__ = ["MCP_REMOTE_SPEC", "build_config_snippet", "build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc-console",
        description=(
            "A terminal interface to connect IFC files to LLMs: a standalone MCP server. "
            "With no subcommand, starts the interactive console (slash commands: "
            "/file, /mode, /viewer, /connect, /tools, /help)."
        ),
    )
    # _version_line() reads package metadata; only --version should pay for it
    parser.add_argument("--version", action=_VersionAction, nargs=0, help="Show the version.")
    _add_run_flags(parser)
    parser.add_argument(
        "--no-tui", action="store_true", help="Headless HTTP daemon instead of the TUI."
    )

    sub = parser.add_subparsers(dest="command")
    run.add_serve_parser(sub)
    run.add_bridge_parser(sub)
    connect.add_mcp_config_parser(sub)
    run.add_dev_parser(sub)
    doctor.add_doctor_parser(sub)
    automation.add_check_parser(sub)
    automation.add_run_parser(sub)
    automation.add_jobs_parser(sub)
    automation.add_batch_parser(sub)
    automation.add_workflows_parser(sub)
    changes.add_transactions_parser(sub)
    changes.add_artifacts_parser(sub)
    changes.add_changes_parser(sub)
    admin.add_settings_parser(sub)
    admin.add_recents_parser(sub)
    connect.add_token_parser(sub)
    admin.add_knowledge_parser(sub)
    agents.add_agents_parser(sub)
    admin.add_keys_parser(sub)
    admin.add_plugins_parser(sub)
    admin.add_sessions_parser(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    try:
        if func is None:
            return run._cmd_interactive(args)
        return func(args)
    except KeyboardInterrupt:
        return 1
