"""Backups + recents (plan 04 §7, 07 §5)."""

from __future__ import annotations

import json
from pathlib import Path

from ifc_console.recents import RecentsStore
from ifc_console.session.backups import BackupStore


def test_backup_created_and_named(tmp_path: Path) -> None:
    target = tmp_path / "model.ifc"
    target.write_text("v1")
    store = BackupStore(tmp_path / "backups")
    backup = store.backup(target)
    assert backup is not None and backup.exists()
    assert backup.read_text() == "v1"
    assert backup.stem.startswith("model.")
    manifest = json.loads(backup.with_suffix(".json").read_text(encoding="utf-8"))
    assert manifest["source_path"] == str(target.resolve())
    assert manifest["backup_file"] == backup.name
    assert len(manifest["source_sha256"]) == 64


def test_backup_none_when_target_absent(tmp_path: Path) -> None:
    store = BackupStore(tmp_path / "backups")
    assert store.backup(tmp_path / "missing.ifc") is None


def test_backup_retention_prunes(tmp_path: Path) -> None:
    target = tmp_path / "m.ifc"
    store = BackupStore(tmp_path / "b", retention=3)
    for i in range(6):
        target.write_text(f"v{i}")
        store.backup(target)
    assert len(store.list_for(target)) == 3
    assert len(list((tmp_path / "b").glob("*.json"))) == 3


def test_same_named_models_have_independent_retention(tmp_path: Path) -> None:
    first = tmp_path / "a" / "model.ifc"
    second = tmp_path / "b" / "model.ifc"
    first.parent.mkdir()
    second.parent.mkdir()
    store = BackupStore(tmp_path / "backups", retention=2)

    for index in range(3):
        first.write_text(f"a{index}")
        second.write_text(f"b{index}")
        store.backup(first)
        store.backup(second)

    first_backups = store.list_for(first)
    second_backups = store.list_for(second)
    assert len(first_backups) == len(second_backups) == 2
    assert set(first_backups).isdisjoint(second_backups)
    assert {path.read_text() for path in first_backups} <= {"a1", "a2"}
    assert {path.read_text() for path in second_backups} <= {"b1", "b2"}


def test_recents_mru_and_cap(tmp_path: Path) -> None:
    store = RecentsStore(tmp_path / "recents.json", max_entries=2)
    for name in ("a", "b", "c"):
        store.touch(tmp_path / f"{name}.ifc", size_bytes=1, schema="IFC4", mode="ask")
    entries = store.entries()
    assert len(entries) == 2
    assert Path(entries[0]["path"]).name == "c.ifc"  # most recent first


def test_recents_dedupes_and_counts_opens(tmp_path: Path) -> None:
    store = RecentsStore(tmp_path / "recents.json")
    p = tmp_path / "a.ifc"
    store.touch(p, size_bytes=1, schema="IFC4", mode="ask")
    store.touch(p, size_bytes=1, schema="IFC4", mode="ask")
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0]["opens"] == 2
    assert entries[0]["last_mode"] == "ask"
