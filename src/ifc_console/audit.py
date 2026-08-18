"""Versioned, redacted, integrity-chained local audit events."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import shutil
import threading
from collections.abc import Iterator
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
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|authorization|credential|password|"
        r"passwd|secret|token)\s*[:=]\s*[\"']?[^\s,\"';]+"
    ),
    re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,31}://[^\s/@:]+:[^\s/@]+@"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
)
_URL_FRAGMENT_TOKEN = re.compile(r"(?i)(#t=)[^&\s\"'<>]+")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_MAX_AUDIT_BYTES = 64 * 1024 * 1024
_MAX_AUDIT_LINE_BYTES = 8 * 1024 * 1024


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
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        for _attempt in range(10):
            session_id = f"{stamp}-{secrets.token_hex(8)}"
            d = self.sessions_dir / session_id
            try:
                d.mkdir(mode=0o700)
            except FileExistsError:
                continue
            self.session_id = session_id
            break
        else:
            raise RuntimeError("could not allocate a unique audit session")
        self._sequence = 0
        self._previous_hash = None
        meta_path = d / "meta.json"
        meta_path.write_text(
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
        with contextlib.suppress(OSError):
            meta_path.chmod(0o600)
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
            key: value for key, value in {**context_fields, **fields}.items() if value is not None
        }
        session_dir = self._session_dir(self.session_id)
        if session_dir is None:
            raise RuntimeError("invalid audit session path")
        path = session_dir / "audit.jsonl"
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
            with contextlib.suppress(OSError):
                path.chmod(0o600)
        return line

    def end(self) -> None:
        self.record("session_end")

    # -- reading (TUI Audit tab, `ifc-console sessions`) ----------------------
    def list_sessions(self) -> list[str]:
        if not self.sessions_dir.exists():
            return []
        return sorted(
            (
                p.name
                for p in self.sessions_dir.iterdir()
                if p.is_dir() and self._session_dir(p.name) is not None
            ),
            reverse=True,
        )

    def tail(self, count: int = 10) -> list[dict]:
        """Last `count` records of the current session (console /audit)."""
        if self.session_id is None:
            return []
        return self.read_session(self.session_id)[-count:]

    def read_session(self, session_id: str) -> list[dict]:
        session_dir = self._session_dir(session_id)
        if session_dir is None:
            return []
        path = session_dir / "audit.jsonl"
        out: list[dict] = []
        try:
            for line in _bounded_audit_lines(path):
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except (OSError, UnicodeDecodeError, ValueError):
            return []
        return out

    def verify_session(self, session_id: str | None = None) -> AuditVerification:
        selected = session_id or self.session_id
        if selected is None:
            return AuditVerification(
                session_id="", valid=False, event_count=0, error="no audit session selected"
            )
        session_dir = self._session_dir(selected)
        if session_dir is None:
            return AuditVerification(
                session_id=selected,
                valid=False,
                event_count=0,
                error="invalid audit session id or path",
            )
        path = session_dir / "audit.jsonl"
        previous: str | None = None
        count = 0
        try:
            for raw in _bounded_audit_lines(path):
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
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            return AuditVerification(
                session_id=selected, valid=False, event_count=count, error=str(exc)
            )
        return AuditVerification(session_id=selected, valid=True, event_count=count)

    def clear(self) -> int:
        ids = [s for s in self.list_sessions() if s != self.session_id]
        for sid in ids:
            session_dir = self._session_dir(sid)
            if session_dir is not None:
                shutil.rmtree(session_dir, ignore_errors=True)
        return len(ids)

    def _prune(self) -> None:
        ids = self.list_sessions()
        for sid in ids[self.retention :]:
            if sid != self.session_id:
                session_dir = self._session_dir(sid)
                if session_dir is not None:
                    shutil.rmtree(session_dir, ignore_errors=True)

    def _session_dir(self, session_id: str) -> Path | None:
        if not _SESSION_ID.fullmatch(session_id):
            return None
        candidate = self.sessions_dir / session_id
        try:
            root = self.sessions_dir.resolve(strict=False)
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        if resolved.parent != root or candidate.is_symlink():
            return None
        return candidate


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


def _bounded_audit_lines(path: Path) -> Iterator[str]:
    total = 0
    with path.open("rb") as handle:
        while raw := handle.readline(_MAX_AUDIT_LINE_BYTES + 1):
            if len(raw) > _MAX_AUDIT_LINE_BYTES:
                raise ValueError("audit event exceeds the line-size limit")
            total += len(raw)
            if total > _MAX_AUDIT_BYTES:
                raise ValueError("audit session exceeds the file-size limit")
            yield raw.decode("utf-8").rstrip("\r\n")


def _redact(value: Any, *, key: str = "") -> Any:
    lowered = key.lower()
    if _SENSITIVE_KEYS.search(lowered) or lowered in _PROPERTY_VALUE_KEYS:
        return _REDACTED
    if isinstance(value, dict):
        return {str(item_key): _redact(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item, key=key) for item in value]
    if isinstance(value, str):
        redacted = _URL_FRAGMENT_TOKEN.sub(
            lambda match: f"{match.group(1)}{_REDACTED}", value
        )
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(_REDACTED, redacted)
        return redacted
    return value
