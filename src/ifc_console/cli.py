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
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifc-console",
        description=(
            "A terminal interface to connect IFC files to LLMs: a standalone MCP server. "
            "With no subcommand, starts the interactive console (slash commands: "
            "/file, /mode, /viewer, /connect, /help)."
        ),
    )
    parser.add_argument("--version", action="version", version=_version_line())
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

    cfg = sub.add_parser("mcp-config", help="Print client wiring snippets.")
    cfg.add_argument(
        "--client",
        choices=["claude-code", "claude-desktop", "cursor", "vscode", "codex"],
        default="claude-code",
    )
    cfg.add_argument("--transport", choices=["http", "stdio"], default=None)
    cfg.add_argument("--file", default=None, help="Model path to pin in stdio snippets.")
    cfg.add_argument("--mode", choices=_MODES, default=None)
    cfg.add_argument("--port", type=int, default=None)
    cfg.set_defaults(func=_cmd_mcp_config)

    doctor = sub.add_parser("doctor", help="Diagnose the environment.")
    doctor.add_argument("--file", default=None, help="Also parse this IFC file.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)

    check = sub.add_parser(
        "check", help="Validate a model for CI: schema plus optional IDS files."
    )
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
    check.add_argument(
        "--output", default=None, metavar="FILE", help="Write the report to a file."
    )
    check.set_defaults(func=_cmd_check)

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

    ses = sub.add_parser("sessions", help="Audit-log sessions.")
    ses_sub = ses.add_subparsers(dest="sessions_cmd", required=True)
    ses_list = ses_sub.add_parser("list")
    ses_list.add_argument("--json", action="store_true")
    ses_list.set_defaults(func=_cmd_sessions_list)
    ses_show = ses_sub.add_parser("show")
    ses_show.add_argument("id")
    ses_show.set_defaults(func=_cmd_sessions_show)
    ses_clear = ses_sub.add_parser("clear")
    ses_clear.set_defaults(func=_cmd_sessions_clear)

    return parser


def _add_run_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", default=None, help="IFC file to load at startup.")
    parser.add_argument("--mode", choices=_MODES, default=None, help="Session mode.")
    parser.add_argument("--port", type=int, default=None, help="MCP HTTP port (default 8383).")
    parser.add_argument(
        "--viewer",
        action="store_true",
        help="Enable the local 3D web viewer (needs the HTTP server: TUI or --http).",
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
def _make_store(args: argparse.Namespace) -> SettingsStore:
    overrides: dict[str, Any] = {}
    if getattr(args, "port", None):
        overrides["server.port"] = args.port
    if getattr(args, "mode", None):
        overrides["mode.default"] = args.mode
    if getattr(args, "log_level", None):
        overrides["logging.level"] = args.log_level
    return _new_store(flag_overrides=overrides)


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
    from ifc_console.app import AppCore

    return AppCore(
        store,
        mode=mode,
        port=getattr(args, "port", None),
        extra_allowed_dirs=extra,
        transport=transport,
        viewer=viewer_flag,
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


def _setup_logging(store: SettingsStore, *, level: str) -> None:
    _ensure_home(store)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if store.settings.logging.file_enabled:
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
    core.start_audit()
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
    core.start_audit()
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


def build_config_snippet(
    client: str,
    transport: str | None,
    *,
    port: int,
    file: str | None,
    mode: str,
    token: str | None,
) -> str:
    stdio_args = ["ifc-console", "serve", "--stdio", "--mode", mode]
    if file:
        stdio_args += ["--file", file]
    url = f"http://127.0.0.1:{port}/mcp"
    headers = {"Authorization": f"Bearer {token or '<TOKEN>'}"}

    # Every default snippet attaches to the reusable terminal-owned session.
    # A model path belongs only to an explicitly requested standalone stdio
    # process; HTTP clients follow whatever the user opens with /file.
    transport = transport or "http"

    if client == "claude-code":
        if transport == "http":
            return _claude_code_http_cmd(port, token or "<TOKEN>")
        argv = ["uvx", *stdio_args]
        if platform.system() == "Windows":
            import subprocess

            command = subprocess.list2cmdline(argv)
        else:
            command = shlex.join(argv)
        return f"claude mcp add --scope user ifc-console -- {command}"
    if client == "claude-desktop":
        if transport == "stdio":
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
        if transport == "stdio":
            snippet = {"mcpServers": {"ifc-console": {"command": "uvx", "args": stdio_args}}}
        else:
            snippet = {"mcpServers": {"ifc-console": {"url": url, "headers": headers}}}
        return json.dumps(snippet, indent=2)
    if client == "vscode":
        if transport == "stdio":
            snippet = {
                "servers": {"ifc-console": {"type": "stdio", "command": "uvx", "args": stdio_args}}
            }
        else:
            snippet = {"servers": {"ifc-console": {"type": "http", "url": url, "headers": headers}}}
        return json.dumps(snippet, indent=2)
    if client == "codex":
        if transport == "stdio":
            arg_list = ", ".join(json.dumps(a, ensure_ascii=False) for a in stdio_args)
            return f'[mcp_servers.ifc-console]\ncommand = "uvx"\nargs = [{arg_list}]'
        authorization = json.dumps(headers["Authorization"])
        return (
            f"[mcp_servers.ifc-console]\n"
            f"url = {json.dumps(url)}\n"
            f"http_headers = {{ Authorization = {authorization} }}"
        )
    raise ValueError(client)


def _cmd_mcp_config(args: argparse.Namespace) -> int:
    store = _make_store(args)
    port = args.port or store.settings.server.port
    mode = args.mode or store.settings.mode.default
    # With the (default) persistent token the snippet is complete and stays
    # valid across restarts: configure the client once, then just run ifc-console.
    token = None
    if store.settings.server.persistent_token and store.settings.server.token_in_config_snippets:
        token = store.load_server_token()
    snippet = build_config_snippet(
        args.client, args.transport, port=port, file=args.file, mode=mode, token=token
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
    elif args.transport != "stdio":
        print(
            "note: configure once; this HTTP setup follows whichever model you "
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
    from ifc_console.mcp.envelope import ToolError

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

    from ifc_console.viewer.routes import STATIC_DIR

    wanted = ("index.html", "app.js", "vendor/web-ifc.wasm", "vendor/three.module.min.js")
    missing = [n for n in wanted if not (STATIC_DIR / n).exists()]
    if missing:
        check("viewer assets", "FAIL", f"missing: {', '.join(missing)}; reinstall ifc-console")
        rc = rc or 2
    else:
        wasm_mb = (STATIC_DIR / "vendor/web-ifc.wasm").stat().st_size / 1_048_576
        check("viewer assets", "ok", f"{STATIC_DIR} (web-ifc.wasm {wasm_mb:.1f} MB)")

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


def _cmd_sessions_clear(_args: argparse.Namespace) -> int:
    store = _new_store()
    from ifc_console.audit import AuditLog

    n = AuditLog(store.sessions_dir).clear()
    print(f"removed {n} session(s)")
    return 0


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
