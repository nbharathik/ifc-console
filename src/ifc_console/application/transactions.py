"""Preview, approval, verified commit, and restore for structured IFC edits."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ifc_console.application.artifacts import ArtifactService
from ifc_console.automation.files import describe_source, sha256_file, source_matches
from ifc_console.core.changes import (
    Approval,
    ApprovalRecord,
    ChangeSet,
    ChangeSetRecord,
    CommitRecord,
    CommitResult,
    IfcScalar,
    PropertyValueChange,
    RestoreRecord,
    RestoreResult,
)
from ifc_console.core.results import ToolError
from ifc_console.core.revisions import RevisionRef
from ifc_console.policy.modes import Mode
from ifc_console.sandbox.client import _child_env, worker_executable
from ifc_console.sandbox.limits import ProcessJail
from ifc_console.sandbox.policy import SandboxPolicy

if TYPE_CHECKING:
    from ifc_console.app import AppCore
    from ifc_console.core.artifacts import ArtifactRef
    from ifc_console.session.model import ModelSession


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TransactionService:
    def __init__(self, core: AppCore, root: Path, artifacts: ArtifactService) -> None:
        self.core = core
        self.root = root
        self.work_dir = root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts = artifacts
        self._commit_lock = asyncio.Lock()

    async def preview_property_value(
        self,
        *,
        global_ids: tuple[str, ...] | list[str],
        pset_name: str,
        property_name: str,
        value: IfcScalar,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        if not pset_name.strip() or not property_name.strip():
            raise ToolError(
                "INVALID_INPUT",
                "property set and property names must not be empty.",
                "Pass exact names such as Pset_WallCommon and FireRating.",
            )
        ids = tuple(dict.fromkeys(item.strip() for item in global_ids if item.strip()))
        if not ids:
            raise ToolError(
                "INVALID_INPUT", "no GlobalIds were supplied.", "Select at least one element."
            )
        async with self.core.active_session() as session:
            session.require_writable()
            revision = self._require_clean_revision(session, expected_revision)
            assert session.path is not None
            source = await asyncio.to_thread(describe_source, session.path)
            self._require_unchanged_session(session, revision, source)
            work = self._new_work("preview")
            try:
                result, worker = await self._run_worker(
                    {
                        "action": "preview",
                        "source": source.model_dump(mode="json"),
                        "global_ids": ids,
                        "pset_name": pset_name.strip(),
                        "property_name": property_name.strip(),
                        "value": value,
                    },
                    work,
                    read_dirs=[session.path.parent],
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)
            self._require_unchanged_session(session, revision, source)
            changes = tuple(PropertyValueChange.model_validate(item) for item in result["changes"])
            change_set = ChangeSet(
                created_at=_now(),
                revision=revision,
                source=source,
                changes=changes,
                warnings=(
                    "Only existing occurrence-level IfcPropertySingleValue values are changed.",
                ),
            )
            artifact = self.artifacts.put_text(
                change_set.model_dump_json(indent=2),
                name=f"{session.path.stem}-property-change.json",
                kind="ifc-changeset",
                media_type="application/vnd.ifc-console.changeset+json",
                producer="preview_property_change",
                revision=revision,
                metadata={
                    "change_count": len(changes),
                    "worker_controls": worker.get("controls", []),
                },
            )
            record = ChangeSetRecord(
                change_set_id=artifact.artifact_id,
                change_set=change_set,
                artifact=artifact,
            )
            self.core.audit.record(
                "changeset_previewed",
                change_set_id=record.change_set_id,
                workspace_id=revision.workspace_id,
                model_id=revision.model_id,
                revision_id=revision.revision_id,
                change_count=len(changes),
                global_ids=[change.global_id for change in changes],
            )
            self.core.events.emit(
                "changeset_previewed",
                change_set_id=record.change_set_id,
                change_count=len(changes),
            )
            return record

    def get_change_set(self, change_set_id: str) -> ChangeSetRecord:
        artifact, document = self._load_document(
            change_set_id,
            kind="ifc-changeset",
            producer="preview_property_change",
            model=ChangeSet,
            missing_code="CHANGESET_NOT_FOUND",
            label="ChangeSet",
        )
        return ChangeSetRecord(
            change_set_id=artifact.artifact_id,
            change_set=document,
            artifact=artifact,
        )

    def approve(
        self,
        change_set_id: str,
        *,
        approved_by: str,
        reason: str = "",
    ) -> ApprovalRecord:
        actor = approved_by.strip()
        explanation = reason.strip()
        if not actor or len(actor) > 200 or len(explanation) > 1000:
            raise ToolError(
                "INVALID_INPUT",
                "the approval identity or reason is empty or too long.",
                "Pass a 1 to 200 character identity and at most 1000 reason characters.",
            )
        change_record = self.get_change_set(change_set_id)
        approval = Approval(
            change_set_id=change_record.change_set_id,
            revision=change_record.change_set.revision,
            approved_by=actor,
            approved_at=_now(),
            reason=explanation,
        )
        artifact = self.artifacts.put_text(
            approval.model_dump_json(indent=2),
            name=f"approval-{change_set_id.split(':', 1)[-1][:16]}.json",
            kind="ifc-change-approval",
            media_type="application/vnd.ifc-console.approval+json",
            producer="approve_change_set",
            revision=approval.revision,
            metadata={"approved_by": actor, "change_set_id": change_set_id},
            references=(change_set_id,),
        )
        record = ApprovalRecord(
            approval_id=artifact.artifact_id,
            approval=approval,
            artifact=artifact,
        )
        self.core.audit.record(
            "changeset_approved",
            change_set_id=change_set_id,
            approval_id=record.approval_id,
            approved_by=actor,
            revision_id=approval.revision.revision_id,
        )
        self.core.events.emit(
            "changeset_approved",
            change_set_id=change_set_id,
            approval_id=record.approval_id,
        )
        return record

    def get_approval(self, approval_id: str) -> ApprovalRecord:
        artifact, document = self._load_document(
            approval_id,
            kind="ifc-change-approval",
            producer="approve_change_set",
            model=Approval,
            missing_code="APPROVAL_NOT_FOUND",
            label="approval",
        )
        return ApprovalRecord(
            approval_id=artifact.artifact_id,
            approval=document,
            artifact=artifact,
        )

    async def commit(self, change_set_id: str, *, approval_id: str) -> CommitRecord:
        self._require_edit_mode("commit")
        if not approval_id:
            raise ToolError(
                "APPROVAL_REQUIRED",
                "commit requires an explicit approval_id.",
                "Review and approve the ChangeSet through the SDK or CLI first.",
            )
        change_record = self.get_change_set(change_set_id)
        approval_record = self.get_approval(approval_id)
        if approval_record.approval.change_set_id != change_record.change_set_id:
            raise ToolError(
                "APPROVAL_MISMATCH",
                "the approval belongs to a different ChangeSet.",
                "Approve this ChangeSet explicitly and pass the returned approval_id.",
            )
        if approval_record.approval.revision != change_record.change_set.revision:
            raise ToolError(
                "APPROVAL_MISMATCH",
                "the approval is bound to a different model revision.",
                "Create a new approval for this ChangeSet.",
            )

        async with self._commit_lock, self.core.active_session() as session:
            session.require_writable()
            revision_before = self._validate_target(session, change_record.change_set)
            assert session.path is not None
            target = session.path.resolve()
            work = self._new_work("commit")
            output = work / "candidate.ifc"
            try:
                result, worker = await self._run_worker(
                    {
                        "action": "apply",
                        "change_set": change_record.change_set.model_dump(mode="json"),
                        "output_path": str(output),
                    },
                    work,
                    read_dirs=[target.parent],
                )
                candidate_sha = sha256_file(output)
                if candidate_sha == change_record.change_set.source.sha256:
                    raise ToolError(
                        "COMMIT_FAILED",
                        "the candidate IFC is byte-identical to the source.",
                        "Discard the ChangeSet and preview the property edit again.",
                    )
                async with self._target_lock(target):
                    return await self._commit_candidate(
                        session=session,
                        target=target,
                        candidate=output,
                        candidate_sha=candidate_sha,
                        change_record=change_record,
                        approval_record=approval_record,
                        revision_before=revision_before,
                        schema_valid=bool(result["schema_valid"]),
                        schema_issue_count=int(result["schema_issue_count"]),
                        worker=worker,
                    )
            finally:
                shutil.rmtree(work, ignore_errors=True)

    def get_commit(self, commit_id: str) -> CommitRecord:
        artifact, document = self._load_document(
            commit_id,
            kind="ifc-commit-receipt",
            producer="commit_change_set",
            model=CommitResult,
            missing_code="COMMIT_NOT_FOUND",
            label="commit receipt",
        )
        return CommitRecord(commit_id=artifact.artifact_id, result=document, artifact=artifact)

    async def restore(self, commit_id: str, *, confirm: bool = False) -> RestoreRecord:
        self._require_edit_mode("restore")
        if not confirm:
            raise ToolError(
                "APPROVAL_REQUIRED",
                "restore requires explicit caller confirmation.",
                "Pass confirm=true only after reviewing the commit receipt.",
            )
        commit_record = self.get_commit(commit_id)
        async with self._commit_lock, self.core.active_session() as session:
            session.require_writable()
            if session.dirty:
                raise ToolError(
                    "UNSAVED_CHANGES",
                    "restore cannot replace a model with unsaved changes.",
                    "Save or discard the in-memory changes first.",
                )
            assert session.path is not None
            target = session.path.resolve()
            if target != Path(commit_record.result.target_path).resolve():
                raise ToolError(
                    "RESTORE_CONFLICT",
                    "the commit receipt belongs to a different model path.",
                    "Open the exact committed model before restoring it.",
                )
            current_sha = await asyncio.to_thread(sha256_file, target)
            if current_sha != commit_record.result.committed_sha256:
                raise ToolError(
                    "RESTORE_CONFLICT",
                    "the target changed after the recorded commit.",
                    "Do not overwrite later work. Review the model and restore manually if needed.",
                )
            backup = self.artifacts.verify(
                commit_record.result.backup_artifact.artifact_id
            )
            if backup.sha256 != commit_record.result.previous_sha256:
                raise ToolError(
                    "ARTIFACT_CORRUPT",
                    "the recorded backup does not match the commit receipt.",
                    "Do not restore this commit; inspect the artifact store.",
                )
            revision_before = self._session_revision(session)
            work = self._new_work("restore")
            candidate = work / "restore.ifc"
            try:
                self.artifacts.export(backup.artifact_id, candidate)
                source = await asyncio.to_thread(describe_source, candidate)
                result, worker = await self._run_worker(
                    {"action": "verify", "source": source.model_dump(mode="json")},
                    work,
                    read_dirs=[],
                )
                async with self._target_lock(target):
                    latest_sha = await asyncio.to_thread(sha256_file, target)
                    if latest_sha != current_sha:
                        raise ToolError(
                            "RESTORE_CONFLICT",
                            "the target changed while restore was being prepared.",
                            "Inspect the current model before retrying restore.",
                        )
                    return await self._restore_candidate(
                        session=session,
                        target=target,
                        backup=candidate,
                        current_sha=current_sha,
                        commit_record=commit_record,
                        revision_before=revision_before,
                        schema_valid=bool(result["schema_valid"]),
                        schema_issue_count=int(result["schema_issue_count"]),
                        worker=worker,
                    )
            finally:
                shutil.rmtree(work, ignore_errors=True)

    async def _commit_candidate(
        self,
        *,
        session: ModelSession,
        target: Path,
        candidate: Path,
        candidate_sha: str,
        change_record: ChangeSetRecord,
        approval_record: ApprovalRecord,
        revision_before: RevisionRef,
        schema_valid: bool,
        schema_issue_count: int,
        worker: dict[str, object],
    ) -> CommitRecord:
        latest = await asyncio.to_thread(describe_source, target)
        expected = change_record.change_set.source
        if latest != expected or not source_matches(expected):
            raise ToolError(
                "REVISION_CONFLICT",
                "the source IFC changed after the ChangeSet was previewed.",
                "Create and approve a new preview against the current model.",
            )
        try:
            backup = self.artifacts.put_file(
                target,
                name=f"{target.stem}-{expected.sha256[:12]}-backup.ifc",
                kind="ifc-verified-backup",
                media_type="application/x-step",
                producer="commit_change_set",
                revision=revision_before,
                metadata={
                    "target_path": str(target),
                    "source_sha256": expected.sha256,
                    "change_set_id": change_record.change_set_id,
                },
                references=(change_record.change_set_id,),
                expected_sha256=expected.sha256,
            )
        except Exception as exc:
            raise ToolError(
                "COMMIT_FAILED",
                f"the verified backup could not be created: {exc}",
                "The source IFC was not replaced. Resolve artifact storage and retry.",
            ) from exc
        replaced = False
        try:
            self._replace_file(target, candidate, expected_sha256=candidate_sha)
            replaced = True
            if await asyncio.to_thread(sha256_file, target) != candidate_sha:
                raise OSError("target checksum does not match the verified candidate")
            await session.reload()
            revision_after = self._session_revision(session)
            result = CommitResult(
                change_set_id=change_record.change_set_id,
                approval_id=approval_record.approval_id,
                target_path=str(target),
                committed_at=_now(),
                revision_before=revision_before,
                revision_after=revision_after,
                previous_sha256=expected.sha256,
                committed_sha256=candidate_sha,
                backup_artifact=backup,
                changed_global_ids=tuple(
                    change.global_id for change in change_record.change_set.changes
                ),
                schema_valid=schema_valid,
                schema_issue_count=schema_issue_count,
                worker=worker,
            )
            artifact = self.artifacts.put_text(
                result.model_dump_json(indent=2),
                name=f"{target.stem}-commit.json",
                kind="ifc-commit-receipt",
                media_type="application/vnd.ifc-console.commit+json",
                producer="commit_change_set",
                revision=revision_after,
                metadata={
                    "change_set_id": change_record.change_set_id,
                    "approval_id": approval_record.approval_id,
                    "target_path": str(target),
                },
                references=(
                    change_record.change_set_id,
                    approval_record.approval_id,
                    backup.artifact_id,
                ),
            )
            record = CommitRecord(commit_id=artifact.artifact_id, result=result, artifact=artifact)
        except Exception as exc:
            rollback_error = ""
            if replaced:
                try:
                    self._replace_file(
                        target,
                        self.artifacts.content_path(backup.artifact_id),
                        expected_sha256=expected.sha256,
                    )
                    await session.reload()
                except Exception as rollback_exc:
                    rollback_error = f" Rollback also failed: {rollback_exc}"
            raise ToolError(
                "COMMIT_FAILED",
                f"the verified IFC could not be committed: {exc}.{rollback_error}".strip(),
                "The original was restored when possible. Inspect the target and backup artifact.",
            ) from exc
        self.core.recents.touch(
            target,
            size_bytes=session.size_bytes,
            schema=session.schema or "?",
            mode=self.core.policy.mode.value,
        )
        self.core.audit.record(
            "changeset_committed",
            commit_id=record.commit_id,
            change_set_id=record.result.change_set_id,
            approval_id=record.result.approval_id,
            target_path=str(target),
            previous_sha256=record.result.previous_sha256,
            committed_sha256=record.result.committed_sha256,
            backup_artifact=backup.artifact_id,
            revision_id=record.result.revision_after.revision_id,
        )
        self.core.events.emit(
            "model_committed",
            commit_id=record.commit_id,
            change_set_id=record.result.change_set_id,
            path=str(target),
        )
        return record

    async def _restore_candidate(
        self,
        *,
        session: ModelSession,
        target: Path,
        backup: Path,
        current_sha: str,
        commit_record: CommitRecord,
        revision_before: RevisionRef,
        schema_valid: bool,
        schema_issue_count: int,
        worker: dict[str, object],
    ) -> RestoreRecord:
        safety = self.artifacts.put_file(
            target,
            name=f"{target.stem}-{current_sha[:12]}-restore-safety.ifc",
            kind="ifc-restore-safety",
            media_type="application/x-step",
            producer="restore_commit",
            revision=revision_before,
            metadata={"target_path": str(target), "commit_id": commit_record.commit_id},
            references=(commit_record.commit_id,),
            expected_sha256=current_sha,
        )
        replaced = False
        try:
            self._replace_file(
                target,
                backup,
                expected_sha256=commit_record.result.previous_sha256,
            )
            replaced = True
            restored_sha = await asyncio.to_thread(sha256_file, target)
            if restored_sha != commit_record.result.previous_sha256:
                raise OSError("restored target checksum does not match the verified backup")
            await session.reload()
            revision_after = self._session_revision(session)
            result = RestoreResult(
                commit_id=commit_record.commit_id,
                target_path=str(target),
                restored_at=_now(),
                revision_before=revision_before,
                revision_after=revision_after,
                replaced_sha256=current_sha,
                restored_sha256=restored_sha,
                safety_artifact=safety,
                schema_valid=schema_valid,
                schema_issue_count=schema_issue_count,
                worker=worker,
            )
            artifact = self.artifacts.put_text(
                result.model_dump_json(indent=2),
                name=f"{target.stem}-restore.json",
                kind="ifc-restore-receipt",
                media_type="application/vnd.ifc-console.restore+json",
                producer="restore_commit",
                revision=revision_after,
                metadata={"commit_id": commit_record.commit_id, "target_path": str(target)},
                references=(commit_record.commit_id, safety.artifact_id),
            )
            record = RestoreRecord(
                restore_id=artifact.artifact_id, result=result, artifact=artifact
            )
        except Exception as exc:
            rollback_error = ""
            if replaced:
                try:
                    self._replace_file(
                        target,
                        self.artifacts.content_path(safety.artifact_id),
                        expected_sha256=current_sha,
                    )
                    await session.reload()
                except Exception as rollback_exc:
                    rollback_error = f" Rollback also failed: {rollback_exc}"
            raise ToolError(
                "COMMIT_FAILED",
                f"the verified backup could not be restored: {exc}.{rollback_error}".strip(),
                "The pre-restore model was restored when possible. Inspect both artifacts.",
            ) from exc
        self.core.audit.record(
            "commit_restored",
            restore_id=record.restore_id,
            commit_id=commit_record.commit_id,
            target_path=str(target),
            replaced_sha256=current_sha,
            restored_sha256=record.result.restored_sha256,
            safety_artifact=safety.artifact_id,
        )
        self.core.events.emit(
            "model_restored",
            restore_id=record.restore_id,
            commit_id=commit_record.commit_id,
            path=str(target),
        )
        return record

    def _validate_target(self, session: ModelSession, change_set: ChangeSet) -> RevisionRef:
        if session.dirty:
            raise ToolError(
                "UNSAVED_CHANGES",
                "commit cannot replace a model with unsaved changes.",
                "Save or discard the in-memory changes, then create a new preview.",
            )
        assert session.path is not None
        if session.path.resolve() != Path(change_set.source.path).resolve():
            raise ToolError(
                "REVISION_CONFLICT",
                "the ChangeSet belongs to a different model path.",
                "Open the previewed model before committing this ChangeSet.",
            )
        current = self._session_revision(session)
        if change_set.revision.workspace_id == self.core.workspace_id and (
            current.model_id != change_set.revision.model_id
            or current.revision_id != change_set.revision.revision_id
        ):
            raise ToolError(
                "REVISION_CONFLICT",
                "the in-memory model revision changed after preview.",
                "Discard this ChangeSet and preview the edit again.",
            )
        if not source_matches(change_set.source):
            raise ToolError(
                "REVISION_CONFLICT",
                "the source IFC changed after preview.",
                "Create and approve a new preview against the current file.",
            )
        return current

    def _require_clean_revision(
        self, session: ModelSession, expected_revision: str | None
    ) -> RevisionRef:
        session.require_loaded()
        if session.dirty:
            raise ToolError(
                "UNSAVED_CHANGES",
                "structured previews require a clean disk-backed model.",
                "Save or discard existing in-memory changes first.",
            )
        if session.path is None or session.model_id is None or session.fingerprint is None:
            raise ToolError(
                "NO_MODEL_LOADED",
                "the active model has no durable revision.",
                "Open a saved IFC model before previewing a change.",
            )
        current = self._session_revision(session)
        if expected_revision is not None and expected_revision != current.revision_id:
            raise ToolError(
                "REVISION_CONFLICT",
                f"expected revision {expected_revision}, current revision is {current.revision_id}.",
                "Refresh workspace context and preview against the current revision.",
            )
        return current

    def _require_unchanged_session(
        self, session: ModelSession, revision: RevisionRef, source: Any
    ) -> None:
        if session.dirty or self._session_revision(session).revision_id != revision.revision_id:
            raise ToolError(
                "REVISION_CONFLICT",
                "the model revision changed while the preview was being prepared.",
                "Preview the edit again against the current revision.",
            )
        if not source_matches(source):
            raise ToolError(
                "SOURCE_CHANGED",
                "the IFC source changed while the preview was being prepared.",
                "Reload the model and preview the edit again.",
            )

    def _session_revision(self, session: ModelSession) -> RevisionRef:
        if session.model_id is None or session.fingerprint is None:
            raise ToolError(
                "NO_MODEL_LOADED", "the model has no revision.", "Open a saved IFC model."
            )
        return RevisionRef(
            workspace_id=self.core.workspace_id,
            model_id=session.model_id,
            revision_id=f"{session.fingerprint}:{session.revision}",
            content_sha256=session.source_sha256,
        )

    def _require_edit_mode(self, action: str) -> None:
        if self.core.policy.mode is Mode.ASK:
            raise ToolError(
                "ASK_MODE_BLOCKED",
                f"{action} is disabled in ask mode.",
                "The caller must explicitly enter edit mode before changing model bytes.",
            )

    def _load_document(
        self,
        artifact_id: str,
        *,
        kind: str,
        producer: str,
        model: type[BaseModel],
        missing_code: str,
        label: str,
    ) -> tuple[ArtifactRef, Any]:
        try:
            artifact = self.artifacts.get(artifact_id)
            if artifact.kind != kind or artifact.producer != producer:
                raise ValueError("artifact type does not match")
            document = model.model_validate_json(self.artifacts.read_text(artifact_id))
        except (ToolError, ValidationError, ValueError, UnicodeError) as exc:
            raise ToolError(
                missing_code,
                f"{label} {artifact_id!r} is unavailable or invalid.",
                f"Use an ID returned by the {producer} workflow.",
            ) from exc
        return artifact, document

    async def _run_worker(
        self, payload: dict[str, Any], work: Path, *, read_dirs: list[Path]
    ) -> tuple[dict[str, Any], dict[str, object]]:
        policy = SandboxPolicy.build(
            read_dirs=read_dirs,
            scratch_dir=work,
            deny_dirs=[self.core.store.home],
            memory_mb=self.core.settings.sandbox.memory_mb,
        )
        input_path = work / "input.json"
        self._write_new(
            input_path,
            json.dumps({**payload, "policy": policy.to_dict()}, ensure_ascii=False).encode("utf-8"),
        )
        process = await asyncio.create_subprocess_exec(
            *self._worker_command(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work,
            env=_child_env(work),
        )
        jail = ProcessJail(self.core.settings.sandbox.memory_mb)
        jail.attach(process.pid)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=float(self.core.settings.automation.transaction_timeout_s),
                )
            except asyncio.TimeoutError as exc:
                jail.kill()
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
                raise ToolError(
                    "COMMIT_FAILED",
                    "the transaction worker exceeded its configured timeout.",
                    "Raise automation.transaction_timeout_s or narrow the edit.",
                ) from exc
            response = self._parse_worker_response(stdout)
            if process.returncode != 0 or not response.get("ok"):
                message = str(
                    response.get("message")
                    or stderr.decode("utf-8", errors="replace")[-1000:]
                    or f"worker exited with code {process.returncode}"
                )
                raise ToolError(
                    str(response.get("code") or "COMMIT_FAILED"),
                    message,
                    str(response.get("hint") or "The source model was not replaced."),
                )
            controls = list(jail.controls)
            controls.extend(str(item) for item in response.get("controls") or ())
            worker: dict[str, object] = {
                "environment": "minimal",
                "network": "blocked",
                "controls": list(dict.fromkeys(controls)),
            }
            return response, worker
        finally:
            jail.close()

    @staticmethod
    def _parse_worker_response(stdout: bytes) -> dict[str, Any]:
        for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _worker_command(input_path: Path) -> tuple[str, ...]:
        return (
            worker_executable(),
            "-s",
            "-B",
            "-m",
            "ifc_console.automation.transaction_worker",
            str(input_path),
        )

    def _new_work(self, prefix: str) -> Path:
        work = self.work_dir / f"{prefix}-{secrets.token_hex(8)}"
        work.mkdir(parents=True, exist_ok=False)
        return work

    @staticmethod
    def _write_new(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    def _replace_file(self, target: Path, source: Path, *, expected_sha256: str) -> None:
        temp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            digest = hashlib.sha256()
            with source.open("rb") as reader, temp.open("xb") as writer:
                for chunk in iter(lambda: reader.read(1 << 20), b""):
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            if digest.hexdigest() != expected_sha256:
                raise OSError("staged target checksum mismatch")
            self._replace_target(temp, target)
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()

    @staticmethod
    def _replace_target(source: Path, target: Path) -> None:
        os.replace(source, target)

    @asynccontextmanager
    async def _target_lock(self, target: Path):
        lock = target.with_name(f".{target.name}.ifc-console.lock")
        token = secrets.token_hex(16)
        deadline = time.monotonic() + float(
            self.core.settings.automation.transaction_lock_timeout_s
        )
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"pid": os.getpid(), "token": token}, handle)
                break
            except (FileExistsError, PermissionError):
                stale = False
                try:
                    owner = json.loads(lock.read_text(encoding="utf-8"))
                    stale = not self._pid_exists(int(owner.get("pid", 0)))
                except (OSError, ValueError, json.JSONDecodeError):
                    stale = False
                if stale:
                    stale_path = lock.with_name(f"{lock.name}.{secrets.token_hex(4)}.stale")
                    try:
                        os.replace(lock, stale_path)
                        stale_path.unlink()
                        continue
                    except OSError:
                        pass
                if time.monotonic() >= deadline:
                    raise ToolError(
                        "MODEL_BUSY",
                        f"timed out waiting for the transaction lock on {target.name}.",
                        "Wait for the other commit or restore to finish, then retry.",
                    ) from None
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            try:
                owner = json.loads(lock.read_text(encoding="utf-8"))
                if owner.get("token") == token:
                    lock.unlink()
            except (OSError, json.JSONDecodeError):
                pass

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        if sys.platform != "win32":
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return False
            except PermissionError:
                return True
            return True
        try:
            import ctypes

            query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(query_limited_information, False, pid)
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return False
