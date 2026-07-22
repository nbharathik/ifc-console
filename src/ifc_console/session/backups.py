"""Pre-overwrite snapshots: ~/.ifc-console/backups.

Backup failure aborts the save; never save without the backup.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path


class BackupStore:
    def __init__(self, directory: Path, retention: int = 20) -> None:
        self.directory = directory
        self.retention = retention

    def backup(self, target: Path) -> Path | None:
        """Copy `target` aside before it gets overwritten. None if it doesn't exist."""
        if not target.exists():
            return None
        self.directory.mkdir(parents=True, exist_ok=True)
        stat = target.stat()
        fp = hashlib.sha256(f"{stat.st_size}|{stat.st_mtime_ns}".encode()).hexdigest()[:8]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = self.directory / f"{target.stem}.{fp}.{stamp}.ifc"
        n = 1
        while dest.exists():
            dest = self.directory / f"{target.stem}.{fp}.{stamp}-{n}.ifc"
            n += 1
        shutil.copy2(target, dest)
        self._prune(target.stem)
        return dest

    def list_for(self, stem: str) -> list[Path]:
        if not self.directory.exists():
            return []
        return sorted(
            self.directory.glob(f"{stem}.*.ifc"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def _prune(self, stem: str) -> None:
        for old in self.list_for(stem)[self.retention :]:
            with contextlib.suppress(OSError):  # pruning is best-effort
                old.unlink()
