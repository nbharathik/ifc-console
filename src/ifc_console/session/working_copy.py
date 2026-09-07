"""Edit-mode working copies: the file the user opened is never written.

Entering edit mode snapshots the loaded file into ~/.ifc-console/working and
points the session at that copy, so every later save, reload and download
touches the copy while the original stays byte-for-byte as it was opened.
"""

from __future__ import annotations

import contextlib
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkingCopy:
    path: Path
    origin: Path
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "origin": str(self.origin),
            "origin_name": self.origin.name,
            "created_at": self.created_at,
        }


class WorkingCopyStore:
    """Snapshots kept outside the user's folders, pruned to a small budget."""

    def __init__(self, directory: Path, retention: int = 10) -> None:
        self.directory = directory
        self.retention = retention

    def create(self, origin: Path) -> WorkingCopy:
        """Byte-copy `origin` aside. Blocking; call it off the event loop."""
        self.directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        suffix = origin.suffix or ".ifc"
        dest = self.directory / f"{origin.stem}.edit-{stamp}{suffix}"
        n = 1
        while dest.exists():
            dest = self.directory / f"{origin.stem}.edit-{stamp}-{n}{suffix}"
            n += 1
        shutil.copyfile(origin, dest)
        copy = WorkingCopy(
            path=dest.resolve(),
            origin=origin,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self.prune(protect=(copy.path,))
        return copy

    def entries(self) -> list[Path]:
        if not self.directory.exists():
            return []
        files = [p for p in self.directory.glob("*.edit-*") if p.is_file()]
        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)

    def prune(self, *, protect: tuple[Path, ...] = ()) -> None:
        keep = {p.resolve() for p in protect}
        for old in self.entries()[self.retention :]:
            if old.resolve() in keep:
                continue
            with contextlib.suppress(OSError):  # pruning is best-effort
                old.unlink()
