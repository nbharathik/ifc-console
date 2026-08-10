"""Pre-overwrite snapshots: ~/.ifc-console/backups.

Backup failure aborts the save; never save without the backup.
"""

from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BackupStore:
    def __init__(self, directory: Path, retention: int = 20) -> None:
        self.directory = directory
        self.retention = retention

    def backup(self, target: Path) -> Path | None:
        """Copy `target` aside before it gets overwritten. None if it doesn't exist."""
        if not target.exists():
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        target = target.resolve()
        namespace = self._namespace(target)
        source_sha256, source_size = self._hash_file(target)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = self.directory / f"{namespace}.{stamp}.{source_sha256[:12]}.ifc"
        n = 1
        while dest.exists():
            dest = self.directory / f"{namespace}.{stamp}-{n}.{source_sha256[:12]}.ifc"
            n += 1
        manifest = dest.with_suffix(".json")
        try:
            shutil.copy2(target, dest)
            backup_sha256, backup_size = self._hash_file(dest)
            current_sha256, current_size = self._hash_file(target)
            if (
                backup_sha256 != source_sha256
                or backup_size != source_size
                or current_sha256 != source_sha256
                or current_size != source_size
            ):
                raise OSError(f"source changed while creating backup for {target}")
            self._write_manifest(
                manifest,
                {
                    "version": 1,
                    "source_path": str(target),
                    "source_sha256": source_sha256,
                    "size_bytes": source_size,
                    "backup_file": dest.name,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            with contextlib.suppress(OSError):
                dest.unlink()
            with contextlib.suppress(OSError):
                manifest.unlink()
            raise
        self._prune(target)
        return dest

    def list_for(self, target: Path) -> list[Path]:
        if not self.directory.exists():
            return []
        namespace = self._namespace(target.resolve())
        return sorted(
            self.directory.glob(f"{glob.escape(namespace)}.*.ifc"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def _prune(self, target: Path) -> None:
        for old in self.list_for(target)[self.retention :]:
            with contextlib.suppress(OSError):  # pruning is best-effort
                old.unlink()
            with contextlib.suppress(OSError):
                old.with_suffix(".json").unlink()

    @staticmethod
    def _namespace(target: Path) -> str:
        canonical = os.path.normcase(str(target))
        path_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
        return f"{target.stem}.{path_id}"

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        return digest.hexdigest(), size

    @staticmethod
    def _write_manifest(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
