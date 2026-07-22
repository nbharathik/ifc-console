"""Append-only session audit log: ~/.ifc-console/sessions/<id>/audit.jsonl."""

from __future__ import annotations

import json
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

CODE_HEAD_LIMIT = 2_000


class AuditLog:
    def __init__(self, sessions_dir: Path, retention: int = 50) -> None:
        self.sessions_dir = sessions_dir
        self.retention = retention
        self.session_id: str | None = None
        self._lock = threading.Lock()

    def start(self, meta: dict) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-{secrets.token_hex(2)}"
        d = self.sessions_dir / self.session_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            json.dumps({"session_id": self.session_id, **meta}, indent=2, default=str),
            encoding="utf-8",
        )
        self.record("session_start")
        self._prune()
        return self.session_id

    def record(self, ev: str, **fields) -> None:
        if self.session_id is None:
            return
        if "code" in fields:
            code = fields.pop("code")
            fields["code_head"] = code[:CODE_HEAD_LIMIT]
        line = {"ts": datetime.now(timezone.utc).isoformat(), "ev": ev, **fields}
        path = self.sessions_dir / self.session_id / "audit.jsonl"
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")

    def end(self) -> None:
        self.record("session_end")

    # -- reading (TUI Audit tab, `ifc-console sessions`) ----------------------
    def list_sessions(self) -> list[str]:
        if not self.sessions_dir.exists():
            return []
        return sorted((p.name for p in self.sessions_dir.iterdir() if p.is_dir()), reverse=True)

    def tail(self, count: int = 10) -> list[dict]:
        """Last `count` records of the current session (console /audit)."""
        if self.session_id is None:
            return []
        return self.read_session(self.session_id)[-count:]

    def read_session(self, session_id: str) -> list[dict]:
        path = self.sessions_dir / session_id / "audit.jsonl"
        out: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass
        return out

    def clear(self) -> int:
        ids = [s for s in self.list_sessions() if s != self.session_id]
        for sid in ids:
            shutil.rmtree(self.sessions_dir / sid, ignore_errors=True)
        return len(ids)

    def _prune(self) -> None:
        ids = self.list_sessions()
        for sid in ids[self.retention :]:
            if sid != self.session_id:
                shutil.rmtree(self.sessions_dir / sid, ignore_errors=True)
