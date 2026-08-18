"""Reference-aware retention for local content-addressed artifacts."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from ifc_console.application.artifacts import ArtifactService
from ifc_console.core.artifacts import ArtifactGCPlan, ArtifactGCResult, ArtifactRef
from ifc_console.core.results import ToolError

_ARTIFACT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_PROTECTED_KINDS = frozenset(
    {
        "ifc-changeset",
        "ifc-change-approval",
        "ifc-verified-backup",
        "ifc-commit-receipt",
        "ifc-restore-safety",
        "ifc-restore-receipt",
    }
)


class ArtifactRetentionService:
    """Plan and explicitly apply deletion of old, unreachable artifacts."""

    def __init__(
        self,
        artifacts: ArtifactService,
        jobs_root: Path,
        *,
        batches_root: Path | None = None,
        workflows_root: Path | None = None,
        default_retention_days: int = 30,
    ) -> None:
        self.artifacts = artifacts
        self.jobs_root = jobs_root
        self.batches_root = batches_root
        self.workflows_root = workflows_root
        self.default_retention_days = max(1, default_retention_days)
        self.pins_path = artifacts.root / "pins.json"
        self._lock = RLock()

    def plan(self, *, older_than_days: int | None = None) -> ArtifactGCPlan:
        days = self.default_retention_days if older_than_days is None else older_than_days
        if days < 1:
            raise ToolError(
                "INVALID_INPUT",
                "artifact retention must be at least one day.",
                "Pass older_than_days=1 or greater.",
            )
        generated_at = datetime.now(timezone.utc)
        cutoff = generated_at - timedelta(days=days)
        with self._lock, self.artifacts.locked():
            return self._build_plan(generated_at=generated_at, cutoff=cutoff, days=days)

    def collect(self, plan: ArtifactGCPlan, *, confirm: bool = False) -> ArtifactGCResult:
        if not confirm:
            raise ToolError(
                "APPROVAL_REQUIRED",
                "artifact garbage collection requires explicit confirmation.",
                "Review the dry-run plan, then pass confirm=true with that exact plan.",
            )
        with self._lock, self.artifacts.locked():
            current = self._build_plan(
                generated_at=datetime.now(timezone.utc),
                cutoff=plan.cutoff,
                days=plan.older_than_days,
            )
            if current.candidate_ids != plan.candidate_ids:
                raise ToolError(
                    "ARTIFACT_GC_CONFLICT",
                    "artifact references changed after the garbage-collection plan was created.",
                    "Generate and review a new dry-run plan before retrying.",
                )
            refs = {ref.artifact_id: ref for ref in self._all_refs()}
            deleted: list[str] = []
            deleted_bytes = 0
            for artifact_id in plan.candidate_ids:
                ref = refs.get(artifact_id)
                if ref is None:
                    raise ToolError(
                        "ARTIFACT_GC_CONFLICT",
                        f"artifact {artifact_id!r} disappeared before collection.",
                        "Generate and review a new dry-run plan.",
                    )
                self.artifacts._delete_unlocked(ref)
                deleted.append(artifact_id)
                deleted_bytes += ref.size_bytes
            return ArtifactGCResult(
                completed_at=datetime.now(timezone.utc),
                deleted_count=len(deleted),
                deleted_bytes=deleted_bytes,
                deleted_ids=tuple(deleted),
                plan=plan,
            )

    def pin(self, artifact_id: str) -> ArtifactRef:
        with self._lock, self.artifacts.locked():
            ref = self.artifacts.get(artifact_id)
            pins = self._load_pins()
            pins.add(ref.artifact_id)
            self._save_pins(pins)
        return ref

    def unpin(self, artifact_id: str) -> bool:
        self._validate_id(artifact_id)
        with self._lock, self.artifacts.locked():
            pins = self._load_pins()
            removed = artifact_id in pins
            pins.discard(artifact_id)
            self._save_pins(pins)
        return removed

    def pins(self) -> tuple[str, ...]:
        with self._lock, self.artifacts.locked():
            return tuple(sorted(self._load_pins()))

    def _build_plan(
        self, *, generated_at: datetime, cutoff: datetime, days: int
    ) -> ArtifactGCPlan:
        refs = self._all_refs()
        by_id = {ref.artifact_id: ref for ref in refs}
        warnings: list[str] = []
        metadata_count = sum(1 for _ in self.artifacts.metadata_dir.glob("*.json"))
        if metadata_count != len(refs):
            warnings.append(
                f"{metadata_count - len(refs)} unreadable artifact metadata file(s) were retained."
            )

        roots = {ref.artifact_id for ref in refs if ref.created_at >= cutoff}
        roots.update(ref.artifact_id for ref in refs if ref.kind in _PROTECTED_KINDS)
        roots.update(self._load_pins())
        roots.update(self._record_artifact_ids())
        unknown_roots = roots.difference(by_id)
        if unknown_roots:
            warnings.append(f"{len(unknown_roots)} retained reference(s) have no artifact metadata.")

        retained: set[str] = set()
        pending = [artifact_id for artifact_id in roots if artifact_id in by_id]
        while pending:
            artifact_id = pending.pop()
            if artifact_id in retained:
                continue
            retained.add(artifact_id)
            for referenced in by_id[artifact_id].references:
                if referenced in by_id and referenced not in retained:
                    pending.append(referenced)
                elif referenced not in by_id:
                    warnings.append(
                        f"artifact {artifact_id} references missing artifact {referenced}."
                    )

        candidates = sorted(
            (ref for ref in refs if ref.artifact_id not in retained),
            key=lambda ref: (ref.created_at, ref.artifact_id),
        )
        return ArtifactGCPlan(
            generated_at=generated_at,
            cutoff=cutoff,
            older_than_days=days,
            scanned_count=len(refs),
            retained_count=len(retained),
            root_count=len(roots),
            candidate_count=len(candidates),
            candidate_bytes=sum(ref.size_bytes for ref in candidates),
            candidate_ids=tuple(ref.artifact_id for ref in candidates),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _all_refs(self) -> list[ArtifactRef]:
        return self.artifacts.list(limit=2**31 - 1)

    def _record_artifact_ids(self) -> set[str]:
        result: set[str] = set()
        roots = [(self.jobs_root, "job-*.json")]
        if self.batches_root is not None:
            roots.append((self.batches_root, "batch-*.json"))
        if self.workflows_root is not None:
            roots.append((self.workflows_root, "workflow-*.json"))
        for root, pattern in roots:
            for path in (root / "records").glob(pattern):
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("record root is not an object")
                    self._find_artifact_ids(payload, result)
                except (OSError, UnicodeError, ValueError, RecursionError) as exc:
                    raise ToolError(
                        "ARTIFACT_STORE_CORRUPT",
                        f"durable automation record {path.name!r} is unreadable: {exc}",
                        "Repair or remove the corrupt record before collecting artifacts.",
                    ) from exc
        return result

    @classmethod
    def _find_artifact_ids(cls, value: Any, result: set[str]) -> None:
        if isinstance(value, dict):
            artifact_id = value.get("artifact_id")
            if isinstance(artifact_id, str) and _ARTIFACT_ID.fullmatch(artifact_id):
                result.add(artifact_id)
            for nested in value.values():
                cls._find_artifact_ids(nested, result)
        elif isinstance(value, list):
            for nested in value:
                cls._find_artifact_ids(nested, result)

    def _load_pins(self) -> set[str]:
        if not self.pins_path.exists():
            return set()
        try:
            payload = json.loads(self.pins_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolError(
                "ARTIFACT_STORE_CORRUPT",
                f"artifact pins are unreadable: {exc}",
                "Repair or remove the pins file before changing artifact retention.",
            ) from exc
        if not isinstance(payload, list) or any(
            not isinstance(item, str) or _ARTIFACT_ID.fullmatch(item) is None for item in payload
        ):
            raise ToolError(
                "ARTIFACT_STORE_CORRUPT",
                "artifact pins do not contain a valid artifact ID list.",
                "Repair or remove the pins file before changing artifact retention.",
            )
        return set(payload)

    def _save_pins(self, pins: set[str]) -> None:
        payload = json.dumps(sorted(pins), indent=2).encode("utf-8")
        temp = self.pins_path.with_name(f".{self.pins_path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.pins_path)
        finally:
            if temp.exists():
                temp.unlink()

    @staticmethod
    def _validate_id(artifact_id: str) -> None:
        if _ARTIFACT_ID.fullmatch(artifact_id) is None:
            raise ToolError(
                "ARTIFACT_NOT_FOUND",
                f"invalid artifact ID {artifact_id!r}.",
                "Artifact IDs have the form sha256:<64 lowercase hex characters>.",
            )
