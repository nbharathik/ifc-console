"""Durable transaction journal recovery without an active IFC session."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from threading import Lock, Thread

import pytest

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import exclusive_file_lock
from ifc_console.application.transaction_journals import TransactionJournalStore
from ifc_console.automation.files import sha256_file
from ifc_console.core.results import ToolError
from ifc_console.core.transaction_journal import TransactionKind, TransactionPhase


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _store(tmp_path: Path) -> tuple[TransactionJournalStore, ArtifactService]:
    artifacts = ArtifactService(tmp_path / "artifacts")
    return TransactionJournalStore(tmp_path / "transactions", artifacts), artifacts


def test_recovery_aborts_a_pre_commit_journal_without_touching_target(tmp_path: Path) -> None:
    store, _artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    candidate = tmp_path / "candidate.ifc"
    before = _write(target, b"before")
    after = _write(candidate, b"after")
    journal = store.create(
        kind=TransactionKind.COMMIT,
        target=target,
        expected_before_sha256=before,
        desired_after_sha256=after,
        candidate=candidate,
    )
    store.update(journal.transaction_id, TransactionPhase.CANDIDATE_VERIFIED)
    recovered = store.recover_incomplete()

    assert recovered[0].phase is TransactionPhase.ABORTED
    assert sha256_file(target) == before
    store.ensure_target_ready(target)


def test_recovery_rolls_back_verified_candidate_bytes(tmp_path: Path) -> None:
    store, artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    candidate = tmp_path / "candidate.ifc"
    original = tmp_path / "original.ifc"
    before = _write(original, b"before")
    after = _write(candidate, b"after")
    _write(target, b"after")
    backup = artifacts.put_file(
        original,
        name="backup.ifc",
        kind="ifc-verified-backup",
        media_type="application/x-step",
        producer="commit_change_set",
        expected_sha256=before,
    )
    journal = store.create(
        kind=TransactionKind.COMMIT,
        target=target,
        expected_before_sha256=before,
        desired_after_sha256=after,
        candidate=candidate,
    )
    store.update(journal.transaction_id, TransactionPhase.CANDIDATE_VERIFIED)
    store.update(
        journal.transaction_id,
        TransactionPhase.BACKUP_VERIFIED,
        rollback_artifact_id=backup.artifact_id,
    )
    store.update(journal.transaction_id, TransactionPhase.COMMIT_POINT)
    recovered = store.recover_incomplete()

    assert recovered[0].phase is TransactionPhase.ROLLED_BACK
    assert sha256_file(target) == before
    store.ensure_target_ready(target)


def test_recovery_accepts_a_durable_receipt_after_target_replacement(tmp_path: Path) -> None:
    store, artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    candidate = tmp_path / "candidate.ifc"
    before = hashlib.sha256(b"before").hexdigest()
    after = _write(target, b"after")
    _write(candidate, b"after")
    receipt = artifacts.put_text(
        '{"committed": true}',
        name="commit.json",
        kind="ifc-commit-receipt",
        media_type="application/vnd.ifc-console.commit+json",
        producer="commit_change_set",
    )
    journal = store.create(
        kind=TransactionKind.COMMIT,
        target=target,
        expected_before_sha256=before,
        desired_after_sha256=after,
        candidate=candidate,
    )
    store.update(journal.transaction_id, TransactionPhase.CANDIDATE_VERIFIED)
    store.update(journal.transaction_id, TransactionPhase.BACKUP_VERIFIED)
    store.update(journal.transaction_id, TransactionPhase.COMMIT_POINT)
    store.update(journal.transaction_id, TransactionPhase.TARGET_VERIFIED)
    store.update(
        journal.transaction_id,
        TransactionPhase.RECEIPT_PREPARED,
        expected_receipt_id=receipt.artifact_id,
    )
    recovered = store.recover_incomplete()

    assert recovered[0].phase is TransactionPhase.RECEIPT_PERSISTED
    assert recovered[0].receipt_artifact_id == receipt.artifact_id
    assert sha256_file(target) == after


def test_unknown_target_hash_requires_manual_recovery_and_blocks_writes(tmp_path: Path) -> None:
    store, _artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    candidate = tmp_path / "candidate.ifc"
    before = hashlib.sha256(b"before").hexdigest()
    after = _write(candidate, b"after")
    _write(target, b"third-state")
    journal = store.create(
        kind=TransactionKind.RESTORE,
        target=target,
        expected_before_sha256=before,
        desired_after_sha256=after,
        candidate=candidate,
    )
    store.update(journal.transaction_id, TransactionPhase.CANDIDATE_VERIFIED)
    store.update(journal.transaction_id, TransactionPhase.BACKUP_VERIFIED)
    store.update(journal.transaction_id, TransactionPhase.COMMIT_POINT)
    recovered = store.recover_incomplete()

    assert recovered[0].phase is TransactionPhase.RECOVERY_FAILED
    with pytest.raises(ToolError) as excinfo:
        store.ensure_target_ready(target)
    assert excinfo.value.code == "TRANSACTION_RECOVERY_REQUIRED"


def test_corrupt_journal_blocks_new_writes(tmp_path: Path) -> None:
    store, _artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    _write(target, b"before")
    (store.records_dir / "txn-deadbeefdeadbeef.json").write_text("{", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        store.ensure_target_ready(target)
    assert excinfo.value.code == "TRANSACTION_JOURNAL_CORRUPT"


def test_legacy_owner_lock_is_serialized_without_stale_takeover(tmp_path: Path) -> None:
    lock = tmp_path / ".model.ifc.ifc-console.lock"
    lock.write_text("{", encoding="utf-8")
    state_lock = Lock()
    active = 0
    peak = 0

    def contend() -> None:
        nonlocal active, peak
        with exclusive_file_lock(lock, timeout_s=2):
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            with state_lock:
                active -= 1

    contenders = [Thread(target=contend) for _ in range(2)]
    for contender in contenders:
        contender.start()
    for contender in contenders:
        contender.join(timeout=5)

    assert peak == 1
    assert all(not contender.is_alive() for contender in contenders)
    assert lock.exists()


def test_transaction_lock_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_bytes(b"do not change")
    lock = tmp_path / ".model.ifc.ifc-console.lock"
    try:
        lock.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with (
        pytest.raises(ToolError) as excinfo,
        exclusive_file_lock(lock, timeout_s=0, error_code="MODEL_BUSY"),
    ):
        pass

    assert excinfo.value.code == "MODEL_BUSY"
    assert "unsafe lock file" in excinfo.value.message
    assert outside.read_bytes() == b"do not change"


def test_journal_rejects_skipped_or_reversed_phases(tmp_path: Path) -> None:
    store, _artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    candidate = tmp_path / "candidate.ifc"
    before = _write(target, b"before")
    after = _write(candidate, b"after")
    journal = store.create(
        kind=TransactionKind.COMMIT,
        target=target,
        expected_before_sha256=before,
        desired_after_sha256=after,
        candidate=candidate,
    )

    with pytest.raises(ToolError) as excinfo:
        store.update(journal.transaction_id, TransactionPhase.COMMIT_POINT)
    assert excinfo.value.code == "TRANSACTION_JOURNAL_INVALID"
    assert store.get(journal.transaction_id).phase is TransactionPhase.PREPARED


@pytest.mark.parametrize(
    ("phase", "expected_phase", "expected_bytes"),
    [
        ("prepared", TransactionPhase.ABORTED, b"before"),
        ("candidate_verified", TransactionPhase.ABORTED, b"before"),
        ("backup_verified", TransactionPhase.ABORTED, b"before"),
        ("commit_point", TransactionPhase.ROLLED_BACK, b"before"),
        ("target_verified", TransactionPhase.ROLLED_BACK, b"before"),
        ("receipt_prepared", TransactionPhase.RECEIPT_PERSISTED, b"after"),
    ],
)
def test_killed_supervisor_is_recovered_at_every_durable_phase(
    tmp_path: Path,
    phase: str,
    expected_phase: TransactionPhase,
    expected_bytes: bytes,
) -> None:
    root = tmp_path / phase
    target = root / "model.ifc"
    candidate = root / "candidate.ifc"
    script = textwrap.dedent(
        """
        import hashlib
        import os
        import sys
        from pathlib import Path

        from ifc_console.application.artifacts import ArtifactService
        from ifc_console.application.transaction_journals import TransactionJournalStore
        from ifc_console.core.transaction_journal import TransactionKind, TransactionPhase

        root, target, candidate = map(Path, sys.argv[1:4])
        stop = sys.argv[4]
        root.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"before")
        candidate.write_bytes(b"after")
        original = root / "original.ifc"
        original.write_bytes(b"before")
        digest = lambda value: hashlib.sha256(value).hexdigest()
        artifacts = ArtifactService(root / "artifacts")
        store = TransactionJournalStore(root / "transactions", artifacts)
        journal = store.create(
            kind=TransactionKind.COMMIT,
            target=target,
            expected_before_sha256=digest(b"before"),
            desired_after_sha256=digest(b"after"),
            candidate=candidate,
        )
        if stop == "prepared": os._exit(91)
        journal = store.update(journal.transaction_id, TransactionPhase.CANDIDATE_VERIFIED)
        if stop == "candidate_verified": os._exit(91)
        backup = artifacts.put_file(
            original,
            name="backup.ifc",
            kind="ifc-verified-backup",
            media_type="application/x-step",
            producer="commit_change_set",
            expected_sha256=digest(b"before"),
        )
        journal = store.update(
            journal.transaction_id,
            TransactionPhase.BACKUP_VERIFIED,
            rollback_artifact_id=backup.artifact_id,
        )
        if stop == "backup_verified": os._exit(91)
        journal = store.update(journal.transaction_id, TransactionPhase.COMMIT_POINT)
        target.write_bytes(b"after")
        if stop == "commit_point": os._exit(91)
        journal = store.update(journal.transaction_id, TransactionPhase.TARGET_VERIFIED)
        if stop == "target_verified": os._exit(91)
        receipt = artifacts.put_text(
            '{"committed": true}',
            name="commit.json",
            kind="ifc-commit-receipt",
            media_type="application/vnd.ifc-console.commit+json",
            producer="commit_change_set",
        )
        store.update(
            journal.transaction_id,
            TransactionPhase.RECEIPT_PREPARED,
            expected_receipt_id=receipt.artifact_id,
        )
        os._exit(91)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), str(target), str(candidate), phase],
        check=False,
        timeout=30,
    )
    assert result.returncode == 91

    store = TransactionJournalStore(root / "transactions", ArtifactService(root / "artifacts"))
    recovered = store.recover_incomplete()

    assert recovered[0].phase is expected_phase
    assert target.read_bytes() == expected_bytes
    store.ensure_target_ready(target)


