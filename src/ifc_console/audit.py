"""Versioned, redacted, integrity-chained local audit events."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from ifc_console.core.context import current_operation_context

AUDIT_SCHEMA_VERSION = "1"
_REDACTED = "[REDACTED]"
_SENSITIVE_KEYS = re.compile(
    r"(^|[_-])(api[_-]?key|authorization|cookie|credential|password|secret|token)($|[_-])",
    re.IGNORECASE,
)
_PROPERTY_VALUE_KEYS = frozenset(
    {"after", "before", "nominal_value", "property_value", "property_values", "value"}
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/-]{8,}={0,2}"),
    re.compile(r"\bsk-[a-zA-Z0-9_-]{8,}\b"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{8,}\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\b"),
)


class AuditVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    valid: bool
    event_count: int
    error: str | None = None


class AuditLog:
    def __init__(self, sessions_dir: Path, retention: int = 50) -> None:
        self.sessions_dir = sessions_dir
        self.retention = retention
        self.session_id: str | None = None
        self._lock = threading.Lock()
        self._sequence = 0
        self._previous_hash: str | None = None

    def start(self, meta: dict) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        self.session_id = f"{stamp}-{secrets.token_hex(2)}"
        self._sequence = 0
        self._previous_hash = None
        d = self.sessions_dir / self.session_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "meta.json").write_text(
            json.dumps(
                {
                    "schema_version": AUDIT_SCHEMA_VERSION,
                    "session_id": self.session_id,
                    **_redact(meta),
                },
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        self.record("session_start")
        self._prune()
        return self.session_id

    def record(self, ev: str, **fields: Any) -> dict[str, Any] | None:
        if self.session_id is None:
            return None
        if "code" in fields:
            source = str(fields.pop("code"))
            fields["code_sha256"] = hashlib.sha256(source.encode("utf-8")).hexdigest()
            fields["code_chars"] = len(source)
        context = current_operation_context()
        context_fields: dict[str, Any] = {}
        if context is not None:
            context_fields = {
                "correlation_id": context.correlation_id,
                "request_id": context.request_id,
                "actor": context.actor,
                "client": context.client,
                "transport": context.transport,
                "workspace_id": context.workspace_id,
                "model_id": context.model_id,
                "revision_id": context.revision_id,
                "operation": context.operation,
                "job_id": context.job_id,
                "authority": context.authority,
            }
        payload = {
            key: value
            for key, value in {**context_fields, **fields}.items()
            if value is not None
        }
        path = self.sessions_dir / self.session_id / "audit.jsonl"
        with self._lock:
            self._sequence += 1
            line = {
                "schema_version": AUDIT_SCHEMA_VERSION,
                "event_id": f"{self.session_id}:{self._sequence}",
                "sequence": self._sequence,
                "previous_hash": self._previous_hash,
                "ts": datetime.now(timezone.utc).isoformat(),
                "ev": ev,
                **_redact(payload),
            }
            line["event_hash"] = _event_hash(line)
            self._previous_hash = line["event_hash"]
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return line

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

    def verify_session(self, session_id: str | None = None) -> AuditVerification:
        selected = session_id or self.session_id
        if selected is None:
            return AuditVerification(
                session_id="", valid=False, event_count=0, error="no audit session selected"
            )
        path = self.sessions_dir / selected / "audit.jsonl"
        previous: str | None = None
        count = 0
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            return AuditVerification(
                session_id=selected, valid=False, event_count=0, error=str(exc)
            )
        for raw in lines:
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                return AuditVerification(
                    session_id=selected,
                    valid=False,
                    event_count=count,
                    error=f"invalid JSON at event {count + 1}: {exc}",
                )
            count += 1
            if event.get("schema_version") != AUDIT_SCHEMA_VERSION:
                return AuditVerification(
                    session_id=selected,
                    valid=False,
                    event_count=count,
                    error=f"unsupported schema at event {count}",
                )
            if event.get("sequence") != count or event.get("previous_hash") != previous:
                return AuditVerification(
                    session_id=selected,
                    valid=False,
                    event_count=count,
                    error=f"broken sequence or chain at event {count}",
                )
            actual = event.get("event_hash")
            if not isinstance(actual, str) or actual != _event_hash(event):
                return AuditVerification(
                    session_id=selected,
                    valid=False,
                    event_count=count,
                    error=f"hash mismatch at event {count}",
                )
            previous = actual
        return AuditVerification(session_id=selected, valid=True, event_count=count)

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


def _event_hash(event: dict[str, Any]) -> str:
    payload = {key: value for key, value in event.items() if key != "event_hash"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if _SENSITIVE_KEYS.search(lowered) or lowered in _PROPERTY_VALUE_KEYS:
        return _REDACTED
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        return redacted
    return value
