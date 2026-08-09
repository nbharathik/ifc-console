"""Audit redaction, context, and integrity-chain behavior."""

from __future__ import annotations

import json
from pathlib import Path

from ifc_console.audit import AuditLog
from ifc_console.core.context import OperationContext, bind_operation_context


def _context() -> OperationContext:
    return OperationContext(
        correlation_id="corr-0123456789abcdef0123456789abcdef",
        workspace_id="workspace-test",
        transport="sdk",
        mode="ask",
        model_id="model-test",
        revision_id="revision-test",
        actor="bim-manager",
        client="pytest",
        authority="caller",
        operation="preview_property_change",
        request_id="request-7",
    )


def test_audit_redacts_secrets_source_and_property_values(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    audit.start({"api_key": "sk-meta-secret-value"})
    source = "print('confidential model value')"
    with bind_operation_context(_context()):
        event = audit.record(
            "test_event",
            code=source,
            api_token="sk-live-secret-value",
            before="F30",
            after="F60",
            message="Authorization: Bearer abcdefghijklmnop",
        )

    assert event is not None
    rendered = json.dumps(event)
    assert source not in rendered
    assert "sk-live-secret-value" not in rendered
    assert "abcdefghijklmnop" not in rendered
    assert event["code_chars"] == len(source)
    assert len(event["code_sha256"]) == 64
    assert event["before"] == "[REDACTED]"
    assert event["after"] == "[REDACTED]"
    assert event["correlation_id"] == _context().correlation_id
    assert event["actor"] == "bim-manager"
    assert event["operation"] == "preview_property_change"

    meta = (tmp_path / audit.session_id / "meta.json").read_text(encoding="utf-8")
    assert "sk-meta-secret-value" not in meta


def test_audit_chain_detects_tampering(tmp_path: Path) -> None:
    audit = AuditLog(tmp_path)
    session_id = audit.start({"interface": "test"})
    audit.record("one", ok=True)
    audit.record("two", ok=False)

    verified = audit.verify_session()
    assert verified.valid is True
    assert verified.event_count == 3

    path = tmp_path / session_id / "audit.jsonl"
    events = path.read_text(encoding="utf-8").splitlines()
    events[1] = events[1].replace('"one"', '"tampered"')
    path.write_text("\n".join(events) + "\n", encoding="utf-8")

    invalid = audit.verify_session(session_id)
    assert invalid.valid is False
    assert "hash mismatch" in (invalid.error or "")
