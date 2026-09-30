"""Engine sessions for one console: one per thread, reaped when idle.

Lives on the panel state and never touches the panel runtime; an engine
process is the only thing here that is ever closed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from pathlib import Path
from typing import Any

from ifc_console.agents.harness.acp import AcpSession
from ifc_console.agents.harness.engine import (
    EngineRegistry,
    EngineSpec,
    engine_environment,
    mcp_servers_for,
)
from ifc_console.agents.harness.workspace import (
    STDERR_LOG,
    prepare_run_directory,
    release_run_directory,
    run_directory,
    sweep_run_directories,
)

log = logging.getLogger("ifc-console.agents.harness")

_REAP_INTERVAL_S = 30.0


class HarnessRuntime:
    """Spawns, hands out, and eventually closes engine sessions."""

    def __init__(self, core: Any) -> None:
        self.core = core
        self.sessions: dict[str, AcpSession] = {}
        self._reaper: asyncio.Task[Any] | None = None
        self._closing: set[asyncio.Task[Any]] = set()

    @property
    def home(self) -> Path:
        return Path(self.core.store.home)

    def registry(self) -> EngineRegistry:
        return EngineRegistry.for_core(self.core)

    @staticmethod
    def _key(engine: EngineSpec, thread_id: str) -> str:
        return f"{engine.name}:{thread_id}"

    async def session_for(
        self, engine: EngineSpec, thread_id: str, *, instructions: str = ""
    ) -> tuple[AcpSession, bool]:
        """The live session for a thread, spawning one when needed.

        Returns the session and whether it was just created, which is when
        the caller has to send the instructions and any recap.
        """
        key = self._key(engine, thread_id)
        session = self.sessions.get(key)
        if session is not None and session.alive:
            return session, False
        if session is not None:
            self.sessions.pop(key, None)
            await session.close()
        file_text = instructions if engine.instructions in ("file", "both") else ""
        core = self.core
        # Disk writes and keyring reads block; keep them off the event loop.
        run_dir, env = await asyncio.gather(
            asyncio.to_thread(
                prepare_run_directory,
                self.home,
                thread_id,
                instructions=file_text,
                instructions_file=engine.instructions_file,
            ),
            asyncio.to_thread(engine_environment, core, engine),
        )
        cwd = run_dir
        if engine.cwd == "project":
            cwd = Path(self.core.store.project_dir)
        elif engine.cwd == "home":
            cwd = self.home
        session = AcpSession(
            engine,
            thread_id=thread_id,
            cwd=cwd,
            env=env,
            mcp_servers=lambda http: mcp_servers_for(core, engine, http_supported=http),
            stderr_path=run_dir / STDERR_LOG,
        )
        await session.start()
        core.audit.record(
            "harness_session_started",
            engine=engine.name,
            command=engine.command,
            args=list(engine.args),
            thread=thread_id,
            cwd=str(cwd),
        )
        self.sessions[key] = session
        self._ensure_reaper()
        return session, True

    async def discard(self, session: AcpSession) -> None:
        """Drop a session whose process failed; the next prompt respawns."""
        key = self._key(session.engine, session.thread_id)
        if self.sessions.get(key) is session:
            self.sessions.pop(key, None)
        await session.close()

    def release_thread(self, thread_id: str) -> None:
        """Close every session of one thread. Safe to call from sync code."""
        doomed = [s for s in self.sessions.values() if s.thread_id == thread_id]
        for session in doomed:
            self.sessions.pop(self._key(session.engine, session.thread_id), None)
        if not doomed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            for session in doomed:
                session.kill_sync()
            release_run_directory(self.home, thread_id)
            return
        task = loop.create_task(self._close_many(doomed, thread_id))
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    async def _close_many(self, sessions: list[AcpSession], thread_id: str) -> None:
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()
            self.core.audit.record(
                "harness_session_closed", engine=session.engine.name, thread=thread_id
            )
        release_run_directory(self.home, thread_id)

    def _ensure_reaper(self) -> None:
        if self._reaper is not None and not self._reaper.done():
            return
        self._reaper = asyncio.get_running_loop().create_task(self._reap())

    async def _reap(self) -> None:
        while self.sessions:
            await asyncio.sleep(_REAP_INTERVAL_S)
            now = time.monotonic()
            for key, session in list(self.sessions.items()):
                idle = now - session.last_used
                if session.busy or idle < session.engine.idle_timeout_s:
                    continue
                self.sessions.pop(key, None)
                with contextlib.suppress(Exception):
                    await session.close()
                self.core.audit.record(
                    "harness_session_idle_closed",
                    engine=session.engine.name,
                    thread=session.thread_id,
                    idle_s=round(idle),
                )
            active = {run_directory(self.home, s.thread_id).name for s in self.sessions.values()}
            with contextlib.suppress(Exception):
                sweep_run_directories(
                    self.home, keep=int(self.core.settings.harness.keep_run_dirs), active=active
                )

    async def close_all(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            self._reaper = None
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            with contextlib.suppress(Exception):
                await session.close()

    def close_all_sync(self) -> None:
        """Console shutdown: kill what is left, nothing is awaited."""
        if self._reaper is not None:
            with contextlib.suppress(RuntimeError):
                self._reaper.cancel()
            self._reaper = None
        sessions = list(self.sessions.values())
        self.sessions.clear()
        for session in sessions:
            session.kill_sync()


__all__ = ["HarnessRuntime"]
