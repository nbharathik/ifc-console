"""Recent-models store: ~/.ifc-console/recents.json."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock

from ifc_console.application.locks import exclusive_file_lock
from ifc_console.core.results import ToolError


class RecentsStore:
    def __init__(self, path: Path, max_entries: int = 20) -> None:
        self.path = path
        self.max_entries = max_entries
        self._lock = RLock()

    def entries(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("entries", []) if isinstance(raw, dict) else []
        return [e for e in items if isinstance(e, dict) and e.get("path")]

    def touch(self, path: Path, *, size_bytes: int, schema: str, mode: str) -> None:
        key = str(path)
        try:
            with self._locked():
                current = self.entries()
                prev = next((item for item in current if item.get("path") == key), {})
                entries = [item for item in current if item.get("path") != key]
                entries.insert(
                    0,
                    {
                        "path": key,
                        "last_opened": datetime.now(timezone.utc).isoformat(),
                        "size_bytes": size_bytes,
                        "schema": schema,
                        "last_mode": mode,
                        "opens": int(prev.get("opens", 0)) + 1,
                    },
                )
                self._write(entries[: self.max_entries])
        except ToolError:
            pass

    def remove(self, path: str) -> None:
        try:
            with self._locked():
                self._write([item for item in self.entries() if item.get("path") != path])
        except ToolError:
            pass

    def clear(self) -> None:
        try:
            with self._locked():
                self._write([])
        except ToolError:
            pass

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        with self._lock, exclusive_file_lock(
            self.path.with_name(f".{self.path.name}.lock"), timeout_s=1
        ):
            yield

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Per-write temp name: two consoles sharing one fixed name clobber each
        # other's file and can leave the list empty. Recents are a convenience,
        # so a failed write is never worth an exception.
        tmp = self.path.with_name(f".{self.path.name}.{secrets.token_hex(4)}.tmp")
        try:
            tmp.write_text(
                json.dumps({"version": 1, "entries": entries}, indent=2, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                tmp.unlink()
