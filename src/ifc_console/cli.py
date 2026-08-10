"""Command-line interface.

Exit codes: 0 ok · 1 runtime error · 2 environment problem · 3 bad usage/no
TTY · 4 file not found/unparseable · 5 check failed.
"""

from __future__ import annotations

import argparse
import json
import logging
import platform
import shlex
import sys
import time
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


log = logging.getLogger("ifc-console")

_MODES = [m.value for m in Mode]


# --------------------------------------------------------------------------- parser
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc-console",
        description=(
            "A terminal interface to connect IFC files to LLMs: a standalone MCP server. "
            "With no subcommand, starts the interactive console (slash commands: "
            "/file, /mode, /viewer, /connect, /help)."
        ),
    )
    # _version_line() reads package metadata; only --version should pay for it
    parser.add_argument("--version", action=_VersionAction, nargs=0, help="Show the version.")
    _add_run_flags(parser)
    parser.add_argument(
        "--no-tui", action="store_true", help="Headless HTTP daemon instead of the TUI."
    )

    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="Run the server without the launcher UI.")
    group = serve.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true", help="stdio transport (client-managed).")
    group.add_argument("--http", action="store_true", help="HTTP daemon (same as --no-tui).")
    _add_run_flags(serve)
    serve.set_defaults(func=_cmd_serve)

    bridge = sub.add_parser(
        "bridge",
        help="stdio proxy to a running console; survives being started first.",
    )
    bridge.add_argument(
        "--port", type=_port, default=None, help="Console port (default from settings)."
    )
    bridge.add_argument("--token", default=None, help="Defaults to the persistent machine token.")
    bridge.set_defaults(func=_cmd_bridge)

    cfg = sub.add_parser("mcp-config", help="Print client wiring snippets.")
    cfg.add_argument(
        "--client",
        choices=["claude-code", "claude-desktop", "cursor", "vscode", "codex"],
        default="claude-code",
    )
    cfg.add_argument("--transport", choices=["bridge", "http", "stdio"], default=None)
    cfg.add_argument("--file", default=None, help="Model path to pin in stdio snippets.")
    cfg.add_argument("--mode", choices=_MODES, default=None)
    cfg.add_argument("--port", type=_port, default=None)
    cfg.set_defaults(func=_cmd_mcp_config)

    doctor = sub.add_parser("doctor", help="Diagnose the environment.")
    doctor.add_argument("--file", default=None, help="Also parse this IFC file.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    check = sub.add_parser("check", help="Validate a model for CI: schema plus optional IDS files.")
    check.add_argument("model", help="IFC file to check.")
    check.add_argument(
        "--ids",
        action="append",
        default=[],
        metavar="FILE",
        help="buildingSMART IDS file to check against (repeatable).",
    )
    check.add_argument(
        "--express-rules",
        action="store_true",
        help="Also run EXPRESS where-rules (slow on large models).",
    )
    check.add_argument("--max-issues", type=int, default=200)
    check.add_argument("--format", choices=["text", "json", "sarif", "junit"], default="text")
    check.add_argument("--output", default=None, metavar="FILE", help="Write the report to a file.")
    check.set_defaults(func=_cmd_check)

    workflow_run = sub.add_parser(
        "run", help="Plan or execute a versioned IFC automation workflow."
    )
    workflow_run.add_argument("manifest", help="Workflow .json, .yaml, or .yml file.")
    workflow_run.add_argument(
        "--plan", action="store_true", help="Resolve and hash inputs without scheduling work."
    )
    workflow_run.add_argument("--json", action="store_true")
    workflow_run.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export workflow and step artifacts."
    )
    workflow_run.set_defaults(func=_cmd_workflow_run)

    jobs = sub.add_parser("jobs", help="Run and inspect durable automation jobs.")
    jobs_sub = jobs.add_subparsers(dest="jobs_cmd", required=True)
    jobs_validate = jobs_sub.add_parser(
        "validate", help="Run isolated validation and persist report artifacts."
    )
    jobs_validate.add_argument("model", help="IFC model to validate.")
    jobs_validate.add_argument("--ids", action="append", default=[], metavar="FILE")
    jobs_validate.add_argument("--express-rules", action="store_true")
    jobs_validate.add_argument("--max-issues", type=int, default=200)
    jobs_validate.add_argument("--expected-revision", default=None)
    jobs_validate.add_argument("--json", action="store_true", help="Print the job record.")
    jobs_validate.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export generated artifacts."
    )
    jobs_validate.set_defaults(func=_cmd_jobs_validate)
    jobs_commit = jobs_sub.add_parser(
        "commit", help="Run a journaled commit and stream transaction phases."
    )
    jobs_commit.add_argument("model")
    jobs_commit.add_argument("change_set_id")
    jobs_commit.add_argument("--approval", required=True, dest="approval_id")
    jobs_commit.add_argument("--json", action="store_true")
    jobs_commit.set_defaults(func=_cmd_jobs_commit)
    jobs_restore = jobs_sub.add_parser(
        "restore", help="Run a journaled restore and stream transaction phases."
    )
    jobs_restore.add_argument("model")
    jobs_restore.add_argument("commit_id")
    jobs_restore.add_argument("--confirm", action="store_true")
    jobs_restore.add_argument("--json", action="store_true")
    jobs_restore.set_defaults(func=_cmd_jobs_restore)
    jobs_list = jobs_sub.add_parser("list", help="List persisted jobs.")
    jobs_list.add_argument("--limit", type=int, default=50)
    jobs_list.add_argument("--json", action="store_true")
    jobs_list.set_defaults(func=_cmd_jobs_list)
    jobs_show = jobs_sub.add_parser("show", help="Show one persisted job.")
    jobs_show.add_argument("job_id")
    jobs_show.add_argument("--json", action="store_true")
    jobs_show.set_defaults(func=_cmd_jobs_show)
    jobs_cancel = jobs_sub.add_parser("cancel", help="Request cancellation of a running job.")
    jobs_cancel.add_argument("job_id")
    jobs_cancel.add_argument("--json", action="store_true")
    jobs_cancel.set_defaults(func=_cmd_jobs_cancel)

    batch = sub.add_parser(
        "batch", help="Run resumable read-only automation across many IFC files."
    )
    batch_sub = batch.add_subparsers(dest="batch_cmd", required=True)
    batch_validate = batch_sub.add_parser(
        "validate", help="Capture and validate IFC files with bounded concurrency."
    )
    batch_validate.add_argument("models", nargs="+", help="IFC files to validate.")
    batch_validate.add_argument("--ids", action="append", default=[], metavar="FILE")
    batch_validate.add_argument("--express-rules", action="store_true")
    batch_validate.add_argument("--max-issues", type=int, default=200)
    batch_validate.add_argument("--concurrency", type=int, default=2)
    batch_validate.add_argument(
        "--failure-policy", choices=["continue", "fail_fast"], default="continue"
    )
    batch_validate.add_argument("--json", action="store_true")
    batch_validate.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export the manifest and reports."
    )
    batch_validate.set_defaults(func=_cmd_batch_validate)
    batch_query = batch_sub.add_parser(
        "query", help="Stream one selector result artifact per IFC file."
    )
    batch_query.add_argument("models", nargs="+", help="IFC files to query.")
    batch_query.add_argument("--selector", required=True, help="IfcOpenShell selector.")
    batch_query.add_argument(
        "--field",
        action="append",
        dest="fields",
        choices=["name", "predefined_type", "type_name", "storey", "description", "tag"],
        help="Result field beyond global_id and class (repeatable).",
    )
    batch_query.add_argument("--order-by", choices=["class", "name", "storey"], default="class")
    batch_query.add_argument("--format", choices=["jsonl", "csv"], default="jsonl")
    batch_query.add_argument("--limit", type=int, default=100_000, help="Per-file row cap.")
    batch_query.add_argument("--concurrency", type=int, default=2)
    batch_query.add_argument(
        "--failure-policy", choices=["continue", "fail_fast"], default="continue"
    )
    batch_query.add_argument("--json", action="store_true")
    batch_query.add_argument(
        "--output-dir", default=None, metavar="DIR", help="Export the manifest and results."
    )
    batch_query.set_defaults(func=_cmd_batch_query)
    batch_list = batch_sub.add_parser("list", help="List durable batch records.")
    batch_list.add_argument("--limit", type=int, default=50)
    batch_list.add_argument("--json", action="store_true")
    batch_list.set_defaults(func=_cmd_batch_list)
    batch_show = batch_sub.add_parser("show", help="Show one batch and its children.")
    batch_show.add_argument("batch_id")
    batch_show.add_argument("--json", action="store_true")
    batch_show.set_defaults(func=_cmd_batch_show)
    batch_resume = batch_sub.add_parser(
        "resume", help="Verify captured sources and retry unfinished children."
    )
    batch_resume.add_argument("batch_id")
    batch_resume.add_argument("--json", action="store_true")
    batch_resume.set_defaults(func=_cmd_batch_resume)
    batch_cancel = batch_sub.add_parser("cancel", help="Cancel a running batch.")
    batch_cancel.add_argument("batch_id")
    batch_cancel.add_argument("--json", action="store_true")
    batch_cancel.set_defaults(func=_cmd_batch_cancel)

    workflows = sub.add_parser("workflows", help="Inspect the workflow schema and durable runs.")
    workflows_sub = workflows.add_subparsers(dest="workflows_cmd", required=True)
    workflows_schema = workflows_sub.add_parser(
        "schema", help="Print the version 1 workflow manifest JSON Schema."
    )
    workflows_schema.set_defaults(func=_cmd_workflows_schema)
    workflows_list = workflows_sub.add_parser("list", help="List durable workflow runs.")
    workflows_list.add_argument("--limit", type=int, default=50)
    workflows_list.add_argument("--json", action="store_true")
    workflows_list.set_defaults(func=_cmd_workflows_list)
    workflows_show = workflows_sub.add_parser("show", help="Show a workflow and its steps.")
    workflows_show.add_argument("workflow_id")
    workflows_show.add_argument("--json", action="store_true")
    workflows_show.set_defaults(func=_cmd_workflows_show)
    workflows_watch = workflows_sub.add_parser(
        "watch", help="Watch a workflow owned by this or another local process."
    )
    workflows_watch.add_argument("workflow_id")
    workflows_watch.add_argument("--json", action="store_true")
    workflows_watch.set_defaults(func=_cmd_workflows_watch)
    workflows_resume = workflows_sub.add_parser(
        "resume", help="Verify sources and retry unfinished workflow steps."
    )
    workflows_resume.add_argument("workflow_id")
    workflows_resume.add_argument("--json", action="store_true")
    workflows_resume.add_argument("--output-dir", default=None, metavar="DIR")
    workflows_resume.set_defaults(func=_cmd_workflows_resume)
    workflows_cancel = workflows_sub.add_parser("cancel", help="Cancel a running workflow.")
    workflows_cancel.add_argument("workflow_id")
    workflows_cancel.add_argument("--json", action="store_true")
    workflows_cancel.set_defaults(func=_cmd_workflows_cancel)

    transactions = sub.add_parser(
        "transactions", help="Inspect durable commit and restore recovery journals."
    )
    transactions_sub = transactions.add_subparsers(dest="transactions_cmd", required=True)
    transactions_list = transactions_sub.add_parser("list")
    transactions_list.add_argument("--json", action="store_true")
    transactions_list.set_defaults(func=_cmd_transactions_list)
    transactions_show = transactions_sub.add_parser("show")
    transactions_show.add_argument("transaction_id")
    transactions_show.add_argument("--json", action="store_true")
    transactions_show.set_defaults(func=_cmd_transactions_show)

    artifacts = sub.add_parser("artifacts", help="Inspect and export durable job outputs.")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_cmd", required=True)
    artifacts_list = artifacts_sub.add_parser("list")
    artifacts_list.add_argument("--limit", type=int, default=50)
    artifacts_list.add_argument("--json", action="store_true")
    artifacts_list.set_defaults(func=_cmd_artifacts_list)
    artifacts_show = artifacts_sub.add_parser("show")
    artifacts_show.add_argument("artifact_id")
    artifacts_show.add_argument("--json", action="store_true")
    artifacts_show.set_defaults(func=_cmd_artifacts_show)
    artifacts_export = artifacts_sub.add_parser("export")
    artifacts_export.add_argument("artifact_id")
    artifacts_export.add_argument("path")
    artifacts_export.add_argument("--overwrite", action="store_true")
    artifacts_export.set_defaults(func=_cmd_artifacts_export)
    artifacts_pin = artifacts_sub.add_parser("pin", help="Retain an artifact across cleanup.")
    artifacts_pin.add_argument("artifact_id")
    artifacts_pin.set_defaults(func=_cmd_artifacts_pin)
    artifacts_unpin = artifacts_sub.add_parser(
        "unpin", help="Remove an explicit artifact retention pin."
    )
    artifacts_unpin.add_argument("artifact_id")
    artifacts_unpin.set_defaults(func=_cmd_artifacts_unpin)
    artifacts_gc = artifacts_sub.add_parser(
        "gc", help="Plan or explicitly apply reference-aware artifact cleanup."
    )
    artifacts_gc.add_argument("--older-than-days", type=int, default=None)
    artifacts_gc.add_argument("--apply", action="store_true")
    artifacts_gc.add_argument("--confirm", action="store_true")
    artifacts_gc.add_argument("--json", action="store_true")
    artifacts_gc.set_defaults(func=_cmd_artifacts_gc)

    changes = sub.add_parser("changes", help="Preview, approve, commit, and restore safe edits.")
    changes_sub = changes.add_subparsers(dest="changes_cmd", required=True)
    changes_preview = changes_sub.add_parser(
        "preview", help="Preview an existing occurrence property value update."
    )
    changes_preview.add_argument("model", help="IFC model to inspect without modifying it.")
    changes_preview.add_argument("--global-id", action="append", required=True, dest="global_ids")
    changes_preview.add_argument("--pset", required=True, dest="pset_name")
    changes_preview.add_argument("--property", required=True, dest="property_name")
    changes_preview.add_argument(
        "--create-missing",
        action="store_true",
        help="Explicitly preview creating a missing occurrence property or property set.",
    )
    changes_preview.add_argument(
        "--nominal-type",
        default=None,
        help="IFC value type for creation, such as IfcLabel or IfcLengthMeasure.",
    )
    value_group = changes_preview.add_mutually_exclusive_group(required=True)
    value_group.add_argument("--value", dest="plain_value", help="String property value.")
    value_group.add_argument(
        "--value-json",
        dest="json_value",
        help="JSON scalar for a string, number, boolean, or null value.",
    )
    changes_preview.add_argument("--expected-revision", default=None)
    changes_preview.add_argument("--json", action="store_true")
    changes_preview.set_defaults(func=_cmd_changes_preview)

    changes_classify = changes_sub.add_parser(
        "classify", help="Preview a direct occurrence classification assignment."
    )
    changes_classify.add_argument("model", help="IFC model to inspect without modifying it.")
    changes_classify.add_argument("--global-id", action="append", required=True, dest="global_ids")
    changes_classify.add_argument("--system", required=True, dest="classification_name")
    changes_classify.add_argument("--identification", required=True)
    changes_classify.add_argument("--name", required=True, dest="reference_name")
    changes_classify.add_argument("--expected-revision", default=None)
    changes_classify.add_argument("--json", action="store_true")
    changes_classify.set_defaults(func=_cmd_changes_classify)

    changes_show = changes_sub.add_parser("show", help="Show one ChangeSet preview.")
    changes_show.add_argument("change_set_id")
    changes_show.add_argument("--json", action="store_true")
    changes_show.set_defaults(func=_cmd_changes_show)

    changes_approve = changes_sub.add_parser(
        "approve", help="Create an explicit caller approval for a ChangeSet."
    )
    changes_approve.add_argument("change_set_id")
    changes_approve.add_argument("--by", required=True, dest="approved_by")
    changes_approve.add_argument("--reason", default="")
    changes_approve.add_argument("--json", action="store_true")
    changes_approve.set_defaults(func=_cmd_changes_approve)

    changes_commit = changes_sub.add_parser(
        "commit", help="Verify and atomically commit an approved ChangeSet."
    )
    changes_commit.add_argument("model")
    changes_commit.add_argument("change_set_id")
    changes_commit.add_argument("--approval", required=True, dest="approval_id")
    changes_commit.add_argument("--json", action="store_true")
    changes_commit.set_defaults(func=_cmd_changes_commit)

    changes_receipt = changes_sub.add_parser("receipt", help="Show a commit receipt.")
    changes_receipt.add_argument("commit_id")
    changes_receipt.add_argument("--json", action="store_true")
    changes_receipt.set_defaults(func=_cmd_changes_receipt)

    changes_restore = changes_sub.add_parser(
        "restore", help="Restore the verified backup from a commit receipt."
    )
    changes_restore.add_argument("model")
    changes_restore.add_argument("commit_id")
    changes_restore.add_argument(
        "--confirm", action="store_true", help="Required explicit restore confirmation."
    )
    changes_restore.add_argument("--json", action="store_true")
    changes_restore.set_defaults(func=_cmd_changes_restore)

    st = sub.add_parser("settings", help="Inspect and edit user settings.")
    st_sub = st.add_subparsers(dest="settings_cmd", required=True)
    st_list = st_sub.add_parser("list")
    st_list.add_argument("--sources", action="store_true")
    st_list.add_argument("--json", action="store_true")
    st_list.set_defaults(func=_cmd_settings_list)
    st_get = st_sub.add_parser("get")
    st_get.add_argument("key")
    st_get.set_defaults(func=_cmd_settings_get)
    st_set = st_sub.add_parser("set")
    st_set.add_argument("key")
    st_set.add_argument("value")
    st_set.set_defaults(func=_cmd_settings_set)
    st_unset = st_sub.add_parser("unset")
    st_unset.add_argument("key")
    st_unset.set_defaults(func=_cmd_settings_unset)
    st_path = st_sub.add_parser("path")
    st_path.set_defaults(func=_cmd_settings_path)

    rec = sub.add_parser("recents", help="Recently opened models.")
    rec_sub = rec.add_subparsers(dest="recents_cmd", required=True)
    rec_list = rec_sub.add_parser("list")
    rec_list.add_argument("--json", action="store_true")
    rec_list.set_defaults(func=_cmd_recents_list)
    rec_clear = rec_sub.add_parser("clear")
    rec_clear.set_defaults(func=_cmd_recents_clear)

    tok = sub.add_parser("token", help="Manage the persistent server token.")
    tok_sub = tok.add_subparsers(dest="token_cmd", required=True)
    tok_show = tok_sub.add_parser("show", help="Print the current token.")
    tok_show.set_defaults(func=_cmd_token_show)
    tok_rotate = tok_sub.add_parser(
        "rotate", help="Generate a new token (existing client configs stop working)."
    )
    tok_rotate.set_defaults(func=_cmd_token_rotate)
    tok_path = tok_sub.add_parser("path", help="Print where the token is stored.")
    tok_path.set_defaults(func=_cmd_token_path)

    kb = sub.add_parser("knowledge", help="The offline IFC reference index.")
    kb_sub = kb.add_subparsers(dest="knowledge_cmd", required=True)
    kb_build = kb_sub.add_parser("build", help="Build or rebuild the index.")
    kb_build.add_argument("--force", action="store_true", help="Rebuild even if it exists.")
    kb_build.set_defaults(func=_cmd_knowledge_build)
    kb_status = kb_sub.add_parser("status", help="Show where the index is and what it holds.")
    kb_status.add_argument("--json", action="store_true")
    kb_status.set_defaults(func=_cmd_knowledge_status)
    kb_search = kb_sub.add_parser("search", help="Search the index from the shell.")
    kb_search.add_argument("query", nargs="+")
    kb_search.add_argument("--kind", action="append", default=None, metavar="KIND")
    kb_search.add_argument("--schema", default=None)
    kb_search.add_argument("--limit", type=int, default=10)
    kb_search.add_argument("--json", action="store_true")
    kb_search.set_defaults(func=_cmd_knowledge_search)

    plugins = sub.add_parser("plugins", help="Inspect trusted operation plugins.")
    plugins_sub = plugins.add_subparsers(dest="plugins_cmd", required=True)
    plugins_list = plugins_sub.add_parser(
        "list", help="List discovered plugins without importing their code."
    )
    plugins_list.add_argument("--json", action="store_true")
    plugins_list.set_defaults(func=_cmd_plugins_list)
    plugins_doctor = plugins_sub.add_parser(
        "doctor", help="Load configured plugins and validate registration."
    )
    plugins_doctor.add_argument("--json", action="store_true")
    plugins_doctor.set_defaults(func=_cmd_plugins_doctor)

    ses = sub.add_parser("sessions", help="Audit-log sessions.")
    ses_sub = ses.add_subparsers(dest="sessions_cmd", required=True)
    ses_list = ses_sub.add_parser("list")
    ses_list.add_argument("--json", action="store_true")
    ses_list.set_defaults(func=_cmd_sessions_list)
    ses_show = ses_sub.add_parser("show")
    ses_show.add_argument("id")
    ses_show.set_defaults(func=_cmd_sessions_show)
    ses_verify = ses_sub.add_parser("verify", help="Verify an audit session's hash chain.")
    ses_verify.add_argument("id")
    ses_verify.add_argument("--json", action="store_true")
    ses_verify.set_defaults(func=_cmd_sessions_verify)
    ses_clear = ses_sub.add_parser("clear")
    ses_clear.set_defaults(func=_cmd_sessions_clear)

    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", default=None, help="IFC file to load at startup.")
    parser.add_argument("--mode", choices=_MODES, default=None, help="Session mode.")
    parser.add_argument("--port", type=_port, default=None, help="MCP HTTP port (default 8383).")
    parser.add_argument(
        "--viewer",
        action="store_true",
        help="Enable the local 3D web viewer (needs the HTTP server: TUI or --http).",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Enable the browser chat panel (talks to the LLM provider you configure).",
    )
    parser.add_argument(
        "--allow-dir",
        action="append",
        default=None,
        metavar="PATH",
        help="Extra directory the LLM may open/save models in (repeatable).",
    )
    parser.add_argument("--log-level", choices=["debug", "info", "warning", "error"], default=None)


