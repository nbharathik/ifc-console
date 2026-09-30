"""The doctor command: diagnose the environment."""

from __future__ import annotations

import argparse
import json
import platform
import time
from pathlib import Path
from typing import Any

from ifc_console import __version__
from ifc_console.cli.common import _new_store
from ifc_console.cli.connect import _claude_code_http_cmd


def add_doctor_parser(sub: argparse._SubParsersAction) -> None:
    doctor = sub.add_parser("doctor", help="Diagnose the environment.")
    doctor.add_argument("--file", default=None, help="Also parse this IFC file.")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_cmd_doctor)


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

    agents_extra = "pip install 'ifc-console[agents]'"
    for label, module_name, distribution, install_hint in (
        ("PDF", "pypdfium2", "pypdfium2", agents_extra),
        ("secure keys", "keyring", "keyring", agents_extra),
        ("agent engines", "acp", "agent-client-protocol", agents_extra),
        ("IDS validation", "ifctester", "ifctester", "pip install 'ifc-console[validation]'"),
    ):
        try:
            from importlib import import_module
            from importlib.metadata import version

            import_module(module_name)
            check(label, "ok", version(distribution))
        except Exception as exc:
            check(label, "warn", f"not installed ({exc}); optional: {install_hint}")

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
    if missing := [n for n in wanted if not (static_dir / n).exists()]:
        check(
            "viewer assets",
            "FAIL",
            f"missing: {', '.join(missing)}; reinstall ifc-console",
        )
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
