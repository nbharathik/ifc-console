"""Fsynced transaction journals and deterministic filesystem recovery."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import exclusive_file_lock
from ifc_console.automation.files import sha256_file
from ifc_console.core.context import OperationContext
from ifc_console.core.results import ToolError
from ifc_console.core.transaction_journal import (
    PRE_COMMIT_PHASES,
    TERMINAL_TRANSACTION_PHASES,
    TransactionJournal,
    TransactionKind,
    TransactionPhase,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_ALLOWED_TRANSITIONS = {
    TransactionPhase.PREPARED: {
        TransactionPhase.CANDIDATE_VERIFIED,
        TransactionPhase.ABORTED,
        TransactionPhase.RECOVERY_FAILED,
    },
    TransactionPhase.CANDIDATE_VERIFIED: {
        TransactionPhase.BACKUP_VERIFIED,
        TransactionPhase.ABORTED,
        TransactionPhase.RECOVERY_FAILED,
    },
    TransactionPhase.BACKUP_VERIFIED: {
        TransactionPhase.COMMIT_POINT,
        TransactionPhase.ABORTED,
        TransactionPhase.RECOVERY_FAILED,
    },
    TransactionPhase.COMMIT_POINT: {
        TransactionPhase.TARGET_VERIFIED,
        TransactionPhase.ROLLBACK_STARTED,
        TransactionPhase.ROLLED_BACK,
        TransactionPhase.RECOVERY_FAILED,
    },
    TransactionPhase.TARGET_VERIFIED: {
        TransactionPhase.RECEIPT_PREPARED,
        TransactionPhase.ROLLBACK_STARTED,
        TransactionPhase.ROLLED_BACK,
        TransactionPhase.RECOVERY_FAILED,
    },
    TransactionPhase.RECEIPT_PREPARED: {
        TransactionPhase.RECEIPT_PERSISTED,
        TransactionPhase.ROLLBACK_STARTED,
        TransactionPhase.ROLLED_BACK,
        TransactionPhase.RECOVERY_FAILED,
    },
    TransactionPhase.ROLLBACK_STARTED: {
        TransactionPhase.ROLLED_BACK,
        TransactionPhase.RECOVERY_FAILED,
    },
}


class TransactionJournalStore:
    """Persist and recover replacement state independently of a model session."""

    def __init__(
        self,
        root: Path,
        artifacts: ArtifactService,
        *,
        lock_timeout_s: float = 15.0,
    ) -> None:
        self.root = root
        self.records_dir = root / "journals"
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        self.lock_timeout_s = lock_timeout_s
        self.instance_id = secrets.token_hex(8)
        self._lock = RLock()
        self._corrupt_paths: tuple[Path, ...] = ()

    def create(
        self,
        *,
        kind: TransactionKind,
        target: Path,
        expected_before_sha256: str,
        desired_after_sha256: str,
        candidate: Path,
        job_id: str | None = None,
        change_set_id: str | None = None,
        approval_id: str | None = None,
        source_commit_id: str | None = None,
        context: OperationContext | None = None,
    ) -> TransactionJournal:
        created = _now()
        journal = TransactionJournal(
            transaction_id=f"txn-{secrets.token_hex(8)}",
            kind=kind,
            created_at=created,
            updated_at=created,
            phase=TransactionPhase.PREPARED,
            owner_pid=os.getpid(),
            owner_id=self.instance_id,
            target_path=str(target.resolve()),
            expected_before_sha256=expected_before_sha256,
            desired_after_sha256=desired_after_sha256,
            candidate_path=str(candidate.resolve()),
            job_id=job_id,
            change_set_id=change_set_id,
            approval_id=approval_id,
            source_commit_id=source_commit_id,
            context=context,
        )
        self._persist(journal, create=True)
        return journal

    def update(
        self,
        transaction_id: str,
        phase: TransactionPhase,
        **updates: Any,
    ) -> TransactionJournal:
        with self._store_lock():
            current = self._read_unlocked(transaction_id)
            allowed = _ALLOWED_TRANSITIONS.get(current.phase, set())
            if phase != current.phase and phase not in allowed:
                raise ToolError(
                    "TRANSACTION_JOURNAL_INVALID",
                    f"transaction {transaction_id} cannot move from "
                    f"{current.phase.value} to {phase.value}.",
                    "Do not bypass transaction phases; inspect the journal and retry safely.",
                )
            payload = current.model_dump(mode="python")
            payload.update(updates)
            payload.update(
                {
                    "phase": phase,
                    "updated_at": _now(),
                    "owner_pid": os.getpid(),
                    "owner_id": self.instance_id,
                }
            )
            journal = TransactionJournal.model_validate(payload)
            self._write_json(self._path(transaction_id), journal.model_dump(mode="json"))
            return journal

    def get(self, transaction_id: str) -> TransactionJournal:
        with self._store_lock():
            return self._read_unlocked(transaction_id)

    def list(self) -> list[TransactionJournal]:
        journals: list[TransactionJournal] = []
        corrupt: list[Path] = []
        with self._store_lock():
            for path in self.records_dir.glob("txn-*.json"):
                try:
                    journals.append(TransactionJournal.model_validate_json(path.read_text("utf-8")))
                except (OSError, ValueError):
                    corrupt.append(path)
        self._corrupt_paths = tuple(corrupt)
        return sorted(journals, key=lambda item: item.created_at)

    def find_by_job(self, job_id: str) -> TransactionJournal | None:
        matches = [item for item in self.list() if item.job_id == job_id]
        return matches[-1] if matches else None

    def ensure_target_ready(self, target: Path) -> None:
        target_key = str(target.resolve())
        journals = self.list()
        if self._corrupt_paths:
            names = ", ".join(path.name for path in self._corrupt_paths[:3])
            raise ToolError(
                "TRANSACTION_JOURNAL_CORRUPT",
                f"transaction journal storage contains invalid records: {names}.",
                "Do not modify a target until the invalid journals and backups are inspected.",
            )
        unresolved = [
            item
            for item in journals
            if item.target_path == target_key and item.phase not in TERMINAL_TRANSACTION_PHASES
        ]
        failed = [
            item
            for item in journals
            if item.target_path == target_key and item.phase is TransactionPhase.RECOVERY_FAILED
        ]
        if unresolved or failed:
            journal = (unresolved or failed)[-1]
            raise ToolError(
                "TRANSACTION_RECOVERY_REQUIRED",
                f"transaction {journal.transaction_id} is unresolved at {journal.phase.value}.",
                "Inspect the journal and verified backup before attempting another write.",
            )

    def recover_incomplete(self) -> tuple[TransactionJournal, ...]:
        recovered: list[TransactionJournal] = []
        for journal in self.list():
            if journal.phase in TERMINAL_TRANSACTION_PHASES:
                continue
            result = self._recover(journal)
            if result is not None:
                recovered.append(result)
        return tuple(recovered)

    def _recover(self, journal: TransactionJournal) -> TransactionJournal | None:
        target = Path(journal.target_path)
        lock_path = target.with_name(f".{target.name}.ifc-console.lock")
        try:
            with exclusive_file_lock(
                lock_path,
                timeout_s=0.0,
                error_code="MODEL_BUSY",
            ):
                current_sha = sha256_file(target) if target.is_file() else None
                if journal.phase in PRE_COMMIT_PHASES:
                    if current_sha == journal.expected_before_sha256:
                        return self.update(
                            journal.transaction_id,
                            TransactionPhase.ABORTED,
                            error="owner exited before commit point",
                        )
                    return self._recovery_failed(
                        journal,
                        "target differs from its expected pre-commit hash",
                    )

                if self._receipt_is_durable(journal) and current_sha == journal.desired_after_sha256:
                    return self.update(
                        journal.transaction_id,
                        TransactionPhase.RECEIPT_PERSISTED,
                        receipt_artifact_id=journal.expected_receipt_id,
                    )

                if current_sha == journal.expected_before_sha256:
                    return self.update(
                        journal.transaction_id,
                        TransactionPhase.ROLLED_BACK,
                        error=journal.error or "target was already restored during recovery",
                    )

                if current_sha == journal.desired_after_sha256 and journal.rollback_artifact_id:
                    rollback = self.artifacts.verify(journal.rollback_artifact_id)
                    if rollback.sha256 != journal.expected_before_sha256:
                        return self._recovery_failed(
                            journal,
                            "rollback artifact does not match the expected source hash",
                        )
                    self.update(journal.transaction_id, TransactionPhase.ROLLBACK_STARTED)
                    self._replace_file(
                        target,
                        self.artifacts.content_path(rollback.artifact_id),
                        expected_sha256=journal.expected_before_sha256,
                    )
                    if sha256_file(target) != journal.expected_before_sha256:
                        raise OSError("recovered target checksum mismatch")
                    return self.update(
                        journal.transaction_id,
                        TransactionPhase.ROLLED_BACK,
                        error=journal.error or "rolled back after supervisor exit",
                    )

                return self._recovery_failed(
                    journal,
                    "target hash matches neither the expected source nor candidate",
                )
        except ToolError as exc:
            if exc.code == "MODEL_BUSY":
                return None
            return self._recovery_failed(journal, f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            return self._recovery_failed(journal, f"{type(exc).__name__}: {exc}")

    def _receipt_is_durable(self, journal: TransactionJournal) -> bool:
        if not journal.expected_receipt_id:
            return False
        try:
            receipt = self.artifacts.verify(journal.expected_receipt_id)
        except ToolError:
            return False
        expected_kind = (
            "ifc-commit-receipt"
            if journal.kind is TransactionKind.COMMIT
            else "ifc-restore-receipt"
        )
        expected_producer = (
            "commit_change_set"
            if journal.kind is TransactionKind.COMMIT
            else "restore_commit"
        )
        return receipt.kind == expected_kind and receipt.producer == expected_producer

    def _recovery_failed(
        self, journal: TransactionJournal, message: str
    ) -> TransactionJournal:
        try:
            return self.update(
                journal.transaction_id,
                TransactionPhase.RECOVERY_FAILED,
                error=message,
            )
        except Exception:
            return journal.model_copy(
                update={"phase": TransactionPhase.RECOVERY_FAILED, "error": message}
            )

    def _persist(self, journal: TransactionJournal, *, create: bool = False) -> None:
        with self._store_lock():
            path = self._path(journal.transaction_id)
            if create and path.exists():
                raise FileExistsError(path)
            self._write_json(path, journal.model_dump(mode="json"))

    def _read_unlocked(self, transaction_id: str) -> TransactionJournal:
        try:
            return TransactionJournal.model_validate_json(
                self._path(transaction_id).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ToolError(
                "TRANSACTION_JOURNAL_CORRUPT",
                f"transaction journal {transaction_id!r} is unavailable or invalid.",
                "Do not modify the target until the journal and backup are inspected.",
            ) from exc

    def _path(self, transaction_id: str) -> Path:
        return self.records_dir / f"{transaction_id}.json"

    def _store_lock(self):
        return _CombinedLock(
            self._lock,
            exclusive_file_lock(
                self.root / ".journal-store.lock",
                timeout_s=self.lock_timeout_s,
                error_code="TRANSACTION_JOURNAL_BUSY",
            ),
        )

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temp.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_retry(temp, path)
            _fsync_directory(path.parent)
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()

    @staticmethod
    def _replace_file(target: Path, source: Path, *, expected_sha256: str) -> None:
        temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.recovery.tmp")
        try:
            digest = hashlib.sha256()
            with source.open("rb") as reader, temp.open("xb") as writer:
                for chunk in iter(lambda: reader.read(1 << 20), b""):
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if digest.hexdigest() != expected_sha256:
                raise OSError("staged recovery checksum mismatch")
            _replace_with_retry(temp, target)
            _fsync_directory(target.parent)
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()

class _CombinedLock:
    def __init__(self, thread_lock: RLock, file_lock: Any) -> None:
        self.thread_lock = thread_lock
        self.file_lock = file_lock

    def __enter__(self) -> None:
        self.thread_lock.acquire()
        try:
            self.file_lock.__enter__()
        except Exception:
            self.thread_lock.release()
            raise

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            self.file_lock.__exit__(exc_type, exc, traceback)
        finally:
            self.thread_lock.release()


def _replace_with_retry(source: Path, target: Path, *, timeout_s: float = 2.0) -> None:
    """Tolerate transient Windows antivirus and file-sharing locks."""

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