def _version_line() -> str:
    # metadata only: importing ifcopenshell itself costs seconds per launch
    try:
        from importlib.metadata import version

        ios = version("ifcopenshell")
    except Exception:
        ios = "missing"
    return f"ifc-console {__version__} (ifcopenshell {ios}, python {platform.python_version()})"


# --------------------------------------------------------------------------- helpers
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
    # the chat panel is served over the same HTTP surface as the viewer
    chat_flag = True if getattr(args, "chat", False) else None
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


def _load_model_blocking(core: AppCore, raw_path: str) -> int:
    path = Path(raw_path).expanduser().resolve()
    if not path.exists():
        print(f"error: {path} does not exist", file=sys.stderr)
        return 4
    core.add_allowed_dir(path.parent)
    size_mb = path.stat().st_size / 1_048_576
    print(f"loading {path.name} ({size_mb:.1f} MB)...", flush=True)
    try:
        import asyncio

        asyncio.run(core.open_model(path))
    except Exception as exc:
        print(f"error: could not load {path.name}: {exc}", file=sys.stderr)
        return 4
    return 0


# --------------------------------------------------------------------------- run modes
def _cmd_interactive(args: argparse.Namespace) -> int:
    if args.no_tui:
        return _run_headless_http(args)
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        print(
            "error: the interactive console needs a terminal; use --no-tui or "
            "`ifc-console serve --stdio`.",
            file=sys.stderr,
        )
        return 3
    # instant feedback; the console takes over the screen once it is up
    print(f"ifc-console {__version__} starting...", file=sys.stderr, flush=True)
    from ifc_console import preload

    preload.start()
    store = _make_store(args)
    _setup_logging(store, level=store.settings.logging.level)
    # the TUI owns the terminal: drop the stderr handler, keep the file log
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, RotatingFileHandler):
            root.removeHandler(h)
    core = _make_core(args, store, transport="http")
    preload.release()
    from ifc_console.tui.app import run_tui

    initial_file = Path(args.file).expanduser().resolve() if args.file else None
    if initial_file is not None and not initial_file.exists():
        print(f"error: {initial_file} does not exist", file=sys.stderr)
        return 4
    return run_tui(core, initial_file=initial_file)


