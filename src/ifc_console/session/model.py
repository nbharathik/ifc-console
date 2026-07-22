"""ModelSession: owns the in-memory IFC model.

All model access is serialized through a single worker thread; IfcOpenShell
files are not thread-safe. A timed-out job leaves the worker occupied and the
session *poisoned* (CPython cannot kill threads); `recover()` swaps in a fresh
worker and reloads from disk.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import ifcopenshell

from ifc_console.mcp.envelope import ToolError
from ifc_console.session.backups import BackupStore

T = TypeVar("T")

_LOAD_TIMEOUT = 600.0
_SAVE_TIMEOUT = 600.0


class ModelSession:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.ifc: Any = None
        self.schema: str | None = None
        self.size_bytes: int = 0
        self.loaded_at: str | None = None
        self.fingerprint: str | None = None
        self.dirty: bool = False
        self.tainted: bool = False
        self.poisoned: bool = False
        # Monotonic change counter: bumped on load, save, and every mutation.
        # fingerprint+revision is the viewer's ETag for the in-memory model.
        self.revision: int = 0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ifc-model")

    @property
    def loaded(self) -> bool:
        return self.ifc is not None

    @property
    def name(self) -> str | None:
        return self.path.name if self.path else None

    def require_loaded(self) -> None:
        if not self.loaded:
            raise ToolError(
                "NO_MODEL_LOADED",
                "no IFC model is loaded in this session.",
                "Call list_ifc_files then open_ifc_file, or ask the user to pick a "
                "model in the ifc-console terminal.",
            )

    # -- serialized execution ------------------------------------------------
    async def run(
        self,
        fn: Callable[[], T],
        *,
        timeout: float | None = None,
        timeout_code: str = "MODEL_BUSY",
    ) -> T:
        if self.poisoned:
            raise ToolError(
                "MODEL_BUSY",
                "the model worker is unavailable after a previous timeout.",
                "Ask the user to run /reload in the ifc-console terminal, then retry.",
            )
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._pool, fn)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self.poisoned = True
            hint = (
                "The worker is still running and the session is paused; ask the user "
                "to run /reload in the ifc-console terminal."
            )
            if timeout_code == "EXEC_TIMEOUT":
                message = f"code exceeded the {timeout:.0f}s execution timeout."
            else:
                message = f"the operation exceeded {timeout:.0f}s."
            raise ToolError(timeout_code, message, hint) from None

    # -- lifecycle -------------------------------------------------------------
    def _load_sync(self, path: Path) -> None:
        ifc = ifcopenshell.open(str(path))
        stat = path.stat()
        self.ifc = ifc
        self.path = path
        self.schema = getattr(ifc, "schema", None)
        self.size_bytes = stat.st_size
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        self.fingerprint = hashlib.sha256(
            f"{path}|{stat.st_size}|{stat.st_mtime_ns}".encode()
        ).hexdigest()[:12]
        self.dirty = False
        self.tainted = False
        self.revision += 1

    async def open(self, path: Path) -> None:
        path = path.resolve()
        if not path.exists():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{path} does not exist.",
                "Use list_ifc_files to see what is available.",
            )
        try:
            await self.run(lambda: self._load_sync(path), timeout=_LOAD_TIMEOUT)
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(
                "FILE_NOT_FOUND",
                f"could not parse {path.name}: {exc}",
                "The file may be corrupt or not an IFC file.",
            ) from exc

    async def reload(self) -> None:
        if self.path is None:
            raise ToolError("NO_MODEL_LOADED", "nothing to reload.", "Open a model first.")
        await self.open(self.path)

    async def recover(self) -> None:
        """Replace a poisoned worker and reload from disk (console /reload)."""
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ifc-model")
        old.shutdown(wait=False, cancel_futures=True)
        self.poisoned = False
        if self.path is not None:
            await self.open(self.path)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- mutation bookkeeping ----------------------------------------------------
    def mark_dirty(self) -> None:
        self.dirty = True
        self.revision += 1

    def max_id(self) -> int | None:
        """Cheap mutation canary; call only from inside the worker."""
        try:
            return int(self.ifc.wrapped_data.getMaxId())
        except Exception:
            return None

    # -- saving ---------------------------------------------------------------
    def _save_sync(self, target: Path, backups: BackupStore) -> dict[str, Any]:
        backup_path = backups.backup(target)  # a failed backup aborts the save
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        try:
            self.ifc.write(str(tmp))
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
        digest = hashlib.sha256()
        with target.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        self.fingerprint = digest.hexdigest()[:12]
        self.path = target
        self.size_bytes = target.stat().st_size
        self.dirty = False
        self.revision += 1
        return {
            "path": str(target),
            "size_bytes": self.size_bytes,
            "backup_path": str(backup_path) if backup_path else None,
            "fingerprint": self.fingerprint,
        }

    async def save(self, target: Path, backups: BackupStore) -> dict[str, Any]:
        self.require_loaded()
        return await self.run(
            lambda: self._save_sync(target.resolve(), backups), timeout=_SAVE_TIMEOUT
        )

    # -- envelope meta -----------------------------------------------------------
    def meta(self, mode: str) -> dict[str, Any]:
        meta: dict[str, Any] = {"mode": mode}
        if self.loaded:
            meta.update(
                model=self.name,
                schema=self.schema,
                dirty=self.dirty,
                fingerprint=self.fingerprint,
            )
            if self.tainted:
                meta["warning"] = (
                    "session tainted: guarded code may have mutated the in-memory "
                    "model; ask the user to run /reload for a pristine state"
                )
        else:
            meta["model"] = None
        return meta
