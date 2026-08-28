"""Parent side of the sandbox: spawn a worker, talk to it, kill it on time.

The worker inherits a hand-built environment, not the console's. That is the
single most valuable line in this file: API keys, cloud credentials, and the
console's own bearer token are simply not present in the process that runs
model-authored code, so there is nothing for a leak to carry.
"""

from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any

from ifc_console.sandbox import protocol
from ifc_console.sandbox.limits import ProcessJail, isolated_process_kwargs
from ifc_console.sandbox.policy import SandboxPolicy

_STARTUP_TIMEOUT = 120.0
_STDERR_KEEP = 16
_STDERR_CHUNK = 4096
_WORKER_BOOTSTRAP = (
    "import json,runpy,sys;"
    "paths,module,*args=sys.argv[1:];"
    "sys.path.extend(json.loads(paths));"
    "sys.argv=[module,*args];"
    "runpy.run_module(module,run_name='__main__')"
)


class SandboxError(RuntimeError):
    """The worker could not be started or stopped answering."""


class SandboxTimeout(SandboxError):
    """A request outlived its deadline; the worker has been killed."""


class SandboxNotReady(SandboxTimeout):
    """Startup or model load ran out of time, so no user code ever ran.

    Subclasses SandboxTimeout so existing handlers keep working, but lets the
    caller name the limit that was actually hit.
    """


def worker_executable() -> str:
    """The real interpreter, not a venv launcher.

    A Windows venv `python.exe` is a trampoline that re-launches the base
    interpreter as a child process, which the sandbox job object forbids.
    Starting the base interpreter directly keeps the one-process limit usable;
    the venv's packages are added by the isolated worker bootstrap.
    """
    base = getattr(sys, "_base_executable", None)
    if base and base != sys.executable and os.path.isfile(base):
        return base
    return sys.executable


def worker_path(narrow: bool = True) -> list[str]:
    """Import roots for the worker.

    Handing over the parent's whole sys.path would also hand over read access
    to every directory on it, including whatever directory the console
    happened to start from. The narrow list is just the two packages the
    worker imports plus the site-packages holding their dependencies.
    """
    entries = [p for p in sys.path if p and os.path.isdir(p)]
    if not narrow:
        return entries
    wanted: list[str] = []
    for name in ("ifc_console", "ifcopenshell"):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        for location in spec.submodule_search_locations or () if spec else ():
            wanted.append(os.path.dirname(os.path.abspath(location)))
    wanted += [p for p in entries if os.path.basename(p) in ("site-packages", "dist-packages")]
    resolved = [p for p in wanted if os.path.isdir(p)]
    return list(dict.fromkeys(resolved)) or entries


def worker_command(
    module: str,
    *args: str | os.PathLike[str],
    narrow_path: bool = True,
) -> tuple[str, ...]:
    """Run a worker after the standard library, without site customisation."""
    return (
        worker_executable(),
        "-I",
        "-S",
        "-B",
        "-u",
        "-X",
        "utf8",
        "-c",
        _WORKER_BOOTSTRAP,
        json.dumps(worker_path(narrow_path)),
        module,
        *(os.fspath(arg) for arg in args),
    )


def _child_env(scratch: Path) -> dict[str, str]:
    """A minimal environment. Nothing is inherited that was not put here."""
    env = {
        "TMPDIR": str(scratch),
        "TEMP": str(scratch),
        "TMP": str(scratch),
    }
    interpreter_dir = os.path.dirname(os.path.abspath(worker_executable()))
    if sys.platform == "win32":
        # The loader needs these to start a process at all; none of them
        # identify the user or carry a credential.
        for name in (
            "SYSTEMROOT",
            "SystemRoot",
            "SystemDrive",
            "PATHEXT",
            "NUMBER_OF_PROCESSORS",
            "PROCESSOR_ARCHITECTURE",
        ):
            value = os.environ.get(name)
            if value:
                env[name] = value
        dll_dir = os.path.join(sys.base_prefix, "DLLs")
        env["PATH"] = os.pathsep.join([interpreter_dir, dll_dir])
    else:
        env["PATH"] = "/usr/bin:/bin"
        env["HOME"] = str(scratch)
        for name in ("LANG", "LC_ALL"):
            value = os.environ.get(name)
            if value:
                env[name] = value
    return env