def _run_headless_http(args: argparse.Namespace) -> int:
    from ifc_console import preload

    preload.start()
    store = _make_store(args)
    _setup_logging(store, level=store.settings.logging.level)
    core = _make_core(args, store, transport="http")
    preload.release()
    core.start_audit()
    core.start_knowledge()
    if args.file:
        rc = _load_model_blocking(core, args.file)
        if rc:
            return rc
    from ifc_console.portcheck import FREE, conflict_hint, port_status

    kind, detail = port_status(core.port, core.token)
    if kind != FREE:
        print(f"error: port {core.port} is already in use by {detail}.", file=sys.stderr)
        print(f"hint: {conflict_hint(kind, core.port)}", file=sys.stderr)
        core.shutdown()
        return 2

    from ifc_console.mcp.server import build_http_app, build_mcp, make_uvicorn_server

    mcp = build_mcp(core)
    app = build_http_app(core, mcp)
    server = make_uvicorn_server(app, core.port)
    banner = [
        f"ifc-console {__version__} (headless HTTP)",
        f"  MCP endpoint : {core.mcp_url}",
        f"  bearer token : {core.token}",
        f"  mode         : {core.policy.mode.value}",
        f"  model        : {core.session.name or '(none loaded)'}",
    ]
    if core.viewer.enabled:
        banner.append(f"  3D viewer    : {core.viewer.url}")
    banner.append("  connect      : " + _claude_code_http_cmd(core.port, core.token))
    banner.append("Ctrl+C to stop.")
    # flush: piped/redirected stdout must show the token before the loop blocks
    print("\n".join(banner), flush=True)
    try:
        import asyncio

        asyncio.run(server.serve())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        pass  # uvicorn aborts failed startups this way; reported just below
    finally:
        core.shutdown()
    if not getattr(server, "started", False):
        # bind lost a race after the pre-check; uvicorn already logged why
        print(f"error: the server never came up on port {core.port}.", file=sys.stderr)
        return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.http:
        return _run_headless_http(args)
    # stdio: stdout belongs to the protocol; logs go to stderr + file only
    from ifc_console import preload

    preload.start()
    store = _make_store(args)
    _setup_logging(store, level=store.settings.logging.level)
    core = _make_core(args, store, transport="stdio")
    preload.release()
    core.start_audit()
    core.start_knowledge()
    if args.file:
        rc = _load_model_blocking(core, args.file)
        if rc:
            return rc
    from ifc_console.mcp.server import build_mcp

    mcp = build_mcp(core)
    log.info(
        "ifc-console %s stdio server starting (mode=%s, model=%s)",
        __version__,
        core.policy.mode.value,
        core.session.name or "none",
    )
    try:
        mcp.run(transport="stdio")
    except KeyboardInterrupt:
        pass
    finally:
        core.shutdown()
    return 0


