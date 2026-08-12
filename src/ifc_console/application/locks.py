"""Small cross-platform advisory locks for local durable stores."""

from __future__ import annotations

import os
import secrets
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ifc_console.core.results import ToolError


@contextmanager
def exclusive_file_lock(
    path: Path, *, timeout_s: float = 15.0, error_code: str = "STORE_BUSY"
) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                _lock(handle.fileno())
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise ToolError(
                        error_code,
                        f"timed out waiting for the local store lock at {path}.",
                        "Wait for the other IFC-Console process to finish, then retry.",
                    ) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle.fileno())


@contextmanager
def owned_file_lock(
    path: Path, *, timeout_s: float = 15.0, error_code: str = "MODEL_BUSY"
) -> Iterator[None]:
    """Acquire a crash-recoverable lock represented by an owner JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(16)
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                import json

                json.dump({"pid": os.getpid(), "token": token}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            break
        except (FileExistsError, PermissionError):
            stale = False
            try:
                import json

                owner = json.loads(path.read_text(encoding="utf-8"))
                stale = not process_is_running(int(owner.get("pid", 0)))
            except (OSError, ValueError, json.JSONDecodeError):
                try:
                    stale = time.time() - path.stat().st_mtime >= 1.0
                except OSError:
                    stale = False
            if stale:
                stale_path = path.with_name(f"{path.name}.{secrets.token_hex(4)}.stale")
                try:
                    os.replace(path, stale_path)
                    stale_path.unlink()
                    continue
                except OSError:
                    pass
            if time.monotonic() >= deadline:
                raise ToolError(
                    error_code,
                    f"timed out waiting for the local owner lock at {path}.",
                    "Wait for the other IFC-Console process to finish, then retry.",
                ) from None
            time.sleep(0.05)
    try:
        yield
    finally:
        try:
            import json

            owner = json.loads(path.read_text(encoding="utf-8"))
            if owner.get("token") == token:
                path.unlink()
        except (OSError, json.JSONDecodeError):
            pass


def process_is_running(pid: int) -> bool:
    """Return whether a PID still represents a running process.

    On Windows, ``OpenProcess`` can succeed for a terminated process whose
    kernel object has not been released yet. The exit code is therefore the
    authoritative liveness check.
    """

    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(query_limited_information, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
