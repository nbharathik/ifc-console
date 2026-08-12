"""Durable state for commit and restore recovery."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ifc_console._compat import StrEnum
from ifc_console.core.context import OperationContext


class TransactionKind(StrEnum):
    COMMIT = "commit"
    RESTORE = "restore"


class TransactionPhase(StrEnum):
    PREPARED = "prepared"
    CANDIDATE_VERIFIED = "candidate_verified"
    BACKUP_VERIFIED = "backup_verified"
    COMMIT_POINT = "commit_point"
    TARGET_VERIFIED = "target_verified"
    RECEIPT_PREPARED = "receipt_prepared"
    RECEIPT_PERSISTED = "receipt_persisted"
    ROLLBACK_STARTED = "rollback_started"
    ROLLED_BACK = "rolled_back"
    ABORTED = "aborted"
    RECOVERY_FAILED = "recovery_failed"


TERMINAL_TRANSACTION_PHASES = frozenset(
    {
        TransactionPhase.RECEIPT_PERSISTED,
        TransactionPhase.ROLLED_BACK,
        TransactionPhase.ABORTED,
        TransactionPhase.RECOVERY_FAILED,
    }
)

PRE_COMMIT_PHASES = frozenset(
    {
        TransactionPhase.PREPARED,
        TransactionPhase.CANDIDATE_VERIFIED,
        TransactionPhase.BACKUP_VERIFIED,
    }
)


class TransactionJournal(BaseModel):
    """One fsynced state machine for an atomic target replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{16}$")
    kind: TransactionKind
    created_at: datetime
    updated_at: datetime
    phase: TransactionPhase
    owner_pid: int = Field(ge=0)
    owner_id: str
    target_path: str
    expected_before_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    desired_after_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_path: str | None = None
    rollback_artifact_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    expected_receipt_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_artifact_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    job_id: str | None = Field(default=None, pattern=r"^job-[0-9a-f]{16}$")
    change_set_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    approval_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    source_commit_id: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    result_document: dict[str, Any] | None = None
    context: OperationContext | None = None
    error: str | None = None
    rollback_error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_TRANSACTION_PHASES

    @property
    def cancellable(self) -> bool:
        return self.phase in PRE_COMMIT_PHASES