def _cmd_bridge(args: argparse.Namespace) -> int:
    """stdout is the protocol here; every log line goes to stderr."""
    from ifc_console.bridge import Bridge

    # MCP clients launch the bridge with cwd inside an arbitrary repo. A
    # cloned project's settings must not steer where this machine's token is
    # sent, so project layers are ignored; mcp-config pins ports via --port.
    store = _make_store(args, include_project=False)
    # stderr only: the client owns this process, and a second writer would
    # fight the console for the rotating log file handle on Windows.
    _setup_logging(store, level=store.settings.logging.level, to_file=False)
    port = args.port if args.port is not None else store.settings.server.port
    if not args.token and not store.settings.server.persistent_token:
        print(
            "error: bridge needs --token when server.persistent_token=false; "
            "copy the token printed by the running console.",
            file=sys.stderr,
        )
        return 2
    token = args.token or store.load_server_token()
    bridge = Bridge(
        f"http://127.0.0.1:{port}/mcp", token, cache_file=store.home / "tools_cache.json"
    )
    log.info("ifc-console %s bridge to port %s", __version__, port)
    try:
        return bridge.run()
    except KeyboardInterrupt:
        return 0


# --------------------------------------------------------------------------- mcp-config
# Pinned so npx serves its cached install instead of asking the npm registry
# for "latest" on every launch. The uncached first run downloads the package,
# which can exceed Claude Desktop's 60 second startup timeout; docs and
# /connect tell users to warm the cache once with this exact spec (npx caches
# each version spec separately, so the pre-warm must match).
MCP_REMOTE_SPEC = "mcp-remote@0.1.38"


def _claude_code_http_cmd(port: int, token: str | None) -> str:
    url = f"http://127.0.0.1:{port}/mcp"
    # User scope makes this a one-time machine setup instead of tying it to
    # whichever project directory the command happened to run from.
    cmd = f"claude mcp add --transport http --scope user ifc-console {url}"
    if token:
        cmd += f' --header "Authorization: Bearer {token}"'
    return cmd


def _bridge_argv(port: int, token: str | None = None) -> list[str]:
    """How a client should launch the stdio bridge.

    An absolute path when we can find one: GUI clients (Claude Desktop) do not
    inherit the shell PATH, and "command not found" is the most common wiring
    failure. uvx is the fallback for an ephemeral install.
    """
    import shutil

    exe = shutil.which("ifc-console")
    argv = [exe, "bridge"] if exe else ["uvx", "ifc-console", "bridge"]
    if port != 8383:
        argv += ["--port", str(port)]
    if token:
        argv += ["--token", token]
    return argv


def _quote_argv(argv: list[str]) -> str:
    if platform.system() == "Windows":
        import subprocess

        return subprocess.list2cmdline(argv)
    return shlex.join(argv)


def build_config_snippet(
    client: str,
    transport: str | None,
    *,
    port: int,
    file: str | None,
    mode: str,
    token: str | None,
    bridge_token: str | None = None,
) -> str:
    stdio_args = ["ifc-console", "serve", "--stdio", "--mode", mode]
    if file:
        stdio_args += ["--file", file]
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {token or '<TOKEN>'}"}

    # Every default snippet attaches to the reusable terminal-owned session.
    # The bridge is the default because it makes start order irrelevant: the
    # client can launch before ifc-console and still connect. A model path
    # belongs only to an explicitly requested standalone stdio process.
    transport = transport or "bridge"
    bridge_argv = _bridge_argv(port, bridge_token)

    if client == "claude-code":
        if transport == "http":
            return _claude_code_http_cmd(port, token or "<TOKEN>")
        argv = bridge_argv if transport == "bridge" else ["uvx", *stdio_args]
        return f"claude mcp add --scope user ifc-console -- {_quote_argv(argv)}"
    if client == "claude-desktop":
        if transport == "bridge":
            snippet = {
                "mcpServers": {"ifc-console": {"command": bridge_argv[0], "args": bridge_argv[1:]}}
            }
        elif transport == "stdio":
            snippet = {"mcpServers": {"ifc-console": {"command": "uvx", "args": stdio_args}}}
        else:
            # Claude Desktop starts local MCP entries over stdio. mcp-remote
            # is the bridge to this terminal's Streamable HTTP endpoint.
            snippet = {
                "mcpServers": {
                    "ifc-console": {
                        "command": "npx",
                        "args": [
                            "-y",
                            MCP_REMOTE_SPEC,
                            url,
                            "--allow-http",
                            "--transport",
                            "http-only",
                            "--header",
                            "Authorization:${IFC_CONSOLE_AUTH_HEADER}",
                        ],
                        "env": {"IFC_CONSOLE_AUTH_HEADER": headers["Authorization"]},
                    }
                }
            }
        return json.dumps(snippet, indent=2)
    if client == "cursor":
        if transport == "bridge":
            snippet = {
                "mcpServers": {"ifc-console": {"command": bridge_argv[0], "args": bridge_argv[1:]}}
            }
        elif transport == "stdio":
            snippet = {"mcpServers": {"ifc-console": {"command": "uvx", "args": stdio_args}}}
        else:
            snippet = {"mcpServers": {"ifc-console": {"url": url, "headers": headers}}}
        return json.dumps(snippet, indent=2)
    if client == "vscode":
        if transport == "bridge":
            snippet = {
                "servers": {
                    "ifc-console": {
                        "type": "stdio",
                        "command": bridge_argv[0],
                        "args": bridge_argv[1:],
                    }
                }
            }
        elif transport == "stdio":
            snippet = {
                "servers": {"ifc-console": {"type": "stdio", "command": "uvx", "args": stdio_args}}
            }
        else:
            snippet = {"servers": {"ifc-console": {"type": "http", "url": url, "headers": headers}}}
        return json.dumps(snippet, indent=2)
    if client == "codex":
        if transport in ("bridge", "stdio"):
            argv = bridge_argv if transport == "bridge" else ["uvx", *stdio_args]
            arg_list = ", ".join(json.dumps(a, ensure_ascii=False) for a in argv[1:])
            return (
                f"[mcp_servers.ifc-console]\n"
                f"command = {json.dumps(argv[0], ensure_ascii=False)}\n"
                f"args = [{arg_list}]"
            )
        authorization = json.dumps(headers["Authorization"])
        return (
            f"[mcp_servers.ifc-console]\n"
            f"url = {json.dumps(url)}\n"
            f"http_headers = {{ Authorization = {authorization} }}"
        )
    raise ValueError(client)


def _cmd_mcp_config(args: argparse.Namespace) -> int:
    store = _make_store(args)
    port = args.port if args.port is not None else store.settings.server.port
    mode = args.mode or store.settings.mode.default
    persistent = store.settings.server.persistent_token
    if args.transport == "bridge" and not persistent:
        print(
            "error: bridge configs require server.persistent_token=true. Use "
            "--transport stdio, or use --transport http with the current run's token.",
            file=sys.stderr,
        )
        return 2
    transport = args.transport or ("bridge" if persistent else "stdio")
    # The default bridge snippet stays valid across restarts without placing
    # the persistent token in a client configuration file.
    token = None
    if persistent and store.settings.server.token_in_config_snippets:
        token = store.load_server_token()
    snippet = build_config_snippet(
        args.client, transport, port=port, file=args.file, mode=mode, token=token
    )
    print(snippet)
    if "<TOKEN>" in snippet:
        if not store.settings.server.token_in_config_snippets:
            note = (
                "<TOKEN> is hidden by server.token_in_config_snippets; replace it "
                "manually (`ifc-console token show` or /copy token)."
            )
        else:
            note = (
                "<TOKEN> is this run's bearer token; the running ifc-console console "
                "can copy a complete setup with /copy <client>."
            )
        print(f"\nnote: {note}", file=sys.stderr)
    elif transport != "stdio":
        print(
            "note: configure once; this shared-console setup follows whichever model you "
            "open with /file and keeps working across ifc-console restarts (rotate "
            "the token with `ifc-console token rotate`).",
            file=sys.stderr,
        )
    return 0


def _cmd_token_show(_args: argparse.Namespace) -> int:
    store = _new_store()
    if not store.settings.server.persistent_token:
        print(
            "per-run tokens are enabled (server.persistent_token=false); "
            "each run prints its own token at startup.",
            file=sys.stderr,
        )
        return 3
    print(store.load_server_token())
    return 0


