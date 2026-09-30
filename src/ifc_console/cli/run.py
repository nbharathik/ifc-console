"""Run modes: the interactive console, servers, the stdio bridge, and the dev harness."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import suppress
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from ifc_console import __version__
from ifc_console.cli.common import (
    _add_run_flags,
    _agent_module,
    _make_core,
    _make_store,
    _new_store,
    _port,
    _setup_logging,
)
from ifc_console.cli.connect import _claude_code_http_cmd

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console")


def add_serve_parser(sub: argparse._SubParsersAction) -> None:
    serve = sub.add_parser("serve", help="Run the server without the launcher UI.")
    group = serve.add_mutually_exclusive_group(required=True)
    group.add_argument("--stdio", action="store_true", help="stdio transport (client-managed).")
    group.add_argument("--http", action="store_true", help="HTTP daemon (same as --no-tui).")
    _add_run_flags(serve, suppress_defaults=True)
    serve.set_defaults(func=_cmd_serve)


def add_bridge_parser(sub: argparse._SubParsersAction) -> None:
    bridge = sub.add_parser(
        "bridge",
        help="stdio proxy to a running console; survives being started first.",
    )
    bridge.add_argument(
        "--port", type=_port, default=None, help="Console port (default from settings)."
    )
    bridge.add_argument("--token", default=None, help="Defaults to the persistent machine token.")
    bridge.add_argument(
        "--tools",
        choices=["full", "lean"],
        default=None,
        help="Tool profile to list (default from settings): lean lists a small core.",
    )
    bridge.set_defaults(func=_cmd_bridge)


def add_dev_parser(sub: argparse._SubParsersAction) -> None:
    dev = sub.add_parser(
        "dev",
        help="Rehearse the browser panel against a demo project, or check it headlessly.",
    )
    dev.add_argument(
        "--project",
        default=None,
        metavar="DIR",
        help="Where the demo project lives (default: a reusable folder under the temp dir).",
    )
    dev.add_argument("--file", default=None, help="Use this IFC file instead of the demo model.")
    dev.add_argument("--port", type=_port, default=None, help="Dev server port (default 8393).")
    dev.add_argument(
        "--check",
        action="store_true",
        help="Run the headless feature checklist and exit. Opens no browser tab.",
    )
    dev.add_argument(
        "--open",
        dest="open_target",
        choices=["agent", "viewer", "none"],
        default=None,
        help=(
            "Which single surface to open. At most one tab, ever. Defaults to the "
            "Agent workspace on a terminal and to none when output is piped or checked."
        ),
    )
    dev.add_argument(
        "--fresh",
        action="store_true",
        help=(
            "Delete and rebuild only the default temporary demo project before starting. "
            "Cannot be combined with --project."
        ),
    )
    dev.add_argument(
        "--keep",
        action="store_true",
        help="With --check, keep serving after the checks so you can look at the panel.",
    )
    dev.add_argument("--json", action="store_true", help="With --check, print JSON results.")
    dev.set_defaults(func=_cmd_dev)


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

    # ui=True: textual is warmed alongside settings/AppCore instead of after.
    preload.start(ui=True)
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
    try:
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
        if not getattr(server, "started", False):
            # bind lost a race after the pre-check; uvicorn already logged why
            print(f"error: the server never came up on port {core.port}.", file=sys.stderr)
            return 1
        return 0
    finally:
        core.shutdown()


def _cmd_serve(args: argparse.Namespace) -> int:
    if args.http:
        return _run_headless_http(args)
    # stdio: stdout belongs to the protocol; logs go to stderr + file only
    from ifc_console import preload

    preload.start()
    store = _make_store(args)
    _setup_logging(store, level=store.settings.logging.level)
    core = _make_core(args, store, transport="stdio")
    try:
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
        with suppress(KeyboardInterrupt):
            mcp.run(transport="stdio")
        return 0
    finally:
        core.shutdown()


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
    profile = getattr(args, "tools", None)
    cache_name = f"tools_cache-{profile}.json" if profile else "tools_cache.json"
    bridge = Bridge(
        f"http://127.0.0.1:{port}/mcp",
        token,
        cache_file=store.home / cache_name,
        profile=profile,
    )
    log.info("ifc-console %s bridge to port %s", __version__, port)
    try:
        return bridge.run()
    except KeyboardInterrupt:
        return 0


def _cmd_dev(args: argparse.Namespace) -> int:
    """Rehearse or check the browser panel without a provider key."""
    import json as _json
    import tempfile

    serve = _agent_module("devkit.serve")

    temp_root = Path(tempfile.gettempdir()).resolve()
    default_project = temp_root / "ifc-console-dev-project"
    explicit_project = args.project is not None
    project = Path(args.project).expanduser().resolve() if explicit_project else default_project
    port = args.port or serve.DEFAULT_PORT
    if args.fresh:
        if explicit_project:
            print(
                "error: --fresh only resets the disposable temporary demo; "
                "omit --project or choose a new empty --project directory",
                file=sys.stderr,
            )
            return 3
        import shutil

        # Resolve the final target before recursively deleting it. A replaced
        # temp child (for example a symlink) must not redirect the reset into a
        # real project elsewhere on disk.
        reset_target = default_project.resolve()
        if reset_target.parent != temp_root or reset_target.name != default_project.name:
            print(
                "error: the temporary dev project resolves outside the temp directory; "
                "refusing to reset it",
                file=sys.stderr,
            )
            return 2
        try:
            shutil.rmtree(reset_target)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(
                f"error: could not reset the temporary dev project: {exc}",
                file=sys.stderr,
            )
            return 2
    _setup_logging(_new_store(), level="warning", to_file=False)
    # A browser tab is a deliberate act, not a side effect of running checks.
    open_target = args.open_target
    if open_target is None:
        open_target = "none" if (args.check or not sys.stdout.isatty()) else "agent"

    if not args.check:
        return serve.run_dev(
            project_dir=project,
            port=port,
            model=args.file,
            open_target=open_target,
        )

    checks = _agent_module("devkit.checks")

    core, scenario = serve.build_dev_core(project, port=port, model=args.file)
    try:
        dev = serve.start(core)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        core.shutdown()
        return 2
    try:
        run = checks.run_checks(dev.base_url, dev.token)
        if args.json:
            print(
                _json.dumps(
                    {
                        "project": str(scenario.project_dir),
                        "passed": run.passed,
                        "checks": [
                            {"name": c.name, "status": c.status, "detail": c.detail}
                            for c in run.checks
                        ],
                    },
                    indent=2,
                )
            )
        else:
            print(f"project: {scenario.project_dir}")
            for note in scenario.notes:
                print(f"note   : {note}")
            print()
            print(checks.render(run))
        if args.keep:
            print(f"\nserving: {dev.agent_url}\nCtrl+C to stop.")
            try:
                while dev.thread.is_alive():
                    dev.thread.join(timeout=0.5)
            except KeyboardInterrupt:
                pass
        return 0 if run.passed else 1
    finally:
        dev.stop()