def test_disk_full_during_journal_fsync_preserves_previous_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _artifacts = _store(tmp_path)
    target = tmp_path / "model.ifc"
    candidate = tmp_path / "candidate.ifc"
    before = _write(target, b"before")
    after = _write(candidate, b"after")
    journal = store.create(
        kind=TransactionKind.COMMIT,
        target=target,
        expected_before_sha256=before,
        desired_after_sha256=after,
        candidate=candidate,
    )
    journal_path = store.records_dir / f"{journal.transaction_id}.json"
    persisted = journal_path.read_bytes()

    def disk_full(_fd: int) -> None:
        raise OSError(28, "injected disk full")

    monkeypatch.setattr(os, "fsync", disk_full)
    with pytest.raises(OSError, match="disk full"):
        store._write_json(
            journal_path,
            journal.model_copy(update={"error": "must not persist"}).model_dump(mode="json"),
        )

    assert journal_path.read_bytes() == persisted
    assert not list(store.records_dir.glob("*.tmp"))


def test_journal_replace_retries_transient_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ifc_console.application.transaction_journals as journals_module

    store, _artifacts = _store(tmp_path)
    path = store.records_dir / "retry.json"
    real_replace = os.replace
    attempts = 0

    def flaky_replace(source: Path, target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("injected sharing violation")
        real_replace(source, target)

    monkeypatch.setattr(journals_module.os, "replace", flaky_replace)
    store._write_json(path, {"ok": True})

    assert attempts == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"ok": True}