def _cmd_token_rotate(_args: argparse.Namespace) -> int:
    store = _new_store()
    token = store.rotate_server_token()
    print(token)
    print(
        "token rotated; update your MCP client configs "
        "(`ifc-console mcp-config` or /connect in the console) and restart ifc-console.",
        file=sys.stderr,
    )
    return 0


def _cmd_token_path(_args: argparse.Namespace) -> int:
    print(_new_store().token_file)
    return 0


# --------------------------------------------------------------------------- check
def _cmd_check(args: argparse.Namespace) -> int:
    from ifc_console.checks import render, run_check
    from ifc_console.core.results import ToolError

    model = Path(args.model)
    if not model.is_file():
        print(f"error: {model} does not exist", file=sys.stderr)
        return 4
    try:
        report = run_check(
            model,
            ids_paths=[Path(p) for p in args.ids],
            express_rules=args.express_rules,
            max_issues=args.max_issues,
        )
    except ToolError as exc:  # missing ifctester extra or unreadable IDS file
        print(f"error: {exc.message}", file=sys.stderr)
        print(f"hint: {exc.hint}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: could not parse {model.name}: {exc}", file=sys.stderr)
        return 4
    rendered = render(report, args.format)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
        print(f"wrote {args.format} report to {args.output}")
    else:
        print(rendered)
    return 0 if report["passed"] else 5


# --------------------------------------------------------------------------- jobs
def _automation_core(mode: Mode | None = None):
    from ifc_console.app import AppCore

    store = _new_store()
    return AppCore(store, mode=mode, transport="cli")


def _print_job(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"job       {record.job_id}")
    print(f"state     {record.state.value}")
    print(f"progress  {record.progress}%")
    print(f"message   {record.message}")
    print(f"phase     {record.phase}")
    print(f"cancel    {'allowed' if record.cancellable else 'closed'}")
    if record.transaction_id:
        print(f"transaction {record.transaction_id}")
    print(f"revision  {record.spec.revision.revision_id}")
    if record.summary:
        for key, value in record.summary.items():
            print(f"{key:<10} {value}")
    if record.failure is not None:
        print(f"error     {record.failure.code}: {record.failure.message}")
    for artifact in record.artifacts:
        print(f"artifact  {artifact.artifact_id}  {artifact.name}")


def _tool_error(exc: Exception) -> int:
    print(f"error: {getattr(exc, 'message', str(exc))}", file=sys.stderr)
    hint = getattr(exc, "hint", "")
    if hint:
        print(f"hint: {hint}", file=sys.stderr)
    return 2


