"""Recent-models store: ~/.ifc-console/recents.json."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path


class RecentsStore:
    def __init__(self, path: Path, max_entries: int = 20) -> None:
        self.path = path
        self.max_entries = max_entries

    def entries(self) -> list[dict]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        items = raw.get("entries", []) if isinstance(raw, dict) else []
        return [e for e in items if isinstance(e, dict) and e.get("path")]

    def touch(self, path: Path, *, size_bytes: int, schema: str, mode: str) -> None:
        key = str(path)
        entries = [e for e in self.entries() if e.get("path") != key]
        prev = next((e for e in self.entries() if e.get("path") == key), {})
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

    def remove(self, path: str) -> None:
        self._write([e for e in self.entries() if e.get("path") != path])

    def clear(self) -> None:
        self._write([])

    def _write(self, entries: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"version": 1, "entries": entries}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
