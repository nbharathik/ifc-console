"""Decides whether a code run goes in the sandbox, and drives it if so.

The honest boundary, stated once so the rest of the code can be short:

- Non-mutating runs go to the worker. That is every run in ask mode, and
  query-classified code in edit mode. The worker reads the model from disk,
  so it can only be used when the in-memory model matches the file.
- Mutating runs stay in-process, because the edit has to land in the model
  the rest of the console is holding. Those runs already required the user
  to turn on edit mode by hand.

`sandbox.mode` picks what happens when the sandbox cannot be used: `auto`
falls back to the in-process guards and says so, `strict` refuses the run.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ifc_console.sandbox.client import (
    SandboxError,
    SandboxNotReady,
    SandboxProcess,
    SandboxTimeout,
)
from ifc_console.sandbox.policy import SandboxPolicy

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.session.model import ModelSession

_MB = 1_048_576


@dataclass
class Decision:
    use: bool
    reason: str


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    result_repr: str | None = None
    stdout_truncated: bool = False
    # The classifier and the guards both missed a mutation, and the sandbox
    # copy absorbed it. The console's model is untouched.
    contained: bool = False
    kind: str = ""
    message: str = ""
    traceback: str | None = None
    info: dict[str, Any] = field(default_factory=dict)


class SandboxRunner:
    """Owns at most one worker process for the lifetime of an AppCore."""

    def __init__(self, core: AppCore) -> None:
        self.core = core
        self._lock = asyncio.Lock()
        self._process: SandboxProcess | None = None
        self._scratch: Path | None = None
        self._dirs_key: tuple[str, ...] = ()
        self.last_error: str | None = None
        # Subscribe unconditionally: warm_on_load is re-read per event, so
        # /settings can turn warming on without a restart.
        core.events.subscribe(self._on_event)

    # -- configuration -----------------------------------------------------------
    @property
    def settings(self):
        return self.core.settings.sandbox

    @property
    def enabled(self) -> bool:
        return self.settings.mode != "off"

    @property
    def strict(self) -> bool:
        return self.settings.mode == "strict"

    def _policy(self, scratch: Path) -> SandboxPolicy:
        return SandboxPolicy.build(
            read_dirs=list(self.core.allowed_dirs),
            scratch_dir=scratch,
            deny_dirs=[self.core.store.home],
            memory_mb=self.settings.memory_mb,
        )

    def _scratch_dir(self) -> Path:
        if self._scratch is None:
            import os
            import secrets

            self._scratch = (
                self.core.store.home / "sandbox" / f"{os.getpid()}-{secrets.token_hex(4)}"
            )
        return self._scratch

    # -- the decision ----------------------------------------------------------------
    def decide(self, session: ModelSession, *, mutating: bool) -> Decision:
        if not self.enabled:
            return Decision(False, "sandbox.mode is off")
        if mutating:
            return Decision(False, "mutating code must run against the live model, not a copy")
        if not session.loaded or session.path is None:
            return Decision(False, "no model is loaded")
        if session.dirty:
            return Decision(False, "the model has unsaved changes the sandbox copy cannot see")
        if session.tainted:
            return Decision(False, "the in-memory model has diverged from the file")
        limit = self.settings.max_model_mb
        if limit and session.size_bytes > limit * _MB:
            return Decision(
                False,
                f"the model is over the {limit} MB sandbox budget (sandbox.max_model_mb)",
            )
        return Decision(True, "")

    # -- running ------------------------------------------------------------------------
    async def run(
        self,
        code: str,
        *,
        session: ModelSession,
        output_limit: int,
        timeout: float,
        extra_system_modules: tuple[str, ...] = (),
    ) -> SandboxResult:
        """Execute in the worker. Raises SandboxError if the worker cannot serve."""
        async with self._lock:
            process = await self._ensure_ready(session)
            request = {
                "op": "run",
                "code": code,
                "output_limit": output_limit,
                "allowed_dirs": [str(d) for d in self.core.allowed_dirs],
                "extra_system_modules": list(extra_system_modules),
            }
            start = time.perf_counter()
            try:
                reply = await asyncio.to_thread(process.request, request, timeout=timeout)
            except (SandboxTimeout, SandboxError):
                # Drop the reference only after killing it, or the child and
                # its job-object handle outlive the console.
                await self._stop(process)
                self._process = None
                raise
            duration_ms = int((time.perf_counter() - start) * 1000)

        info = {"duration_ms": duration_ms, **process.info}
        if not reply.get("ok"):
            return SandboxResult(
                ok=False,
                kind=str(reply.get("kind") or "error"),
                message=str(reply.get("message") or "the sandbox refused the run"),
                traceback=reply.get("traceback"),
                info=info,
            )
        before, after = reply.get("max_id_before"), reply.get("max_id_after")
        contained = isinstance(before, int) and isinstance(after, int) and after > before
        return SandboxResult(
            ok=True,
            stdout=reply.get("stdout") or "",
            result_repr=reply.get("result"),
            stdout_truncated=bool(reply.get("stdout_truncated")),
            contained=contained,
            info=info,
        )

    async def _ensure_ready(self, session: ModelSession) -> SandboxProcess:
        """A live worker holding the same file the console holds."""
        dirs_key = tuple(str(d) for d in self.core.allowed_dirs)
        if self._process is not None and (not self._process.alive or dirs_key != self._dirs_key):
            await self._stop(self._process)
            self._process = None

        if self._process is None:
            scratch = self._scratch_dir()
            process = SandboxProcess(self._policy(scratch), scratch)
            try:
                await asyncio.to_thread(process.start, self.settings.startup_timeout)
            except SandboxTimeout as exc:
                self.last_error = str(exc)
                raise SandboxNotReady(
                    f"the sandbox worker did not start within "
                    f"{self.settings.startup_timeout:.0f}s (sandbox.startup_timeout)"
                ) from exc
            except SandboxError as exc:
                self.last_error = str(exc)
                raise
            self.last_error = None
            self._process = process
            self._dirs_key = dirs_key
            self.core.audit.record(
                "sandbox_start", pid=process.info.get("pid"), controls=process.info.get("controls")
            )

        process = self._process
        key = f"{session.path}|{session.fingerprint}|{session.revision}"
        if process.loaded_key != key:
            try:
                reply = await asyncio.to_thread(
                    process.request,
                    {"op": "load", "path": str(session.path), "key": key},
                    timeout=self.settings.load_timeout,
                )
            except SandboxTimeout as exc:
                process.loaded_key = None
                self.last_error = str(exc)
                raise SandboxNotReady(
                    f"the sandbox did not finish reading the model within "
                    f"{self.settings.load_timeout:.0f}s (sandbox.load_timeout)"
                ) from exc
            # A refused load leaves the worker holding ifc = None. Caching the
            # key here would make every later run answer from an empty model.
            if not reply.get("ok"):
                process.loaded_key = None
                self.last_error = str(
                    reply.get("message") or "the sandbox could not read the model"
                )
                raise SandboxError(self.last_error)
            process.loaded_key = key
        return process

    # -- warming ---------------------------------------------------------------------------
    def _on_event(self, event: dict) -> None:
        if event.get("type") not in ("model_loaded", "active_model_changed"):
            return
        if not self.enabled or not self.settings.warm_on_load:
            return
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.warm())

    async def warm(self) -> bool:
        """Pre-start the worker so the first code run is not the slow one."""
        session = self.core.session
        if not self.decide(session, mutating=False).use:
            return False
        try:
            async with self._lock:
                await self._ensure_ready(session)
        except (SandboxError, OSError) as exc:
            self.last_error = str(exc)
            return False
        return True

    # -- status and shutdown ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        process = self._process
        decision = self.decide(self.core.session, mutating=False)
        return {
            "mode": self.settings.mode,
            "running": bool(process and process.alive),
            "pid": process.info.get("pid") if process else None,
            "controls": process.info.get("controls", []) if process else [],
            "limits": process.info.get("limits", []) if process else [],
            "memory_mb": self.settings.memory_mb,
            "max_model_mb": self.settings.max_model_mb,
            "would_sandbox": decision.use,
            "reason": decision.reason,
            "last_error": self.last_error,
        }

    async def _stop(self, process: SandboxProcess) -> None:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(process.terminate)

    async def aclose(self) -> None:
        async with self._lock:
            if self._process is not None:
                await self._stop(self._process)
                self._process = None
        self._cleanup_scratch()

    def close(self) -> None:
        """Synchronous shutdown, for AppCore.shutdown."""
        if self._process is not None:
            with contextlib.suppress(Exception):
                self._process.terminate()
            self._process = None
        self._cleanup_scratch()

    def _cleanup_scratch(self) -> None:
        if self._scratch is not None and self._scratch.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(self._scratch, ignore_errors=True)