def _cmd_jobs_validate(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    model = Path(args.model).expanduser().resolve()
    ids_paths = tuple(Path(path).expanduser().resolve() for path in args.ids)
    core = _automation_core()

    async def run():
        core.start_audit()
        core.add_allowed_dir(model.parent)
        for path in ids_paths:
            core.add_allowed_dir(path.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_validation(
            ids_paths=ids_paths,
            express_rules=args.express_rules,
            max_issues=args.max_issues,
            expected_revision=args.expected_revision,
        )
        completed = submitted
        async for update in core.jobs.watch(submitted.job_id):
            completed = update
            print(
                f"{update.job_id}  {update.progress:3}%  {update.message}",
                file=sys.stderr,
            )
        return completed

    try:
        record = asyncio.run(run())
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser().resolve()
            for artifact in record.artifacts:
                core.artifacts.export(artifact.artifact_id, output_dir / artifact.name)
        _print_job(record, as_json=args.json)
        if record.state.value != "succeeded":
            return 1
        return 0 if record.summary.get("passed") else 5
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_jobs_commit(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_commit(args.change_set_id, approval_id=args.approval_id)
        completed = submitted
        async for update in core.jobs.watch(submitted.job_id):
            completed = update
            print(
                f"{update.job_id}  {update.progress:3}%  {update.phase}  {update.message}",
                file=sys.stderr,
            )
        return completed

    try:
        record = asyncio.run(run())
        _print_job(record, as_json=args.json)
        return 0 if record.state.value == "succeeded" else 1
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_jobs_restore(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_restore(args.commit_id, confirm=args.confirm)
        completed = submitted
        async for update in core.jobs.watch(submitted.job_id):
            completed = update
            print(
                f"{update.job_id}  {update.progress:3}%  {update.phase}  {update.message}",
                file=sys.stderr,
            )
        return completed

    try:
        record = asyncio.run(run())
        _print_job(record, as_json=args.json)
        return 0 if record.state.value == "succeeded" else 1
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_jobs_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        records = core.jobs.list(limit=args.limit)
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        elif not records:
            print("(no jobs)")
        else:
            for record in records:
                print(
                    f"{record.job_id}  {record.state.value:9}  "
                    f"{record.progress:3}%  {record.message}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_jobs_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = core.jobs.get(args.job_id)
        _print_job(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_jobs_cancel(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = asyncio.run(core.jobs.cancel(args.job_id))
        _print_job(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- batches
def _print_batch(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"batch      {record.batch_id}")
    print(f"state      {record.state.value}")
    print(f"progress   {record.progress}%")
    print(f"message    {record.message}")
    print(f"inputs     {len(record.children)}")
    print(f"runs       {record.run_count}")
    print(f"concurrency {record.spec.concurrency}")
    print(f"policy     {record.spec.failure_policy}")
    for child in record.children:
        name = Path(child.source.path).name
        detail = f"job={child.job_id}" if child.job_id else "not submitted"
        print(
            f"child {child.index:03} {child.state.value:9} attempts={child.attempts} "
            f"{name}  {detail}"
        )
        if child.failure is not None:
            print(f"      error {child.failure.code}: {child.failure.message}")
    if record.aggregate_artifact is not None:
        print(
            f"manifest   {record.aggregate_artifact.artifact_id}  {record.aggregate_artifact.name}"
        )


async def _watch_batch(core: Any, batch_id: str) -> Any:
    completed = core.batches.get(batch_id)
    async for update in core.batches.watch(batch_id):
        completed = update
        print(
            f"{update.batch_id}  {update.progress:3}%  {update.message}",
            file=sys.stderr,
        )
    return completed


def _batch_exit_code(record: Any) -> int:
    if record.state.value != "succeeded":
        return 1
    return 0 if record.summary.get("passed") else 5


def _cmd_batch_validate(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    models = tuple(Path(path).expanduser().resolve() for path in args.models)
    ids_paths = tuple(Path(path).expanduser().resolve() for path in args.ids)
    core = _automation_core()

    async def run():
        core.start_audit()
        for path in (*models, *ids_paths):
            core.add_allowed_dir(path.parent)
        submitted = await core.batches.submit_validation(
            models,
            ids_paths=ids_paths,
            express_rules=args.express_rules,
            max_issues=args.max_issues,
            concurrency=args.concurrency,
            failure_policy=args.failure_policy,
        )
        return await _watch_batch(core, submitted.batch_id)

    try:
        record = asyncio.run(run())
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser().resolve()
            if record.aggregate_artifact is not None:
                core.artifacts.export(
                    record.aggregate_artifact.artifact_id,
                    output_dir / record.aggregate_artifact.name,
                )
            for child in record.children:
                for artifact in child.artifacts:
                    core.artifacts.export(
                        artifact.artifact_id,
                        output_dir / f"{child.index:03}-{artifact.name}",
                    )
        _print_batch(record, as_json=args.json)
        return _batch_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_batch_query(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    models = tuple(Path(path).expanduser().resolve() for path in args.models)
    fields = tuple(args.fields or ("name", "storey", "type_name"))
    core = _automation_core()

    async def run():
        core.start_audit()
        for path in models:
            core.add_allowed_dir(path.parent)
        submitted = await core.batches.submit_query(
            models,
            query=args.selector,
            fields=fields,
            order_by=args.order_by,
            output_format=args.format,
            limit=args.limit,
            concurrency=args.concurrency,
            failure_policy=args.failure_policy,
        )
        return await _watch_batch(core, submitted.batch_id)

    try:
        record = asyncio.run(run())
        if args.output_dir:
            output_dir = Path(args.output_dir).expanduser().resolve()
            if record.aggregate_artifact is not None:
                core.artifacts.export(
                    record.aggregate_artifact.artifact_id,
                    output_dir / record.aggregate_artifact.name,
                )
            for child in record.children:
                for artifact in child.artifacts:
                    core.artifacts.export(
                        artifact.artifact_id,
                        output_dir / f"{child.index:03}-{artifact.name}",
                    )
        _print_batch(record, as_json=args.json)
        return _batch_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_batch_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        records = core.batches.list(limit=args.limit)
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        elif not records:
            print("(no batches)")
        else:
            for record in records:
                print(
                    f"{record.batch_id}  {record.state.value:11}  "
                    f"{record.progress:3}%  {len(record.children):4} inputs  {record.message}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_batch_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        _print_batch(core.batches.get(args.batch_id), as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_batch_resume(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()

    async def run():
        core.start_audit()
        resumed = await core.batches.resume(args.batch_id)
        return await _watch_batch(core, resumed.batch_id)

    try:
        record = asyncio.run(run())
        _print_batch(record, as_json=args.json)
        return _batch_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_batch_cancel(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        core.start_audit()
        record = asyncio.run(core.batches.cancel(args.batch_id))
        _print_batch(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- workflows
def _print_workflow(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"workflow    {record.workflow_id}")
    print(f"name        {record.plan.spec.name}")
    print(f"plan        {record.plan.plan_id}")
    print(f"state       {record.state.value}")
    print(f"progress    {record.progress}%")
    print(f"message     {record.message}")
    print(f"runs        {record.run_count}")
    for step in record.steps:
        detail = f"batch={step.batch_id}" if step.batch_id else "not submitted"
        print(
            f"step {step.id:<20} {step.state.value:11} attempts={step.attempts} "
            f"output={step.output} {detail}"
        )
        if step.failure is not None:
            print(f"     error {step.failure.code}: {step.failure.message}")
    if record.aggregate_artifact is not None:
        print(
            f"manifest    {record.aggregate_artifact.artifact_id}  {record.aggregate_artifact.name}"
        )


def _print_workflow_plan(plan: Any, *, as_json: bool) -> None:
    if as_json:
        print(plan.model_dump_json(indent=2))
        return
    print(f"workflow    {plan.spec.name}")
    print(f"plan        {plan.plan_id}")
    print(f"version     {plan.spec.version}")
    print(f"steps       {len(plan.steps)}")
    print(f"children    {plan.total_children}")
    for step in plan.steps:
        dependencies = ",".join(step.needs) if step.needs else "-"
        print(
            f"step {step.id:<20} {step.batch_spec.operation.kind:10} "
            f"inputs={len(step.batch_spec.inputs)} needs={dependencies} output={step.output}"
        )


async def _watch_workflow(core: Any, workflow_id: str) -> Any:
    completed = core.workflows.get(workflow_id)
    async for update in core.workflows.watch(workflow_id):
        completed = update
        print(
            f"{update.workflow_id}  {update.progress:3}%  {update.message}",
            file=sys.stderr,
        )
    return completed


def _workflow_exit_code(record: Any) -> int:
    if record.state.value != "succeeded":
        return 1
    return 0 if record.summary.get("passed") else 5


def _export_workflow(core: Any, record: Any, output_dir: Path) -> None:
    if record.aggregate_artifact is not None:
        core.artifacts.export(
            record.aggregate_artifact.artifact_id,
            output_dir / record.aggregate_artifact.name,
        )
    for step in record.steps:
        if step.batch_id is None:
            continue
        batch = core.batches.get(step.batch_id)
        if batch.aggregate_artifact is not None:
            core.artifacts.export(
                batch.aggregate_artifact.artifact_id,
                output_dir / f"{step.output}-{batch.aggregate_artifact.name}",
            )
        for child in batch.children:
            for artifact in child.artifacts:
                core.artifacts.export(
                    artifact.artifact_id,
                    output_dir / f"{step.output}-{child.index:03}-{artifact.name}",
                )


def _cmd_workflow_run(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    manifest = Path(args.manifest).expanduser().resolve()
    core = _automation_core()

    async def run() -> Any:
        core.start_audit()
        core.add_allowed_dir(manifest.parent)
        plan = await core.workflows.plan_manifest(manifest)
        if args.plan:
            return plan
        submitted = await core.workflows.submit_plan(plan)
        return await _watch_workflow(core, submitted.workflow_id)

    try:
        result = asyncio.run(run())
        if args.plan:
            _print_workflow_plan(result, as_json=args.json)
            return 0
        if args.output_dir:
            _export_workflow(core, result, Path(args.output_dir).expanduser().resolve())
        _print_workflow(result, as_json=args.json)
        return _workflow_exit_code(result)
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_workflows_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        records = core.workflows.list(limit=args.limit)
        if args.json:
            print(json.dumps([record.model_dump(mode="json") for record in records], indent=2))
        elif not records:
            print("(no workflows)")
        else:
            for record in records:
                print(
                    f"{record.workflow_id}  {record.state.value:11}  "
                    f"{record.progress:3}%  {len(record.steps):3} steps  {record.plan.spec.name}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_workflows_schema(_args: argparse.Namespace) -> int:
    from ifc_console.core.workflows import WorkflowSpec

    print(json.dumps(WorkflowSpec.model_json_schema(), indent=2))
    return 0


def _cmd_workflows_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        _print_workflow(core.workflows.get(args.workflow_id), as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_workflows_watch(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = asyncio.run(_watch_workflow(core, args.workflow_id))
        _print_workflow(record, as_json=args.json)
        return _workflow_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_workflows_resume(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()

    async def run() -> Any:
        core.start_audit()
        existing = core.workflows.get(args.workflow_id)
        if existing.plan.manifest_path:
            core.add_allowed_dir(Path(existing.plan.manifest_path).parent)
        resumed = await core.workflows.resume(args.workflow_id)
        return await _watch_workflow(core, resumed.workflow_id)

    try:
        record = asyncio.run(run())
        if args.output_dir:
            _export_workflow(core, record, Path(args.output_dir).expanduser().resolve())
        _print_workflow(record, as_json=args.json)
        return _workflow_exit_code(record)
    except ToolError as exc:
        return _tool_error(exc)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_workflows_cancel(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        core.start_audit()
        record = asyncio.run(core.workflows.cancel(args.workflow_id))
        _print_workflow(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_transactions_list(args: argparse.Namespace) -> int:
    core = _automation_core()
    try:
        journals = core.transactions.journals.list()
        if args.json:
            print(json.dumps([item.model_dump(mode="json") for item in journals], indent=2))
        elif not journals:
            print("(no transactions)")
        else:
            for item in journals:
                print(
                    f"{item.transaction_id}  {item.kind.value:7}  "
                    f"{item.phase.value:18}  {item.target_path}"
                )
        return 0
    finally:
        core.shutdown()


def _cmd_transactions_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        journal = core.transactions.journals.get(args.transaction_id)
        if args.json:
            print(journal.model_dump_json(indent=2))
        else:
            print(f"transaction {journal.transaction_id}")
            print(f"kind        {journal.kind.value}")
            print(f"phase       {journal.phase.value}")
            print(f"target      {journal.target_path}")
            print(f"before      {journal.expected_before_sha256}")
            print(f"after       {journal.desired_after_sha256}")
            print(f"cancellable {journal.cancellable}")
            if journal.rollback_artifact_id:
                print(f"rollback    {journal.rollback_artifact_id}")
            if journal.receipt_artifact_id:
                print(f"receipt     {journal.receipt_artifact_id}")
            if journal.error:
                print(f"error       {journal.error}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _artifact_service():
    from ifc_console.application.artifacts import ArtifactService

    store = _new_store()
    store.ensure_dirs()
    return ArtifactService(store.artifacts_dir)


def _artifact_retention_service():
    from ifc_console.application.artifacts import ArtifactService
    from ifc_console.application.retention import ArtifactRetentionService
    from ifc_console.audit import AuditLog

    store = _new_store()
    store.ensure_dirs()
    artifacts = ArtifactService(store.artifacts_dir)
    audit = AuditLog(store.sessions_dir, store.settings.sessions.retention)
    audit.start({"interface": "cli", "command": "artifacts"})
    return (
        ArtifactRetentionService(
            artifacts,
            store.jobs_dir,
            batches_root=store.batches_dir,
            workflows_root=store.workflows_dir,
            default_retention_days=store.settings.automation.artifact_retention_days,
        ),
        audit,
    )


def _cmd_artifacts_list(args: argparse.Namespace) -> int:
    refs = _artifact_service().list(limit=args.limit)
    if args.json:
        print(json.dumps([ref.model_dump(mode="json") for ref in refs], indent=2))
    elif not refs:
        print("(no artifacts)")
    else:
        for ref in refs:
            print(f"{ref.artifact_id}  {ref.size_bytes:8}  {ref.name}")
    return 0


def _cmd_artifacts_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    try:
        ref = _artifact_service().get(args.artifact_id)
        if args.json:
            print(ref.model_dump_json(indent=2))
        else:
            print(f"artifact  {ref.artifact_id}")
            print(f"name      {ref.name}")
            print(f"type      {ref.media_type}")
            print(f"size      {ref.size_bytes}")
            print(f"producer  {ref.producer}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)


def _cmd_artifacts_export(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    try:
        target = _artifact_service().export(
            args.artifact_id, Path(args.path), overwrite=args.overwrite
        )
        print(target)
        return 0
    except ToolError as exc:
        return _tool_error(exc)


def _cmd_artifacts_pin(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    service, audit = _artifact_retention_service()
    try:
        ref = service.pin(args.artifact_id)
        audit.record("artifact_pinned", artifact_id=ref.artifact_id)
        print(f"pinned {ref.artifact_id}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        audit.end()


def _cmd_artifacts_unpin(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    service, audit = _artifact_retention_service()
    try:
        removed = service.unpin(args.artifact_id)
        audit.record("artifact_unpinned", artifact_id=args.artifact_id, pin_existed=removed)
        print("unpinned" if removed else "not pinned")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        audit.end()


def _cmd_artifacts_gc(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    service, audit = _artifact_retention_service()
    try:
        plan = service.plan(older_than_days=args.older_than_days)
        audit.record(
            "artifact_gc_planned",
            cutoff=plan.cutoff.isoformat(),
            candidate_count=plan.candidate_count,
            candidate_bytes=plan.candidate_bytes,
        )
        if args.apply:
            result = service.collect(plan, confirm=args.confirm)
            audit.record(
                "artifact_gc_completed",
                deleted_count=result.deleted_count,
                deleted_bytes=result.deleted_bytes,
                deleted_ids=list(result.deleted_ids),
            )
            if args.json:
                print(result.model_dump_json(indent=2))
            else:
                print(f"deleted {result.deleted_count} artifact(s), {result.deleted_bytes} byte(s)")
            return 0
        if args.json:
            print(plan.model_dump_json(indent=2))
        else:
            print(f"scanned    {plan.scanned_count}")
            print(f"retained   {plan.retained_count}")
            print(f"candidates {plan.candidate_count}")
            print(f"bytes      {plan.candidate_bytes}")
            for warning in plan.warnings:
                print(f"warning    {warning}")
            if plan.candidate_count:
                print("dry-run only; use --apply --confirm after reviewing the JSON plan")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        audit.end()


# --------------------------------------------------------------------------- changes
def _change_value(args: argparse.Namespace) -> Any:
    if args.json_value is None:
        return args.plain_value
    try:
        value = json.loads(args.json_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--value-json is invalid JSON: {exc}") from exc
    if value is not None and type(value) not in (str, int, float, bool):
        raise ValueError("--value-json must be a string, number, boolean, or null")
    return value


def _print_change_set(record: Any, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"change set  {record.change_set_id}")
    print(f"revision    {record.change_set.revision.revision_id}")
    print(f"source      {record.change_set.source.path}")
    for change in record.change_set.changes:
        if change.kind == "classification_assignment":
            print(
                f"change      {change.global_id}  {change.classification_name}."
                f"{change.identification}  assign {change.reference_name!r}"
            )
        else:
            print(
                f"change      {change.global_id}  {change.pset_name}.{change.property_name}  "
                f"{change.before!r} -> {change.after!r}"
            )


def _cmd_changes_preview(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.ASK)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        return await core.transactions.preview_property_value(
            global_ids=args.global_ids,
            pset_name=args.pset_name,
            property_name=args.property_name,
            value=_change_value(args),
            create_missing=args.create_missing,
            nominal_type=args.nominal_type,
            expected_revision=args.expected_revision,
        )

    try:
        record = asyncio.run(run())
        _print_change_set(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_changes_classify(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.ASK)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        return await core.transactions.preview_classification_assignment(
            global_ids=args.global_ids,
            classification_name=args.classification_name,
            identification=args.identification,
            reference_name=args.reference_name,
            expected_revision=args.expected_revision,
        )

    try:
        record = asyncio.run(run())
        _print_change_set(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4
    finally:
        core.shutdown()


def _cmd_changes_show(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = core.transactions.get_change_set(args.change_set_id)
        _print_change_set(record, as_json=args.json)
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_approve(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        core.start_audit()
        record = core.transactions.approve(
            args.change_set_id,
            approved_by=args.approved_by,
            reason=args.reason,
        )
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"approval    {record.approval_id}")
            print(f"change set  {record.approval.change_set_id}")
            print(f"approved by {record.approval.approved_by}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_commit(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_commit(args.change_set_id, approval_id=args.approval_id)
        completed = await core.jobs.wait(submitted.job_id)
        if completed.state.value != "succeeded":
            failure = completed.failure
            raise ToolError(
                failure.code if failure else "JOB_CANCELLED",
                failure.message if failure else "the commit job did not complete",
                failure.hint if failure else "Inspect the durable job record.",
            )
        return core.transactions.get_commit(str(completed.summary["commit_id"]))

    try:
        record = asyncio.run(run())
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"commit      {record.commit_id}")
            print(f"target      {record.result.target_path}")
            print(f"checksum    {record.result.committed_sha256}")
            print(f"backup      {record.result.backup_artifact.artifact_id}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_receipt(args: argparse.Namespace) -> int:
    from ifc_console.core.results import ToolError

    core = _automation_core()
    try:
        record = core.transactions.get_commit(args.commit_id)
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"commit      {record.commit_id}")
            print(f"change set  {record.result.change_set_id}")
            print(f"target      {record.result.target_path}")
            print(f"checksum    {record.result.committed_sha256}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


def _cmd_changes_restore(args: argparse.Namespace) -> int:
    import asyncio

    from ifc_console.core.results import ToolError

    core = _automation_core(Mode.EDIT)

    async def run():
        model = Path(args.model).expanduser().resolve()
        core.start_audit()
        core.add_allowed_dir(model.parent)
        await core.open_model(model)
        submitted = await core.jobs.submit_restore(args.commit_id, confirm=args.confirm)
        completed = await core.jobs.wait(submitted.job_id)
        if completed.state.value != "succeeded":
            failure = completed.failure
            raise ToolError(
                failure.code if failure else "JOB_CANCELLED",
                failure.message if failure else "the restore job did not complete",
                failure.hint if failure else "Inspect the durable job record.",
            )
        return core.transactions.get_restore(str(completed.summary["restore_id"]))

    try:
        record = asyncio.run(run())
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"restore     {record.restore_id}")
            print(f"target      {record.result.target_path}")
            print(f"checksum    {record.result.restored_sha256}")
        return 0
    except ToolError as exc:
        return _tool_error(exc)
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- doctor
def _cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[dict[str, Any]] = []
    rc = 0

    def check(name: str, status: str, detail: str = "") -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    check("ifc-console", "ok", __version__)
    check("python", "ok", f"{platform.python_version()} ({platform.platform()})")
    try:
        import ifcopenshell

        check("ifcopenshell", "ok", str(ifcopenshell.version))
    except Exception as exc:
        check("ifcopenshell", "FAIL", f"{exc} (reinstall: uv sync / pip install ifc-console)")
        rc = 2
    for mod in ("mcp", "textual", "uvicorn"):
        try:
            from importlib.metadata import version

            check(mod, "ok", version(mod))
        except Exception as exc:
            check(mod, "FAIL", str(exc))
            rc = 2

    store = _new_store()
    try:
        store.ensure_dirs()
        home_ok = True
        check("home", "ok", str(store.home))
    except OSError as exc:
        home_ok = False
        check("home", "FAIL", f"{store.home} is not writable ({exc}); set IFC_CONSOLE_HOME")
        rc = rc or 2
    check(
        "settings",
        "ok" if not store.warnings else "warn",
        f"{store.user_file}" + (f" ({len(store.warnings)} warnings)" if store.warnings else ""),
    )
    if not home_ok:
        check("token", "warn", "skipped (home directory not writable)")
    elif store.settings.server.persistent_token:
        check("token", "ok", f"persistent, clients configure once ({store.token_file})")
    else:
        check("token", "ok", "per-run (server.persistent_token=false)")

    from ifc_console.viewer import assets as viewer_assets

    static_dir = viewer_assets.static_dir()
    wanted = ("index.html", "app.js", "vendor/web-ifc.wasm", "vendor/three.module.min.js")
    if static_dir is None:
        check("viewer assets", "optional", viewer_assets.INSTALL_HINT)
    elif missing := [n for n in wanted if not (static_dir / n).exists()]:
        check("viewer assets", "FAIL", f"missing: {', '.join(missing)}; reinstall the viewer extra")
        rc = rc or 2
    else:
        wasm_mb = (static_dir / "vendor/web-ifc.wasm").stat().st_size / 1_048_576
        check("viewer assets", "ok", f"{static_dir} (web-ifc.wasm {wasm_mb:.1f} MB)")

    mode = store.settings.sandbox.mode
    if mode == "off":
        check("sandbox", "warn", "off; generated code runs with in-process guards only")
    else:
        from ifc_console.sandbox.client import worker_executable

        check(
            "sandbox",
            "ok",
            f"{mode}; read-only code runs isolated "
            f"({store.settings.sandbox.memory_mb} MB cap, {Path(worker_executable()).name})",
        )

    from ifc_console.portcheck import FOREIGN, FREE, IFC_CONSOLE, conflict_hint, port_status

    port = store.settings.server.port
    probe_token = (
        store.load_server_token() if home_ok and store.settings.server.persistent_token else None
    )
    kind, detail = port_status(port, probe_token)
    if kind == FREE:
        check("port", "ok", f"{port} free")
    elif kind == IFC_CONSOLE:
        check("port", "ok", f"{port} in use by {detail}")
    elif kind == FOREIGN:
        # clients pointing at this port would hand their requests (and the
        # bearer token) to that application: worth a non-zero exit
        check("port", "FAIL", f"{port} in use by {detail}; {conflict_hint(kind, port)}")
        rc = rc or 2
    else:
        check("port", "warn", f"{port} in use by {detail}; {conflict_hint(kind, port)}")

    if args.file and rc == 0:
        path = Path(args.file).expanduser()
        if not path.exists():
            check("model", "FAIL", f"{path} does not exist")
            rc = 4
        else:
            try:
                import ifcopenshell

                t0 = time.perf_counter()
                f = ifcopenshell.open(str(path))
                dt = time.perf_counter() - t0
                products = len(f.by_type("IfcProduct"))
                check(
                    "model",
                    "ok",
                    f"{path.name}: {f.schema}, {products} products, parsed in {dt:.1f}s",
                )
            except Exception as exc:
                check("model", "FAIL", f"{exc}")
                rc = 4

    if args.json:
        print(json.dumps({"ok": rc == 0, "checks": checks}, indent=2))
    else:
        width = max(len(c["check"]) for c in checks)
        for c in checks:
            print(f"{c['check']:<{width}}  {c['status']:<4}  {c['detail']}")
        print()
        print("Client wiring (one-time; works whenever ifc-console is running):")
        if not store.settings.server.token_in_config_snippets:
            token = "<TOKEN>"
        elif store.settings.server.persistent_token:
            token = store.load_server_token()
        else:
            token = "<shown at startup>"
        cmd = _claude_code_http_cmd(store.settings.server.port, token)
        print("  " + cmd)
    return rc


# --------------------------------------------------------------------------- settings/recents/sessions
def _cmd_settings_list(args: argparse.Namespace) -> int:
    store = _new_store()
    flat = store.flat()
    if args.json:
        payload = (
            {k: {"value": v, "source": store.provenance.get(k, "default")} for k, v in flat.items()}
            if args.sources
            else flat
        )
        print(json.dumps(payload, indent=2, default=str))
        return 0
    width = max(len(k) for k in flat)
    for key, value in sorted(flat.items()):
        line = f"{key:<{width}}  {json.dumps(value, default=str)}"
        if args.sources:
            line += f"    [{store.provenance.get(key, 'default')}]"
        print(line)
    for w in store.warnings:
        print(f"warning: {w}", file=sys.stderr)
    return 0


def _cmd_settings_get(args: argparse.Namespace) -> int:
    store = _new_store()
    try:
        print(json.dumps(store.get(args.key), default=str))
        return 0
    except KeyError:
        print(f"error: unknown setting {args.key!r}", file=sys.stderr)
        return 3


def _cmd_settings_set(args: argparse.Namespace) -> int:
    store = _new_store()
    store.ensure_dirs()
    try:
        value = store.set_user(args.key, args.value)
    except KeyError:
        print(f"error: unknown setting {args.key!r}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"error: invalid value: {exc}", file=sys.stderr)
        return 3
    print(f"{args.key} = {json.dumps(value, default=str)}  (written to {store.user_file})")
    return 0


def _cmd_settings_unset(args: argparse.Namespace) -> int:
    store = _new_store()
    store.unset_user(args.key)
    print(f"{args.key} removed from {store.user_file}")
    return 0


def _cmd_settings_path(_args: argparse.Namespace) -> int:
    print(_new_store().user_file)
    return 0


def _cmd_recents_list(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.recents import RecentsStore

    entries = RecentsStore(store.recents_file).entries()
    if args.json:
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("(no recent models)")
        return 0
    for e in entries:
        size_mb = e.get("size_bytes", 0) / 1_048_576
        print(
            f"{e['path']}  ({size_mb:.1f} MB, {e.get('schema', '?')}, "
            f"last {e.get('last_opened', '?')})"
        )
    return 0


def _cmd_recents_clear(_args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.recents import RecentsStore

    RecentsStore(store.recents_file).clear()
    print("recents cleared")
    return 0


def _cmd_sessions_list(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    ids = AuditLog(store.sessions_dir).list_sessions()
    if args.json:
        print(json.dumps(ids, indent=2))
    else:
        print("\n".join(ids) if ids else "(no sessions)")
    return 0


def _cmd_sessions_show(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    records = AuditLog(store.sessions_dir).read_session(args.id)
    if not records:
        print(f"error: no session {args.id!r}", file=sys.stderr)
        return 4
    for record in records:
        print(json.dumps(record, ensure_ascii=False, default=str))
    return 0


def _cmd_sessions_verify(args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    result = AuditLog(store.sessions_dir).verify_session(args.id)
    if args.json:
        print(result.model_dump_json(indent=2))
    elif result.valid:
        print(f"valid: {result.event_count} chained event(s)")
    else:
        print(f"invalid: {result.error}", file=sys.stderr)
    return 0 if result.valid else 5


def _cmd_sessions_clear(_args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    n = AuditLog(store.sessions_dir).clear()
    print(f"removed {n} session(s)")
    return 0


# --------------------------------------------------------------------------- knowledge
def _knowledge():
    from ifc_console.knowledge import KnowledgeBase

    store = _new_store()
    store.ensure_dirs()
    return KnowledgeBase(store.home, schemas=tuple(store.settings.knowledge.schemas))


def _cmd_knowledge_build(args: argparse.Namespace) -> int:
    kb = _knowledge()
    print(f"building the reference index at {kb.path} …")
    info = kb.build(force=args.force)
    if not info.get("built"):
        print("already built; --force rebuilds it")
        return 0
    counts = ", ".join(f"{k} {v}" for k, v in sorted(info["counts"].items()))
    print(f"indexed {info['total']} records ({counts}), {info['size_bytes'] / 1e6:.1f} MB")
    return 0


def _cmd_knowledge_status(args: argparse.Namespace) -> int:
    kb = _knowledge()
    stats = kb.stats()
    if args.json:
        print(json.dumps({"path": str(kb.path), **stats}, indent=2, default=str))
        return 0
    if not stats["ready"]:
        print(f"not built ({kb.path})\nbuild it with: ifc-console knowledge build")
        return 1
    counts = ", ".join(f"{k} {v}" for k, v in sorted(stats["counts"].items()))
    print(f"index    {kb.path}")
    print(f"records  {stats['total']} ({counts})")
    print(f"search   {stats['search']}   ifcopenshell {stats.get('ifcopenshell', '?')}")
    return 0


def _cmd_knowledge_search(args: argparse.Namespace) -> int:
    kb = _knowledge()
    if not kb.ready:
        print("the index is not built; run: ifc-console knowledge build")
        return 1
    hits = kb.search(
        " ".join(args.query),
        kind=tuple(args.kind) if args.kind else None,
        schema=args.schema,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
        return 0
    if not hits:
        print("no matches")
        return 1
    for hit in hits:
        schema = f" [{hit['schema']}]" if hit.get("schema") else ""
        print(f"{hit['kind']:9} {hit['name']}{schema}\n    {hit['summary'][:100]}")
    return 0


# --------------------------------------------------------------------------- plugins
def _print_plugin_records(records, *, as_json: bool) -> None:
    payload = [record.model_dump(mode="json") for record in records]
    if as_json:
        print(json.dumps({"plugins": payload}, indent=2))
        return
    if not payload:
        print("no ifc-console plugins are installed")
        return
    for record in payload:
        version = (record.get("manifest") or {}).get("version")
        suffix = f" {version}" if version else ""
        detail = ", ".join(record.get("operations") or ())
        if record.get("error"):
            detail = record["error"]
        print(f"{record['name']}{suffix}: {record['status']}" + (f" ({detail})" if detail else ""))


def _cmd_plugins_list(args: argparse.Namespace) -> int:
    from ifc_console.plugins import PluginManager

    store = _new_store()
    allow = {name.strip().lower() for name in store.settings.plugins.allow if name.strip()}
    records = PluginManager().inventory(
        enabled=store.settings.plugins.enabled,
        allow=allow,
    )
    _print_plugin_records(records, as_json=args.json)
    return 0


def _cmd_plugins_doctor(args: argparse.Namespace) -> int:
    from ifc_console.app import AppCore
    from ifc_console.application.operations import build_operations

    store = _new_store()
    core = AppCore(store, transport="embedded")
    core.start_audit()
    try:
        build_operations(core)
        records = core.plugins.records
        _print_plugin_records(records, as_json=args.json)
        return 1 if any(record.status in {"error", "missing"} for record in records) else 0
    finally:
        core.shutdown()


# --------------------------------------------------------------------------- entry
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    try:
        if func is None:
            return _cmd_interactive(args)
        return func(args)
    except KeyboardInterrupt:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