class SandboxProcess:
    """One worker process and the pipe to it. Not thread-safe by itself; the
    runner serializes calls."""

    def __init__(self, policy: SandboxPolicy, scratch: Path) -> None:
        self.policy = policy
        self.scratch = scratch
        self.proc: subprocess.Popen[bytes] | None = None
        self.jail = ProcessJail(policy.memory_mb)
        self.info: dict[str, Any] = {}
        self.loaded_key: str | None = None
        self._replies: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_STDERR_KEEP)
        self._ids = itertools.count(1)
        self._dead_reason: str | None = None

    # -- lifecycle -----------------------------------------------------------
    def start(self, timeout: float = _STARTUP_TIMEOUT) -> dict[str, Any]:
        try:
            return self._spawn(timeout, single_process=True, narrow_path=True)
        except SandboxError:
            # An unusual interpreter layout can need a helper process, or import
            # roots the narrow path missed. A weaker sandbox beats none.
            self._reset()
            return self._spawn(timeout, single_process=False, narrow_path=False)

    def _spawn(self, timeout: float, *, single_process: bool, narrow_path: bool) -> dict[str, Any]:
        self.scratch.mkdir(parents=True, exist_ok=True)
        self.jail = ProcessJail(self.policy.memory_mb, single_process=single_process)
        kwargs: dict[str, Any] = isolated_process_kwargs()
        if sys.platform == "win32":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            worker_command("ifc_console.sandbox.worker", narrow_path=narrow_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_child_env(self.scratch),
            cwd=str(self.scratch),
            close_fds=True,
            **kwargs,
        )
        self.jail.attach(self.proc.pid)
        threading.Thread(target=self._read_loop, daemon=True, name="ifc-sandbox-rx").start()
        threading.Thread(target=self._drain_stderr, daemon=True, name="ifc-sandbox-err").start()
        reply = self.request({"op": "init", "policy": self.policy.to_dict()}, timeout=timeout)
        self.info = {
            "pid": reply.get("pid"),
            "limits": reply.get("limits") or [],
            "controls": (reply.get("controls") or []) + self.jail.controls,
        }
        return self.info

    def _reset(self) -> None:
        with contextlib.suppress(Exception):
            self.terminate("restarting")
        self.proc = None
        self._dead_reason = None
        self._replies = queue.Queue()
        self._stderr.clear()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def terminate(self, reason: str = "closed") -> None:
        self._dead_reason = reason
        proc = self.proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin is not None:
                proc.stdin.close()
        if proc.poll() is None:
            self.jail.kill()
            with contextlib.suppress(Exception):
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    proc.kill()
        self.jail.close()
        for stream in (proc.stdout, proc.stderr):
            with contextlib.suppress(Exception):
                if stream is not None:
                    stream.close()
        self._replies.put(None)

    # -- request/response --------------------------------------------------------
    def request(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        proc = self.proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise SandboxError(self._dead_reason or "the sandbox worker is not running")
        request_id = next(self._ids)
        payload = {**payload, "id": request_id}
        try:
            proc.stdin.write(protocol.encode(payload))
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SandboxError(f"the sandbox worker stopped accepting work: {exc}") from exc
        try:
            reply = self._replies.get(timeout=timeout)
        except queue.Empty:
            self.terminate("timed out")
            raise SandboxTimeout("the sandbox worker exceeded its deadline") from None
        if reply is None:
            detail = self.stderr_tail() or self._dead_reason or "no output"
            raise SandboxError(f"the sandbox worker exited: {detail}")
        if reply.get("id") != request_id:
            self.terminate("the sandbox worker returned a mismatched response id")
            raise SandboxError("the sandbox worker returned a mismatched response id")
        return reply

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr).strip()

    # -- pipe threads --------------------------------------------------------------
    def _read_loop(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                frame = protocol.read_frame(proc.stdout)
                if frame is None:
                    break
                self._replies.put(frame)
        except (protocol.ProtocolError, OSError, ValueError) as exc:
            self._dead_reason = str(exc)
        finally:
            self._replies.put(None)

    def _drain_stderr(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stderr is not None
        with contextlib.suppress(Exception):
            while chunk := proc.stderr.read1(_STDERR_CHUNK):
                self._stderr.append(chunk.decode("utf-8", "replace").rstrip())
