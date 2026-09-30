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
from ifc_console.ifc.change_record import ChangeSummary, summarize, verify_rollback
from ifc_console.session.backups import BackupStore
from ifc_console.session.history import ChangeHistory, ChangeRecord
from ifc_console.session.working_copy import WorkingCopy

T = TypeVar("T")

# An edit that logs more steps than this is kept in the model but not in the
# undo log: the log holds a serialized copy of every entity it touched.
MAX_UNDO_OPS = 250_000

_file_digest = getattr(hashlib, "file_digest", None)  # 3.11+

_LOAD_TIMEOUT = 600.0
_SAVE_TIMEOUT = 600.0


class OpenChange:
    """One edit in progress: the transaction it runs in and what to restore."""

    def __init__(self, transaction: Any | None, was_dirty: bool, live: bool) -> None:
        self.transaction = transaction
        self.was_dirty = was_dirty
        self.live = live  # False for a dry run, which never touches the flags


class MutationOutcome:
    """How an edit ended, for whoever has to report it."""

    def __init__(
        self,
        record: ChangeRecord | None = None,
        *,
        rolled_back: bool = False,
        verified: bool | None = None,
    ) -> None:
        self.record = record
        self.rolled_back = rolled_back
        self.verified = verified


class ModelSession:
    def __init__(self, undo_depth: int = 20) -> None:
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
        # Every edit since the load as one undoable step, and where the saved
        # state sits among them. IfcOpenShell keeps `undo_depth` steps.
        self.undo_depth = max(0, int(undo_depth))
        self.history = ChangeHistory()
        self.poisoned: bool = False
        self._timeout_poisoned = False
        self._cancelled_jobs = 0
        # The model's identity is fingerprint + load_nonce + revision. The
        # fingerprint hashes the file as it was loaded and the nonce tells one
        # load from the next. The revision moves only when the in-memory model
        # changes (a load or a mutation): saving writes it out and entering edit
        # mode swaps the file underneath, neither of which changes the model.
        self.load_nonce: str = ""
        self.revision: int = 0
        # Held by a mutation or a save for its whole run, so their bookkeeping
        # (dirty flag, change log, events) cannot interleave.
        self.edit_lock = asyncio.Lock()
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

        # One hash, taken before the parse. A write during the parse moves the
        # file's size or mtime, which the stat pair below catches.
        stat_before = path.stat()
        digest_after, size_hashed = self._hash_file(path)
        ifc = ifcopenshell.open(str(path))
        stat = path.stat()
        if (
            (stat.st_size, stat.st_mtime_ns) != (stat_before.st_size, stat_before.st_mtime_ns)
            or size_hashed != stat.st_size
        ):
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
        ifc.set_history_size(self.undo_depth)
        self.ifc = ifc
        self.path = path
        self.schema = getattr(ifc, "schema", None)
        self.size_bytes = stat.st_size
        self.loaded_at = datetime.now(timezone.utc).isoformat()
        self.source_sha256 = digest_after
        self.fingerprint = digest_after[:12]
        self.load_nonce = secrets.token_hex(4)
        self.disk_key = (stat.st_size, stat.st_mtime_ns)
        self.dirty = False
        self.tainted = False
        self.history = ChangeHistory()
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
                "INVALID_IFC",
                f"could not parse {path.name}: {exc}",
                "The file may be corrupt or not an IFC file; check it with "
                "`ifc-console check <file>`.",
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
    @property
    def change_count(self) -> int:
        """Steps between the model and the file on disk."""
        return self.history.unsaved

    def mark_dirty(self) -> None:
        self.dirty = True
        self.revision += 1

    def record_change(self, description: str, *, tool: str = "") -> int:
        """Log a step that has no transaction, so nothing before it can be undone."""
        self.history.push(ChangeRecord.new(tool, description, ChangeSummary()), alive=0)
        return self.change_count

    def changes_summary(self, limit: int = 5) -> dict[str, Any]:
        return {"count": self.change_count, "recent": self.history.recent(limit)}

    # -- undoable edits (worker thread only) --------------------------------------
    def begin_change(self, *, dry_run: bool = False) -> OpenChange:
        """Open the transaction one edit runs in and flag the model as changing.

        The transaction is made here rather than by `begin_transaction`, which
        does nothing when the undo depth is 0; rolling back a failed edit must
        work either way. The revision moves when the edit is kept, not now: a
        rolled-back edit leaves the model, and the context a caller holds, as
        it was.
        """
        from ifcopenshell.file import Transaction

        change = OpenChange(None, self.dirty, live=not dry_run)
        if not dry_run:
            self.dirty = True
        change.transaction = Transaction(self.ifc)
        self.ifc.transaction = change.transaction
        return change

    def keep_change(
        self, change: OpenChange, *, tool: str, description: str
    ) -> ChangeRecord | None:
        """Close a finished edit as one step. None when it changed nothing."""
        ifc = self.ifc
        transaction = change.transaction
        if ifc.transaction is not transaction:
            # Something replaced the log mid-run, so it is incomplete: keep the
            # edit, say it cannot be undone, and redraw everything.
            ifc.transaction = None
            ifc.history.clear()
            ifc.future.clear()
            summary = ChangeSummary(geometry=True, tree=True, labels=True, truncated=True)
        else:
            try:
                summary = summarize(ifc, transaction)
            except Exception:
                summary = ChangeSummary(
                    ops=len(transaction.operations), geometry=True, tree=True, truncated=True
                )
            if not summary.ops:
                ifc.discard_transaction()
                if change.live:
                    self.dirty = change.was_dirty
                return None
            ifc.end_transaction()
            if summary.ops > MAX_UNDO_OPS:
                ifc.history.clear()
                ifc.future.clear()
        record = ChangeRecord.new(tool, description, summary)
        self.history.push(record, alive=len(ifc.history))
        self.dirty = True
        self.revision += 1
        return record

    def drop_change(self, change: OpenChange) -> tuple[bool, bool | None]:
        """Roll a failed or dry-run edit back. Returns (rolled_back, verified)."""
        ifc = self.ifc
        transaction = change.transaction
        if ifc.transaction is transaction:
            ifc.discard_transaction()
        else:
            ifc.transaction = None
            transaction.rollback()
        verified = verify_rollback(ifc, transaction)
        if verified is True and change.live:
            self.dirty = change.was_dirty
        elif verified is False:
            self.tainted = True  # the model no longer matches anything we can name
        return True, verified

    def mutate(
        self,
        fn: Callable[[], T],
        *,
        tool: str,
        description: str,
        dry_run: bool = False,
    ) -> tuple[T, MutationOutcome]:
        """Run `fn` as one atomic, undoable edit.

        Raising inside `fn` rolls everything it did back; the exception carries
        the outcome as `mutation_outcome`. A dry run always rolls back.
        """
        from ifc_console.policy.savepoints import savepoints

        change = self.begin_change(dry_run=dry_run)
        try:
            with savepoints(self.ifc, change.transaction):
                result = fn()
        except BaseException as exc:
            rolled_back, verified = self.drop_change(change)
            exc.mutation_outcome = MutationOutcome(  # type: ignore[attr-defined]
                rolled_back=rolled_back, verified=verified
            )
            raise
        if dry_run:
            rolled_back, verified = self.drop_change(change)
            return result, MutationOutcome(rolled_back=rolled_back, verified=verified)
        return result, MutationOutcome(self.keep_change(change, tool=tool, description=description))

    def _after_history_step(self) -> None:
        self.revision += 1
        self.dirty = self.tainted or not self.history.at_saved_state

    def undo_change(self) -> ChangeRecord:
        if not self.history.can_undo or not self.ifc.history:
            raise ToolError(
                "NOTHING_TO_UNDO",
                "there is no edit to undo.",
                f"The undo log keeps the last {self.undo_depth} edits since the model "
                "was loaded (edit.undo_depth). /reload restores the file.",
            )
        from ifcopenshell.file import UndoSystemError

        try:
            self.ifc.undo()
        except UndoSystemError as exc:
            self.tainted = True
            raise ToolError(
                "UNDO_FAILED",
                f"the edit could not be undone cleanly: {exc}",
                "The model may be half restored. Run /reload to start again from the file.",
            ) from exc
        record = self.history.undo(alive=len(self.ifc.history))
        self._after_history_step()
        return record

    def redo_change(self) -> ChangeRecord:
        if not self.history.can_redo or not self.ifc.future:
            raise ToolError(
                "NOTHING_TO_REDO",
                "there is no undone edit to redo.",
                "Redo is only available right after an undo; a new edit clears it.",
            )
        from ifcopenshell.file import UndoSystemError

        try:
            self.ifc.redo()
        except UndoSystemError as exc:
            self.tainted = True
            raise ToolError(
                "UNDO_FAILED",
                f"the edit could not be redone cleanly: {exc}",
                "The model may be half restored. Run /reload to start again from the file.",
            ) from exc
        record = self.history.redo(alive=len(self.ifc.history))
        self._after_history_step()
        return record

    def adopt_working_copy(self, copy: WorkingCopy) -> None:
        """Point the session at a byte-identical copy of the loaded file.

        The bytes are the same, so the digest and the model's identity still
        describe them; only the path and the (size, mtime) pair the fast reader
        compares move over.
        """
        stat = copy.path.stat()
        self.working_copy = copy
        self.path = copy.path
        self.disk_key = (stat.st_size, stat.st_mtime_ns) if not self.dirty else None

    @property
    def revision_id(self) -> str:
        return f"{self.fingerprint}:{self.revision}"

    def etag_at(self, revision: int) -> str:
        return f"{self.model_id or 'model'}-{self.fingerprint}-{self.load_nonce}-{revision}"

    @property
    def etag(self) -> str:
        """Names one state of the loaded model; the viewer caches on it."""
        return self.etag_at(self.revision)

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
        # Saving changes where the model lives and what is on disk, not the model
        # itself, so its identity (fingerprint, nonce, revision) stays put and
        # anything keyed on it, a viewer tab or a reviewed context, stays valid.
        self.source_sha256 = source_sha256
        self.path = target
        self.size_bytes = saved_stat.st_size
        self.disk_key = (saved_stat.st_size, saved_stat.st_mtime_ns)
        self.dirty = False
        self.history.mark_saved()
        return {
            "path": str(target),
            "size_bytes": self.size_bytes,
            "backup_path": str(backup_path) if backup_path else None,
            "fingerprint": self.fingerprint,
            "content_sha256": source_sha256,
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
