"""Small cross-platform advisory locks for local durable stores."""

from __future__ import annotations

import os
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
