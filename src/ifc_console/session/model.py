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

from ifc_console.core.results import ToolError
from ifc_console.session.backups import BackupStore
from ifc_console.session.working_copy import WorkingCopy

T = TypeVar("T")

_file_digest = getattr(hashlib, "file_digest", None)  # 3.11+

_LOAD_TIMEOUT = 600.0
_SAVE_TIMEOUT = 600.0


class ModelSession:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.ifc: Any = None
        # Registry identity: set once the session joins a ModelRegistry.
        # Only the active model is writable; attached ones are read-only.
        self.model_id: str | None = None
        self.read_only: bool = False
        self.schema: str | None = None
        self.size_bytes: int = 0
        self.loaded_at: str | None = None
        self.fingerprint: str | None = None
        self.source_sha256: str | None = None
        # (size, mtime_ns) of the file at the last load or save. While it still
        # matches and nothing is dirty, the bytes on disk ARE the model, so the
        # viewer can stream the file instead of re-serializing it.
        self.disk_key: tuple[int, int] | None = None
        self.dirty: bool = False
        self.tainted: bool = False
        # Set while this session edits a snapshot instead of the opened file.
        # `path` is the copy from then on; `working_copy.origin` names what the
        # user actually opened, which nothing in the session writes.
        self.working_copy: WorkingCopy | None = None
        # What has changed since the last load or save, for the surfaces that
        # offer to save it. The list is capped; the counter is not.
        self.change_count: int = 0
        self.change_log: list[dict[str, Any]] = []
        self.poisoned: bool = False
        self._timeout_poisoned = False
        self._cancelled_jobs = 0
        # Monotonic change counter: bumped on load, save, and every mutation.
        # fingerprint+revision is the viewer's ETag for the in-memory model.
        self.revision: int = 0
        # Remembered across reload()/recover(); 0 disables the size guard.
        self._max_open_mb: int = 0
        # Bumped by recover(); a job from an older generation must not write
        # its results back into the session.
        self._generation: int = 0
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ifc-model")

    @property
    def loaded(self) -> bool:
        return self.ifc is not None

    @property
    def name(self) -> str | None:
        return self.path.name if self.path else None

    @property
    def origin_path(self) -> Path | None:
        """The file the user opened: the working copy's origin, or `path`."""
        return self.working_copy.origin if self.working_copy else self.path

    @property
    def origin_name(self) -> str | None:
        origin = self.origin_path
        return origin.name if origin else None

    def matches_disk(self) -> bool:
        """True when the file on disk is byte-identical to the loaded model.

        Lets a reader stream the file rather than pay a full re-serialization.
        Deliberately conservative: any doubt answers False.
        """
        if self.dirty or self.tainted or self.path is None or self.disk_key is None:
            return False
        try:
            stat = self.path.stat()
        except OSError:
            return False
        if (stat.st_size, stat.st_mtime_ns) != self.disk_key or self.source_sha256 is None:
            return False
        try:
            digest, size_bytes = self._hash_file(self.path)
            verified_stat = self.path.stat()
        except OSError:
            return False
        return (
            size_bytes == verified_stat.st_size
            and (verified_stat.st_size, verified_stat.st_mtime_ns) == self.disk_key
            and digest == self.source_sha256
        )

    def require_loaded(self) -> None:
        if not self.loaded:
            raise ToolError(
                "NO_MODEL_LOADED",
                "no IFC model is loaded in this session.",
                "Call list_ifc_files then open_ifc_file, or ask the user to pick a "
                "model in the ifc-console terminal.",
            )

    def require_writable(self) -> None:
        if self.read_only:
            raise ToolError(
                "MODEL_READ_ONLY",
                f"{self.name} is attached read-only; only the active model can change.",
                "Call set_active_model to make it the active model first, or ask the "
                "user to run /use in the ifc-console terminal.",
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
        work = self._pool.submit(fn)
        future = asyncio.wrap_future(work, loop=loop)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.CancelledError:
            self.poisoned = True
            self._cancelled_jobs += 1
            generation = self._generation

            def completed(_future: object) -> None:
                with contextlib.suppress(RuntimeError):
                    loop.call_soon_threadsafe(self._finish_cancelled_job, generation)

            work.add_done_callback(completed)
            raise
        except asyncio.TimeoutError:
            self.poisoned = True
            self._timeout_poisoned = True
            hint = (
                "The worker is still running and the session is paused; ask the user "
                "to run /reload in the ifc-console terminal."
            )
            if timeout_code == "EXEC_TIMEOUT":
                message = f"code exceeded the {timeout:.0f}s execution timeout."
            else:
                message = f"the operation exceeded {timeout:.0f}s."
            raise ToolError(timeout_code, message, hint) from None

    def _finish_cancelled_job(self, generation: int) -> None:
        if generation != self._generation:
            return
        self._cancelled_jobs = max(0, self._cancelled_jobs - 1)
        if not self._cancelled_jobs and not self._timeout_poisoned:
            self.poisoned = False

    # -- lifecycle -------------------------------------------------------------
    def _load_sync(self, path: Path, generation: int) -> None:
        # Imported here, not at module top: ifcopenshell costs about a second
        # and `ifc-console --help` should never pay it.
        import ifcopenshell

        digest_before, size_before = self._hash_file(path)
        ifc = ifcopenshell.open(str(path))
        digest_after, size_after = self._hash_file(path)
        stat = path.stat()
        if digest_before != digest_after or size_before != size_after or size_after != stat.st_size:
            raise ToolError(
                "SOURCE_CHANGED",
                f"{path.name} changed while it was being opened.",
                "Retry after the source file is stable.",
            )
        if generation != self._generation:
            return  # a recover() superseded this job; its result is stale
        # Reloading the same file keeps the working copy; opening another one
        # is a different model and starts from its own file again.
        if self.path is None or path != self.path:
            self.working_copy = None
        self.ifc = ifc
        self.path = path
        self.schema = getattr(ifc, "schema", None)
        self.size_bytes = stat.st_size
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        self.source_sha256 = digest_after
        self.fingerprint = digest_after[:12]
        self.disk_key = (stat.st_size, stat.st_mtime_ns)
        self.dirty = False
        self.tainted = False
        self.change_count = 0
        self.change_log.clear()
        self.revision += 1

    async def open(self, path: Path, *, max_mb: int | None = None) -> None:
        path = path.resolve()
        if not path.exists():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{path} does not exist.",
                "Use list_ifc_files to see what is available.",
            )
        if max_mb is not None:
            self._max_open_mb = max_mb
        if self._max_open_mb:
            size_mb = path.stat().st_size / 1_048_576
            if size_mb > self._max_open_mb:
                raise ToolError(
                    "MODEL_TOO_LARGE",
                    f"{path.name} is {size_mb:.0f} MB, over the {self._max_open_mb} MB "
                    "open budget (files.max_open_mb).",
                    "Loading it could exhaust memory. If you really want to, ask the "
                    "user to raise it: /settings files.max_open_mb <mb> in the "
                    "ifc-console terminal.",
                )
        try:
            generation = self._generation
            await self.run(lambda: self._load_sync(path, generation), timeout=_LOAD_TIMEOUT)
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
        # Fence the abandoned job: it keeps running on the old pool and would
        # otherwise write a stale fingerprint, revision and dirty flag into the
        # freshly reloaded session minutes later.
        self._generation += 1
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ifc-model")
        old.shutdown(wait=False, cancel_futures=True)
        self._timeout_poisoned = False
        self._cancelled_jobs = 0
        self.poisoned = False
        if self.path is not None:
            await self.open(self.path)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    # -- mutation bookkeeping ----------------------------------------------------
    def mark_dirty(self) -> None:
        self.dirty = True
        self.revision += 1

    def record_change(self, description: str, *, tool: str = "") -> int:
        """Log one announced edit and return the running change count."""
        self.change_count += 1
        self.change_log.append(
            {
                "at": datetime.now(timezone.utc).isoformat(),
                "tool": tool,
                "description": (description or "model edit").strip()[:200],
            }
        )
        del self.change_log[:-100]
        return self.change_count

    def changes_summary(self, limit: int = 5) -> dict[str, Any]:
        return {
            "count": self.change_count,
            "recent": [entry["description"] for entry in self.change_log[-limit:]],
        }

    def adopt_working_copy(self, copy: WorkingCopy) -> None:
        """Point the session at a byte-identical copy of the loaded file.

        The bytes are the same, so the digest still describes them; only the
        path and the (size, mtime) pair the fast reader compares move over.
        """
        stat = copy.path.stat()
        self.working_copy = copy
        self.path = copy.path
        self.disk_key = (stat.st_size, stat.st_mtime_ns) if not self.dirty else None
        self.revision += 1

    def max_id(self) -> int | None:
        """Cheap mutation canary; call only from inside the worker."""
        try:
            return int(self.ifc.wrapped_data.getMaxId())
        except Exception:
            return None

    # -- saving ---------------------------------------------------------------
    def _verify_expected_target(self, target: Path) -> None:
        if self.path is None or target != self.path.resolve() or self.source_sha256 is None:
            return
        try:
            digest, _size = self._hash_file(target)
        except OSError as exc:
            raise ToolError(
                "REVISION_CONFLICT",
                f"the loaded source {target.name} is no longer readable: {exc}",
                "Reload the model and review the external change before saving.",
            ) from exc
        if digest != self.source_sha256:
            raise ToolError(
                "REVISION_CONFLICT",
                f"{target.name} changed on disk after it was opened.",
                "Reload the model and review the external change before saving.",
            )

    def _is_working_copy(self, target: Path) -> bool:
        return self.working_copy is not None and target == self.working_copy.path

    def _save_sync(self, target: Path, backups: BackupStore, generation: int) -> dict[str, Any]:
        self._verify_expected_target(target)
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(4)}.tmp")
        try:
            self.ifc.write(str(tmp))
            with tmp.open("r+b") as handle:
                os.fsync(handle.fileno())

            import ifcopenshell

            try:
                verified = ifcopenshell.open(str(tmp))
            except Exception as exc:
                raise ToolError(
                    "VALIDATION_FAILED",
                    f"the serialized model could not be reopened: {exc}",
                    "Reload the model and retry. The original file was not changed.",
                ) from exc
            if getattr(verified, "schema", None) != self.schema:
                raise ToolError(
                    "VALIDATION_FAILED",
                    "the serialized model did not reopen with the expected schema.",
                    "Reload the model and retry. The original file was not changed.",
                )
            del verified

            self._verify_expected_target(target)
            # A working copy needs no backup: the file the user opened has not
            # been written, so it is the snapshot a backup would be.
            backup_path = None if self._is_working_copy(target) else backups.backup(target)
            self._verify_expected_target(target)
            os.replace(tmp, target)
            self._fsync_directory(target.parent)
        finally:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()
        source_sha256, _size_bytes = self._hash_file(target)
        fingerprint = source_sha256[:12]
        saved_stat = target.stat()
        if generation != self._generation:
            # The file is written, but a recover() has replaced this session's
            # model since. Reporting the write back would mark the reloaded
            # model clean and hand the viewer a fingerprint it is not holding.
            return {
                "path": str(target),
                "size_bytes": saved_stat.st_size,
                "backup_path": str(backup_path) if backup_path else None,
                "fingerprint": fingerprint,
                "superseded": True,
            }
        self.fingerprint = fingerprint
        self.source_sha256 = source_sha256
        self.path = target
        self.size_bytes = saved_stat.st_size
        self.disk_key = (saved_stat.st_size, saved_stat.st_mtime_ns)
        self.dirty = False
        self.change_count = 0
        self.change_log.clear()
        self.revision += 1
        return {
            "path": str(target),
            "size_bytes": self.size_bytes,
            "backup_path": str(backup_path) if backup_path else None,
            "fingerprint": self.fingerprint,
            "working_copy": self._is_working_copy(target),
        }

    async def save(self, target: Path, backups: BackupStore) -> dict[str, Any]:
        self.require_loaded()
        self.require_writable()
        generation = self._generation
        return await self.run(
            lambda: self._save_sync(target.resolve(), backups, generation),
            timeout=_SAVE_TIMEOUT,
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
                source_sha256=self.source_sha256,
            )
            meta["changes"] = self.change_count
            if self.working_copy is not None:
                # The same key names the fuller payloads use, so `origin` is
                # never a path in one place and a filename in another.
                meta["working_copy"] = {
                    "name": self.working_copy.path.name,
                    "origin_name": self.working_copy.origin.name,
                }
            if self.tainted:
                meta["warning"] = (
                    "session tainted: guarded code may have mutated the in-memory "
                    "model; ask the user to run /reload for a pristine state"
                )
        else:
            meta["model"] = None
        return meta

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        # file_digest (3.11+) hashes in C and drops the GIL; on a 70 MB model
        # that is about three times faster than a read/update loop in Python.
        with path.open("rb") as handle:
            if _file_digest is not None:
                return _file_digest(handle, "sha256").hexdigest(), handle.tell()
            digest = hashlib.sha256()
            size_bytes = 0
            for chunk in iter(lambda: handle.read(1 << 22), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
        return digest.hexdigest(), size_bytes

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
