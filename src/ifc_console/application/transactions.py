"""Preview, approval, verified commit, and restore for structured IFC edits."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.locks import async_exclusive_file_lock
from ifc_console.application.transaction_journals import TransactionJournalStore
from ifc_console.automation.files import describe_source, sha256_file, source_matches
from ifc_console.core.capabilities import Capability
from ifc_console.core.changes import (
    Approval,
    ApprovalRecord,
    ChangeSet,
    ChangeSetRecord,
    CommitRecord,
    CommitResult,
    IfcScalar,
    PropertyPreview,
    RestoreRecord,
    RestoreResult,
)
from ifc_console.core.context import current_operation_context
from ifc_console.core.results import ToolError
from ifc_console.core.revisions import RevisionRef
from ifc_console.core.transaction_journal import (
    TransactionJournal,
    TransactionKind,
    TransactionPhase,
)
from ifc_console.policy.modes import Mode
from ifc_console.sandbox.client import _child_env, worker_command
from ifc_console.sandbox.limits import ProcessJail, isolated_process_kwargs
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
        self.journals = TransactionJournalStore(
            root,
            artifacts,
            lock_timeout_s=float(core.settings.automation.transaction_lock_timeout_s),
        )
        self.recovered = self.journals.recover_incomplete()
        self._commit_lock = asyncio.Lock()

    async def preview_property_value(
        self,
        *,
        global_ids: tuple[str, ...] | list[str],
        pset_name: str,
        property_name: str,
        value: IfcScalar,
        create_missing: bool = False,
        nominal_type: str | None = None,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "preview_property_change", authority="caller", client=self.core.transport
            ):
                return await self.preview_property_value(
                    global_ids=global_ids,
                    pset_name=pset_name,
                    property_name=property_name,
                    value=value,
                    create_missing=create_missing,
                    nominal_type=nominal_type,
                    expected_revision=expected_revision,
                )
        return await self.preview_property_values(
            global_ids=global_ids,
            properties=(
                PropertyPreview.model_construct(
                    pset_name=pset_name,
                    property_name=property_name,
                    value=value,
                    create_missing=create_missing,
                    nominal_type=nominal_type,
                ),
            ),
            expected_revision=expected_revision,
        )

    async def preview_property_values(
        self,
        *,
        global_ids: tuple[str, ...] | list[str],
        properties: tuple[PropertyPreview, ...] | list[PropertyPreview],
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "preview_property_changes", authority="caller", client=self.core.transport
            ):
                return await self.preview_property_values(
                    global_ids=global_ids,
                    properties=properties,
                    expected_revision=expected_revision,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.MODEL_PREVIEW],
            authority=context.authority,
            action="preview property change",
        )
        if not properties or len(properties) > 16:
            raise ToolError(
                "INVALID_INPUT",
                "an atomic property preview must contain between 1 and 16 properties.",
                "Split larger edits into smaller reviewable ChangeSets.",
            )
        normalized: list[PropertyPreview] = []
        seen: set[tuple[str, str]] = set()
        for requested in properties:
            pset_name = requested.pset_name.strip()
            property_name = requested.property_name.strip()
            if (
                not pset_name
                or not property_name
                or len(pset_name) > 255
                or len(property_name) > 255
            ):
                raise ToolError(
                    "INVALID_INPUT",
                    "property set and property names must contain 1 to 255 characters.",
                    "Pass exact names such as Pset_WallCommon and FireRating.",
                )
            clean_nominal_type = (
                requested.nominal_type.strip() if requested.nominal_type is not None else None
            )
            if clean_nominal_type == "" or (
                clean_nominal_type is not None and len(clean_nominal_type) > 255
            ):
                raise ToolError(
                    "INVALID_INPUT",
                    "nominal_type must be a non-empty IFC type name of at most 255 characters.",
                    "Use a schema value type such as IfcLabel or IfcLengthMeasure.",
                )
            key = (pset_name, property_name)
            if key in seen:
                raise ToolError(
                    "INVALID_INPUT",
                    f"the atomic preview repeats {pset_name}.{property_name}.",
                    "Include each property only once.",
                )
            seen.add(key)
            normalized.append(
                requested.model_copy(
                    update={
                        "pset_name": pset_name,
                        "property_name": property_name,
                        "nominal_type": clean_nominal_type,
                    }
                )
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
                        "properties": [item.model_dump(mode="json") for item in normalized],
                    },
                    work,
                    read_dirs=[session.path.parent],
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)
            self._require_unchanged_session(session, revision, source)
            creates = sum(item.get("kind") == "property_create" for item in result["changes"])
            change_set = ChangeSet(
                created_at=_now(),
                revision=revision,
                source=source,
                changes=result["changes"],
                warnings=(
                    (
                        f"Creates {creates} occurrence-level IfcPropertySingleValue "
                        "properties; inherited type properties are not edited."
                        if creates
                        else "Only existing occurrence-level IfcPropertySingleValue values are changed."
                    ),
                ),
                context=context,
            )
            artifact = self.artifacts.put_text(
                change_set.model_dump_json(indent=2),
                name=f"{session.path.stem}-property-change.json",
                kind="ifc-changeset",
                media_type="application/vnd.ifc-console.changeset+json",
                producer="preview_property_change",
                revision=revision,
                metadata={
                    "change_count": len(change_set.changes),
                    "create_count": creates,
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
                change_count=len(change_set.changes),
                global_ids=list(dict.fromkeys(change.global_id for change in change_set.changes)),
            )
            self.core.events.emit(
                "changeset_previewed",
                change_set_id=record.change_set_id,
                change_count=len(change_set.changes),
            )
            return record

    async def preview_classification_assignment(
        self,
        *,
        global_ids: tuple[str, ...] | list[str],
        classification_name: str,
        identification: str,
        reference_name: str,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "preview_classification_assignment",
                authority="caller",
                client=self.core.transport,
            ):
                return await self.preview_classification_assignment(
                    global_ids=global_ids,
                    classification_name=classification_name,
                    identification=identification,
                    reference_name=reference_name,
                    expected_revision=expected_revision,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.MODEL_PREVIEW],
            authority=context.authority,
            action="preview classification assignment",
        )
        values = {
            "classification_name": classification_name.strip(),
            "identification": identification.strip(),
            "reference_name": reference_name.strip(),
        }
        if any(not value or len(value) > 255 for value in values.values()):
            raise ToolError(
                "INVALID_INPUT",
                "classification system, identification, and reference name must each "
                "contain 1 to 255 characters.",
                "Pass an exact system and reference, such as Uniclass 2015 and Ss_25_10.",
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
            work = self._new_work("classification-preview")
            try:
                result, worker = await self._run_worker(
                    {
                        "action": "preview_classification",
                        "source": source.model_dump(mode="json"),
                        "global_ids": ids,
                        **values,
                    },
                    work,
                    read_dirs=[session.path.parent],
                )
            finally:
                shutil.rmtree(work, ignore_errors=True)
            self._require_unchanged_session(session, revision, source)
            change_set = ChangeSet(
                operation="classification.assign",
                created_at=_now(),
                revision=revision,
                source=source,
                changes=result["changes"],
                warnings=(
                    "Creates direct occurrence assignments; inherited type classifications "
                    "are not changed.",
                ),
                context=context,
            )
            artifact = self.artifacts.put_text(
                change_set.model_dump_json(indent=2),
                name=f"{session.path.stem}-classification-assignment.json",
                kind="ifc-changeset",
                media_type="application/vnd.ifc-console.changeset+json",
                producer="preview_classification_assignment",
                revision=revision,
                metadata={
                    "change_count": len(change_set.changes),
                    "classification_name": values["classification_name"],
                    "identification": values["identification"],
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
                operation=change_set.operation,
                change_count=len(change_set.changes),
                global_ids=[change.global_id for change in change_set.changes],
            )
            self.core.events.emit(
                "changeset_previewed",
                change_set_id=record.change_set_id,
                change_count=len(change_set.changes),
            )
            return record

    def get_change_set(self, change_set_id: str) -> ChangeSetRecord:
        artifact, document = self._load_document(
            change_set_id,
            kind="ifc-changeset",
            producer=("preview_property_change", "preview_classification_assignment"),
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
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "approve_change_set",
                authority="caller",
                client=self.core.transport,
                actor=approved_by,
            ):
                return self.approve(
                    change_set_id,
                    approved_by=approved_by,
                    reason=reason,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.MODEL_APPROVE],
            authority=context.authority,
            action="approve ChangeSet",
        )
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
            context=context,
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

    async def commit(
        self,
        change_set_id: str,
        *,
        approval_id: str,
        _job_id: str | None = None,
        _progress: Callable[[TransactionJournal], None] | None = None,
        _cancel_check: Callable[[], bool] | None = None,
    ) -> CommitRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "commit_change_set", authority="caller", client=self.core.transport
            ):
                return await self.commit(
                    change_set_id,
                    approval_id=approval_id,
                    _job_id=_job_id,
                    _progress=_progress,
                    _cancel_check=_cancel_check,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.MODEL_COMMIT],
            authority=context.authority,
            action="commit ChangeSet",
        )
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
                regression_count = int(result.get("schema_regression_count", 0))
                if regression_count:
                    raise ToolError(
                        "COMMIT_FAILED",
                        f"the candidate introduced {regression_count} new schema validation "
                        "issue(s).",
                        "The source was not replaced. Review the ChangeSet or repair the model.",
                    )
                candidate_sha = sha256_file(output)
                if candidate_sha == change_record.change_set.source.sha256:
                    raise ToolError(
                        "COMMIT_FAILED",
                        "the candidate IFC is byte-identical to the source.",
                        "Discard the ChangeSet and preview the property edit again.",
                    )
                async with self._target_lock(target):
                    self.journals.ensure_target_ready(target)
                    journal = self.journals.create(
                        kind=TransactionKind.COMMIT,
                        target=target,
                        expected_before_sha256=change_record.change_set.source.sha256,
                        desired_after_sha256=candidate_sha,
                        candidate=output,
                        job_id=_job_id,
                        change_set_id=change_record.change_set_id,
                        approval_id=approval_record.approval_id,
                        context=context,
                    )
                    journal = self._journal_phase(
                        journal,
                        TransactionPhase.CANDIDATE_VERIFIED,
                        _progress,
                    )
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
                        journal=journal,
                        progress=_progress,
                        cancel_check=_cancel_check,
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

    async def restore(
        self,
        commit_id: str,
        *,
        confirm: bool = False,
        _job_id: str | None = None,
        _progress: Callable[[TransactionJournal], None] | None = None,
        _cancel_check: Callable[[], bool] | None = None,
    ) -> RestoreRecord:
        if current_operation_context() is None:
            with self.core.operation_service.invocation(
                "restore_commit", authority="caller", client=self.core.transport
            ):
                return await self.restore(
                    commit_id,
                    confirm=confirm,
                    _job_id=_job_id,
                    _progress=_progress,
                    _cancel_check=_cancel_check,
                )
        context = current_operation_context()
        assert context is not None
        self.core.policy.require(
            [Capability.MODEL_RESTORE],
            authority=context.authority,
            action="restore commit",
        )
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
            backup = self.artifacts.verify(commit_record.result.backup_artifact.artifact_id)
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
                    self.journals.ensure_target_ready(target)
                    journal = self.journals.create(
                        kind=TransactionKind.RESTORE,
                        target=target,
                        expected_before_sha256=current_sha,
                        desired_after_sha256=commit_record.result.previous_sha256,
                        candidate=candidate,
                        job_id=_job_id,
                        source_commit_id=commit_record.commit_id,
                        context=context,
                    )
                    journal = self._journal_phase(
                        journal,
                        TransactionPhase.CANDIDATE_VERIFIED,
                        _progress,
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
                        journal=journal,
                        progress=_progress,
                        cancel_check=_cancel_check,
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
        journal: TransactionJournal,
        progress: Callable[[TransactionJournal], None] | None,
        cancel_check: Callable[[], bool] | None,
    ) -> CommitRecord:
        latest = await asyncio.to_thread(describe_source, target)
        expected = change_record.change_set.source
        if latest != expected or not source_matches(expected):
            self._journal_phase(
                journal,
                TransactionPhase.ABORTED,
                progress,
                error="source changed after the candidate was verified",
            )
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
            self._journal_phase(
                journal,
                TransactionPhase.ABORTED,
                progress,
                error=f"backup creation failed: {type(exc).__name__}: {exc}",
            )
            raise ToolError(
                "COMMIT_FAILED",
                f"the verified backup could not be created: {exc}",
                "The source IFC was not replaced. Resolve artifact storage and retry.",
            ) from exc
        journal = self._journal_phase(
            journal,
            TransactionPhase.BACKUP_VERIFIED,
            progress,
            rollback_artifact_id=backup.artifact_id,
        )
        if cancel_check is not None and cancel_check():
            self._journal_phase(
                journal,
                TransactionPhase.ABORTED,
                progress,
                error="commit cancelled before commit point",
            )
            raise asyncio.CancelledError
        try:
            journal = self._journal_phase(
                journal,
                TransactionPhase.COMMIT_POINT,
                progress,
            )
            self._replace_file(target, candidate, expected_sha256=candidate_sha)
            if await asyncio.to_thread(sha256_file, target) != candidate_sha:
                raise OSError("target checksum does not match the verified candidate")
            journal = self._journal_phase(
                journal,
                TransactionPhase.TARGET_VERIFIED,
                progress,
            )
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
                context=current_operation_context(),
            )
            receipt_text = result.model_dump_json(indent=2)
            expected_receipt_id = (
                f"sha256:{hashlib.sha256(receipt_text.encode('utf-8')).hexdigest()}"
            )
            journal = self._journal_phase(
                journal,
                TransactionPhase.RECEIPT_PREPARED,
                progress,
                expected_receipt_id=expected_receipt_id,
                result_document=result.model_dump(mode="json"),
            )
            artifact = self.artifacts.put_text(
                receipt_text,
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
            if artifact.artifact_id != expected_receipt_id:
                raise OSError("commit receipt identity does not match its journal")
            record = CommitRecord(commit_id=artifact.artifact_id, result=result, artifact=artifact)
            try:
                journal = self._journal_phase(
                    journal,
                    TransactionPhase.RECEIPT_PERSISTED,
                    progress,
                    receipt_artifact_id=artifact.artifact_id,
                )
            except Exception as journal_exc:
                self.core.audit.record(
                    "transaction_journal_degraded",
                    transaction_id=journal.transaction_id,
                    phase=journal.phase.value,
                    receipt_artifact_id=artifact.artifact_id,
                    error_type=type(journal_exc).__name__,
                )
        except asyncio.CancelledError:
            rollback_error = await self._rollback_after_failure(
                journal=journal,
                target=target,
                rollback_artifact_id=backup.artifact_id,
                expected_sha256=expected.sha256,
                session=session,
                progress=progress,
                error="commit cancelled during finalization",
            )
            if rollback_error:
                raise ToolError(
                    "TRANSACTION_RECOVERY_REQUIRED",
                    f"commit cancellation rollback failed: {rollback_error}",
                    "Do not modify the target until the journal and backup are inspected.",
                ) from None
            raise
        except Exception as exc:
            rollback_error = await self._rollback_after_failure(
                journal=journal,
                target=target,
                rollback_artifact_id=backup.artifact_id,
                expected_sha256=expected.sha256,
                session=session,
                progress=progress,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise ToolError(
                "COMMIT_FAILED",
                f"the verified IFC could not be committed: {exc}. {rollback_error}".strip(),
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
        journal: TransactionJournal,
        progress: Callable[[TransactionJournal], None] | None,
        cancel_check: Callable[[], bool] | None,
    ) -> RestoreRecord:
        try:
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
        except Exception as exc:
            self._journal_phase(
                journal,
                TransactionPhase.ABORTED,
                progress,
                error=f"safety backup creation failed: {type(exc).__name__}: {exc}",
            )
            raise ToolError(
                "COMMIT_FAILED",
                f"the restore safety backup could not be created: {exc}",
                "The target was not replaced. Resolve artifact storage and retry.",
            ) from exc
        journal = self._journal_phase(
            journal,
            TransactionPhase.BACKUP_VERIFIED,
            progress,
            rollback_artifact_id=safety.artifact_id,
        )
        if cancel_check is not None and cancel_check():
            self._journal_phase(
                journal,
                TransactionPhase.ABORTED,
                progress,
                error="restore cancelled before commit point",
            )
            raise asyncio.CancelledError
        try:
            journal = self._journal_phase(
                journal,
                TransactionPhase.COMMIT_POINT,
                progress,
            )
            self._replace_file(
                target,
                backup,
                expected_sha256=commit_record.result.previous_sha256,
            )
            restored_sha = await asyncio.to_thread(sha256_file, target)
            if restored_sha != commit_record.result.previous_sha256:
                raise OSError("restored target checksum does not match the verified backup")
            journal = self._journal_phase(
                journal,
                TransactionPhase.TARGET_VERIFIED,
                progress,
            )
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
                context=current_operation_context(),
            )
            receipt_text = result.model_dump_json(indent=2)
            expected_receipt_id = (
                f"sha256:{hashlib.sha256(receipt_text.encode('utf-8')).hexdigest()}"
            )
            journal = self._journal_phase(
                journal,
                TransactionPhase.RECEIPT_PREPARED,
                progress,
                expected_receipt_id=expected_receipt_id,
                result_document=result.model_dump(mode="json"),
            )
            artifact = self.artifacts.put_text(
                receipt_text,
                name=f"{target.stem}-restore.json",
                kind="ifc-restore-receipt",
                media_type="application/vnd.ifc-console.restore+json",
                producer="restore_commit",
                revision=revision_after,
                metadata={"commit_id": commit_record.commit_id, "target_path": str(target)},
                references=(commit_record.commit_id, safety.artifact_id),
            )
            if artifact.artifact_id != expected_receipt_id:
                raise OSError("restore receipt identity does not match its journal")
            record = RestoreRecord(
                restore_id=artifact.artifact_id, result=result, artifact=artifact
            )
            try:
                journal = self._journal_phase(
                    journal,
                    TransactionPhase.RECEIPT_PERSISTED,
                    progress,
                    receipt_artifact_id=artifact.artifact_id,
                )
            except Exception as journal_exc:
                self.core.audit.record(
                    "transaction_journal_degraded",
                    transaction_id=journal.transaction_id,
                    phase=journal.phase.value,
                    receipt_artifact_id=artifact.artifact_id,
                    error_type=type(journal_exc).__name__,
                )
        except asyncio.CancelledError:
            rollback_error = await self._rollback_after_failure(
                journal=journal,
                target=target,
                rollback_artifact_id=safety.artifact_id,
                expected_sha256=current_sha,
                session=session,
                progress=progress,
                error="restore cancelled during finalization",
            )
            if rollback_error:
                raise ToolError(
                    "TRANSACTION_RECOVERY_REQUIRED",
                    f"restore cancellation rollback failed: {rollback_error}",
                    "Do not modify the target until the journal and backup are inspected.",
                ) from None
            raise
        except Exception as exc:
            rollback_error = await self._rollback_after_failure(
                journal=journal,
                target=target,
                rollback_artifact_id=safety.artifact_id,
                expected_sha256=current_sha,
                session=session,
                progress=progress,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise ToolError(
                "COMMIT_FAILED",
                f"the verified backup could not be restored: {exc}. {rollback_error}".strip(),
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

    def get_restore(self, restore_id: str) -> RestoreRecord:
        artifact, document = self._load_document(
            restore_id,
            kind="ifc-restore-receipt",
            producer="restore_commit",
            model=RestoreResult,
            missing_code="RESTORE_NOT_FOUND",
            label="restore receipt",
        )
        return RestoreRecord(
            restore_id=artifact.artifact_id,
            result=document,
            artifact=artifact,
        )

    def _journal_phase(
        self,
        journal: TransactionJournal,
        phase: TransactionPhase,
        progress: Callable[[TransactionJournal], None] | None,
        **updates: Any,
    ) -> TransactionJournal:
        updated = self.journals.update(journal.transaction_id, phase, **updates)
        if progress is not None:
            with contextlib.suppress(Exception):
                progress(updated)
        return updated

    async def _rollback_after_failure(
        self,
        *,
        journal: TransactionJournal,
        target: Path,
        rollback_artifact_id: str,
        expected_sha256: str,
        session: ModelSession,
        progress: Callable[[TransactionJournal], None] | None,
        error: str,
    ) -> str:
        try:
            current_sha = await asyncio.to_thread(sha256_file, target)
            if current_sha == expected_sha256:
                self._journal_phase(
                    journal,
                    TransactionPhase.ROLLED_BACK,
                    progress,
                    error=error,
                )
                return ""
            if current_sha != journal.desired_after_sha256:
                raise OSError("target checksum matches neither the expected source nor candidate")
            journal = self._journal_phase(
                journal,
                TransactionPhase.ROLLBACK_STARTED,
                progress,
                error=error,
            )
            self._replace_file(
                target,
                self.artifacts.content_path(rollback_artifact_id),
                expected_sha256=expected_sha256,
            )
            if await asyncio.to_thread(sha256_file, target) != expected_sha256:
                raise OSError("rollback target checksum mismatch")
            self._journal_phase(
                journal,
                TransactionPhase.ROLLED_BACK,
                progress,
                error=error,
            )
            try:
                await session.reload()
            except Exception as reload_exc:
                self.core.audit.record(
                    "transaction_reload_degraded",
                    transaction_id=journal.transaction_id,
                    error_type=type(reload_exc).__name__,
                )
            return ""
        except Exception as rollback_exc:
            with contextlib.suppress(Exception):
                self._journal_phase(
                    journal,
                    TransactionPhase.RECOVERY_FAILED,
                    progress,
                    error=error,
                    rollback_error=f"{type(rollback_exc).__name__}: {rollback_exc}",
                )
            return f"Rollback also failed: {rollback_exc}"

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
        producer: str | tuple[str, ...],
        model: type[BaseModel],
        missing_code: str,
        label: str,
    ) -> tuple[ArtifactRef, Any]:
        producers = (producer,) if isinstance(producer, str) else producer
        try:
            artifact = self.artifacts.get(artifact_id)
            if artifact.kind != kind or artifact.producer not in producers:
                raise ValueError("artifact type does not match")
            document = model.model_validate_json(self.artifacts.read_text(artifact_id))
        except (ToolError, ValidationError, ValueError, UnicodeError) as exc:
            raise ToolError(
                missing_code,
                f"{label} {artifact_id!r} is unavailable or invalid.",
                f"Use an ID returned by the {' or '.join(producers)} workflow.",
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
        context = current_operation_context()
        self._write_new(
            input_path,
            json.dumps(
                {
                    **payload,
                    "policy": policy.to_dict(),
                    "context": context.model_dump(mode="json") if context else None,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
        )
        process = await asyncio.create_subprocess_exec(
            *self._worker_command(input_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work,
            env=_child_env(work),
            **isolated_process_kwargs(),
        )
        jail = ProcessJail(self.core.settings.sandbox.memory_mb)
        jail.attach(process.pid)
        try:
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=float(self.core.settings.automation.transaction_timeout_s),
                )
            except asyncio.CancelledError:
                jail.kill()
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                await process.wait()
                raise
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
                "correlation_id": response.get("correlation_id"),
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
        return worker_command("ifc_console.automation.transaction_worker", input_path)

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
            self._fsync_directory(target.parent)
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()

    @staticmethod
    def _replace_target(source: Path, target: Path) -> None:
        deadline = time.monotonic() + 2.0
        while True:
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    def _target_lock(self, target: Path):
        lock = target.with_name(f".{target.name}.ifc-console.lock")
        return async_exclusive_file_lock(
            lock,
            timeout_s=float(self.core.settings.automation.transaction_lock_timeout_s),
            error_code="MODEL_BUSY",
        )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
