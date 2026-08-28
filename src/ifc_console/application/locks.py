"""Small cross-platform advisory locks for local durable stores."""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import BinaryIO

from ifc_console.core.results import ToolError


@contextmanager
def exclusive_file_lock(
    path: Path, *, timeout_s: float = 15.0, error_code: str = "STORE_BUSY"
) -> Iterator[None]:
    with _open_lock_file(path, error_code) as handle:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                _lock(handle.fileno())
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise _busy(path, error_code) from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle.fileno())


@asynccontextmanager
async def async_exclusive_file_lock(
    path: Path, *, timeout_s: float = 15.0, error_code: str = "MODEL_BUSY"
) -> AsyncIterator[None]:
    """Async variant for transaction paths that must not block the event loop."""
    with _open_lock_file(path, error_code) as handle:
        deadline = time.monotonic() + timeout_s
        locked = False
        while not locked:
            try:
                _lock(handle.fileno())
                locked = True
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise _busy(path, error_code) from exc
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            _unlock(handle.fileno())


def _open_lock_file(path: Path, error_code: str) -> BinaryIO:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    for name in ("O_BINARY", "O_CLOEXEC", "O_NOINHERIT", "O_NOFOLLOW"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(path, flags, 0o600)
        opened = os.fstat(fd)
        linked = os.lstat(path)
        is_junction = getattr(path, "is_junction", None)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (is_junction is not None and is_junction())
            or not os.path.samestat(opened, linked)
        ):
            raise OSError("lock path is not a regular file")
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        raise ToolError(
            error_code,
            f"refusing unsafe lock file at {path}.",
            "Remove the symlink, junction, or non-regular lock path and retry.",
        ) from exc
    handle = os.fdopen(fd, "r+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


def _busy(path: Path, error_code: str) -> ToolError:
    return ToolError(
        error_code,
        f"timed out waiting for the local lock at {path}.",
        "Wait for the other IFC-Console process to finish, then retry.",
    )


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
