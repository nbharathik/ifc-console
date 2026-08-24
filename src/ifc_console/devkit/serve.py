"""`ifc-console dev`: boot the panel against a demo project, or check it.

Two jobs, one setup. ``--check`` runs the headless feature checklist and never
touches a browser; without it exactly one tab opens on the surface asked for.
The state directory is isolated from the user's real console home so a
rehearsal cannot disturb settings, keys, or recents.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8393
_STARTUP_TIMEOUT = 30.0


@dataclass
class DevServer:
    core: Any
    server: Any
    thread: threading.Thread
    base_url: str
    token: str

    @property
    def viewer_url(self) -> str:
        return f"{self.base_url}/viewer#t={self.token}"

    @property
    def chat_url(self) -> str:
        return f"{self.base_url}/viewer?chat=1#t={self.token}"

    @property
    def solo_chat_url(self) -> str:
        return f"{self.base_url}/chat#t={self.token}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        self.core.shutdown()


def _dev_home(project_dir: Path, home: str | Path | None) -> Path:
    return Path(home).expanduser().resolve() if home else project_dir / ".ifc-console-dev-home"


def build_dev_core(
    project_dir: str | Path,
    *,
    port: int = DEFAULT_PORT,
    home: str | Path | None = None,
    model: str | Path | None = None,
) -> tuple[Any, Any]:
    """An AppCore on an isolated home with the demo scenario loaded."""
    from ifc_console.devkit.rehearsal import REHEARSAL_ID, enable_rehearsal_provider
    from ifc_console.devkit.scenario import build_scenario

    root = Path(project_dir).expanduser().resolve()
    scenario = build_scenario(root, model=model)
    dev_home = _dev_home(root, home)
    os.environ["IFC_CONSOLE_HOME"] = str(dev_home)
    enable_rehearsal_provider()

    from ifc_console.app import AppCore
    from ifc_console.settings import SettingsStore

    store = SettingsStore(
        home=dev_home,
        project_dir=root,
        flag_overrides={"server.port": port, "mode.default": "ask"},
        include_project=True,
    )
    store.ensure_dirs()
    core = AppCore(store, port=port, transport="http", viewer=True, chat=True)
    core.chat.provider = REHEARSAL_ID
    core.chat.model = "rehearsal-tools"
    core.add_allowed_dir(root)
    core.start_audit()
    core.start_knowledge()
    asyncio.run(core.open_model(scenario.model))
    try:
        core.agent_files.sync(core.project_knowledge)
    except Exception as exc:  # a missing PDF dependency must not stop the boot
        scenario = scenario.__class__(
            project_dir=scenario.project_dir,
            model=scenario.model,
            references=scenario.references,
            recipe=scenario.recipe,
            notes=(*scenario.notes, f"reference indexing failed: {exc}"),
        )
    return core, scenario


def start(core: Any) -> DevServer:
    """Run the HTTP app on a background thread and wait for it to bind."""
    from ifc_console.mcp.server import build_http_app, build_mcp, make_uvicorn_server

    mcp = build_mcp(core)
    app = build_http_app(core, mcp)
    server = make_uvicorn_server(app, core.port)

    failure: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(server.serve())
        except BaseException as exc:  # reported by the caller, never swallowed
            failure.append(exc)

    thread = threading.Thread(target=run, name="ifc-console-dev", daemon=True)
    thread.start()
    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            break
        if failure or not thread.is_alive():
            raise RuntimeError(f"the dev server did not start: {failure[0] if failure else 'exited'}")
        time.sleep(0.05)
    else:
        raise RuntimeError("the dev server did not start within 30 seconds")
    return DevServer(
        core=core,
        server=server,
        thread=thread,
        base_url=f"http://127.0.0.1:{core.port}",
        token=core.token,
    )


def _open_once(url: str) -> bool:
    """One tab, once. Repeated tabs are the reason UI checks feel expensive."""
    import webbrowser

    try:
        return webbrowser.open(url)
    except Exception:
        return False


def run_dev(
    *,
    project_dir: str | Path,
    port: int = DEFAULT_PORT,
    home: str | Path | None = None,
    model: str | Path | None = None,
    open_target: str = "none",
) -> int:
    """Boot the rehearsal console and serve until interrupted.

    At most one browser tab is ever opened, and only when open_target names a
    surface. The headless checklist lives in devkit.checks and never gets here.
    """
    core, scenario = build_dev_core(project_dir, port=port, home=home, model=model)
    try:
        dev = start(core)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        core.shutdown()
        return 2

    print(f"project      : {scenario.project_dir}")
    print(f"model        : {scenario.model.name}")
    print(f"references   : {', '.join(path.name for path in scenario.references) or '(none)'}")
    for note in scenario.notes:
        print(f"note         : {note}")
    print("provider     : rehearsal (offline, no key, no network)")
    print()
    print(f"viewer + chat: {dev.chat_url}")
    print(f"3D only      : {dev.viewer_url}")
    print(f"chat only    : {dev.solo_chat_url}")

    urls = {"chat": dev.chat_url, "viewer": dev.viewer_url, "solo": dev.solo_chat_url}
    if open_target in urls:
        opened = _open_once(urls[open_target])
        print(
            f"opened one tab on the {open_target} surface"
            if opened
            else "could not open a browser tab; copy a URL above"
        )
    else:
        print("no browser tab opened; copy a URL above, or pass --open chat")
    print("Ctrl+C to stop.")
    try:
        while dev.thread.is_alive():
            dev.thread.join(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        dev.stop()
    return 0


__all__ = ["DEFAULT_PORT", "DevServer", "build_dev_core", "run_dev", "start"]
