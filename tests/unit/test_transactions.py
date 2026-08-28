"""Revision-bound structured property commits and verified restore."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import ifcopenshell
import pytest

from ifc_console.app import AppCore
from ifc_console.automation.files import sha256_file
from ifc_console.core.changes import PropertyPreview
from ifc_console.core.results import ToolError
from ifc_console.policy.modes import Mode
from ifc_console.settings import SettingsStore


async def _core(tmp_path: Path, source: Path, *, mode: Mode = Mode.ASK) -> AppCore:
    model = tmp_path / "model.ifc"
    shutil.copy2(source, model)
    core = AppCore(
        SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={}),
        mode=mode,
        transport="sdk",
    )
    core.start_audit()
    core.add_allowed_dir(tmp_path)
    await core.open_model(model)
    return core


def _target(core: AppCore) -> tuple[str, int]:
    wall = core.session.ifc.by_type("IfcWall")[0]
    for relation in wall.IsDefinedBy:
        definition = relation.RelatingPropertyDefinition
        if definition.is_a("IfcPropertySet") and definition.Name == "Pset_WallCommon":
            prop = next(item for item in definition.HasProperties if item.Name == "FireRating")
            return wall.GlobalId, prop.id()
    raise AssertionError("fixture property missing")


def _disk_value(path: Path, property_id: int) -> str:
    return ifcopenshell.open(str(path)).by_id(property_id).NominalValue.wrappedValue


async def _preview(core: AppCore, value: str = "F60"):
    global_id, _property_id = _target(core)
    return await core.transactions.preview_property_value(
        global_ids=[global_id],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value=value,
    )


async def test_preview_is_isolated_typed_and_durable(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        _global_id, property_id = _target(core)
        record = await _preview(core)

        change = record.change_set.changes[0]
        assert change.before == "F30"
        assert change.after == "F60"
        assert change.nominal_type == "IfcLabel"
        assert sha256_file(core.session.path) == original_sha
        assert _disk_value(core.session.path, property_id) == "F30"
        assert core.session.dirty is False
        assert core.transactions.get_change_set(record.change_set_id) == record
        controls = record.artifact.metadata["worker_controls"]
        assert "network-blocked" in controls
        assert record.change_set.context is not None
        correlation_id = record.change_set.context.correlation_id
        assert record.artifact.correlation_ids == (correlation_id,)
        assert record.artifact.metadata["worker_controls"]
    finally:
        core.shutdown()


async def test_preview_rejects_stale_missing_and_noop_changes(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        global_id, _property_id = _target(core)
        with pytest.raises(ToolError, match="REVISION_CONFLICT"):
            await core.transactions.preview_property_value(
                global_ids=[global_id],
                pset_name="Pset_WallCommon",
                property_name="FireRating",
                value="F60",
                expected_revision="stale:1",
            )
        with pytest.raises(ToolError) as missing:
            await core.transactions.preview_property_value(
                global_ids=[global_id],
                pset_name="Pset_WallCommon",
                property_name="DoesNotExist",
                value="x",
            )
        assert missing.value.code == "PROPERTY_NOT_FOUND"
        with pytest.raises(ToolError) as noop:
            await core.transactions.preview_property_value(
                global_ids=[global_id],
                pset_name="Pset_WallCommon",
                property_name="FireRating",
                value="F30",
            )
        assert noop.value.code == "CHANGESET_INVALID"
    finally:
        core.shutdown()


async def test_create_missing_property_set_commits_and_restores(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        global_id, _property_id = _target(core)
        preview = await core.transactions.preview_property_value(
            global_ids=[global_id],
            pset_name="Company_QA",
            property_name="ReviewStatus",
            value="Checked",
            create_missing=True,
        )

        change = preview.change_set.changes[0]
        assert change.kind == "property_create"
        assert change.pset_id is None
        assert change.nominal_type == "IfcLabel"
        assert not _occurrence_psets(core.session.ifc.by_guid(global_id), "Company_QA")

        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )
        reopened = ifcopenshell.open(str(core.session.path))
        pset = _occurrence_psets(reopened.by_guid(global_id), "Company_QA")[0]
        prop = next(item for item in pset.HasProperties if item.Name == "ReviewStatus")
        assert prop.NominalValue.is_a() == "IfcLabel"
        assert prop.NominalValue.wrappedValue == "Checked"

        await core.transactions.restore(commit.commit_id, confirm=True)
        restored = ifcopenshell.open(str(core.session.path))
        assert not _occurrence_psets(restored.by_guid(global_id), "Company_QA")
    finally:
        core.shutdown()


async def test_atomic_preview_creates_two_properties_in_one_missing_pset(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        global_id, _property_id = _target(core)
        preview = await core.transactions.preview_property_values(
            global_ids=[global_id],
            properties=[
                PropertyPreview(
                    pset_name="Company_QA",
                    property_name="ReviewStatus",
                    value="Checked",
                    create_missing=True,
                    nominal_type="IfcLabel",
                ),
                PropertyPreview(
                    pset_name="Company_QA",
                    property_name="Reviewer",
                    value="Ada",
                    create_missing=True,
                    nominal_type="IfcText",
                ),
            ],
        )

        assert len(preview.change_set.changes) == 2
        assert all(change.kind == "property_create" for change in preview.change_set.changes)
        assert all(change.pset_id is None for change in preview.change_set.changes)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)

        reopened = ifcopenshell.open(str(core.session.path))
        psets = _occurrence_psets(reopened.by_guid(global_id), "Company_QA")
        assert len(psets) == 1
        values = {prop.Name: prop.NominalValue.wrappedValue for prop in psets[0].HasProperties}
        assert values == {"ReviewStatus": "Checked", "Reviewer": "Ada"}
    finally:
        core.shutdown()


async def test_create_missing_property_in_existing_pset_preserves_explicit_type(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        global_id, _property_id = _target(core)
        preview = await core.transactions.preview_property_value(
            global_ids=[global_id],
            pset_name="Pset_WallCommon",
            property_name="CompanyTargetLength",
            value=1250.0,
            create_missing=True,
            nominal_type="IfcLengthMeasure",
        )

        change = preview.change_set.changes[0]
        assert change.kind == "property_create"
        assert change.pset_id is not None
        assert change.nominal_type == "IfcLengthMeasure"
        assert change.after == 1250.0
    finally:
        core.shutdown()


async def test_property_set_creation_does_not_add_ifc2x3_schema_issues(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    from ifc_console.ifc.validation import run_schema_validation

    source = minimal_ifc4_path.with_name("minimal_ifc2x3.ifc")
    baseline_issues = run_schema_validation(
        ifcopenshell.open(str(source)), express_rules=False, max_issues=20
    )["issue_count"]
    core = await _core(tmp_path, source, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        global_id = core.session.ifc.by_type("IfcWall")[0].GlobalId
        preview = await core.transactions.preview_property_value(
            global_ids=[global_id],
            pset_name="Company_QA",
            property_name="ReviewStatus",
            value="Checked",
            create_missing=True,
        )
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )

        assert commit.result.schema_issue_count == baseline_issues
        reopened = ifcopenshell.open(str(core.session.path))
        pset = _occurrence_psets(reopened.by_guid(global_id), "Company_QA")[0]
        prop = next(item for item in pset.HasProperties if item.Name == "ReviewStatus")
        assert prop.NominalValue.wrappedValue == "Checked"

        await core.transactions.restore(commit.commit_id, confirm=True)
    finally:
        core.shutdown()


async def test_create_missing_rejects_null_and_entity_nominal_types(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        global_id, _property_id = _target(core)
        with pytest.raises(ToolError) as null_value:
            await core.transactions.preview_property_value(
                global_ids=[global_id],
                pset_name="Company_QA",
                property_name="Empty",
                value=None,
                create_missing=True,
            )
        assert null_value.value.code == "CHANGESET_INVALID"

        with pytest.raises(ToolError) as entity_type:
            await core.transactions.preview_property_value(
                global_ids=[global_id],
                pset_name="Company_QA",
                property_name="UnsafeType",
                value="x",
                create_missing=True,
                nominal_type="IfcWall",
            )
        assert entity_type.value.code == "CHANGESET_INVALID"
    finally:
        core.shutdown()


async def test_classification_assignment_creates_reuses_commits_and_restores(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        global_ids = [wall.GlobalId for wall in core.session.ifc.by_type("IfcWall")[:2]]
        preview = await core.transactions.preview_classification_assignment(
            global_ids=global_ids,
            classification_name="Company Classification",
            identification="WALL-EXT",
            reference_name="External wall",
        )

        assert preview.change_set.operation == "classification.assign"
        assert len(preview.change_set.changes) == len(global_ids)
        assert all(
            change.kind == "classification_assignment" for change in preview.change_set.changes
        )
        assert all(change.classification_id is None for change in preview.change_set.changes)
        assert all(change.reference_id is None for change in preview.change_set.changes)

        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )
        reopened = ifcopenshell.open(str(core.session.path))
        systems = [
            item
            for item in reopened.by_type("IfcClassification")
            if item.Name == "Company Classification"
        ]
        assert len(systems) == 1
        references = [
            item
            for item in reopened.by_type("IfcClassificationReference")
            if item.ReferencedSource == systems[0] and item.Identification == "WALL-EXT"
        ]
        assert len(references) == 1
        assert references[0].Name == "External wall"
        for global_id in global_ids:
            assert any(
                relation.RelatingClassification == references[0]
                for relation in reopened.by_guid(global_id).HasAssociations
                if relation.is_a("IfcRelAssociatesClassification")
            )

        with pytest.raises(ToolError) as duplicate:
            await core.transactions.preview_classification_assignment(
                global_ids=[global_ids[0]],
                classification_name="Company Classification",
                identification="WALL-EXT",
                reference_name="External wall",
            )
        assert duplicate.value.code == "CHANGESET_INVALID"

        await core.transactions.restore(commit.commit_id, confirm=True)
        restored = ifcopenshell.open(str(core.session.path))
        assert not [
            item
            for item in restored.by_type("IfcClassification")
            if item.Name == "Company Classification"
        ]
    finally:
        core.shutdown()


async def test_classification_assignment_is_schema_valid_in_ifc2x3(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    from ifc_console.ifc.validation import run_schema_validation

    source = minimal_ifc4_path.with_name("minimal_ifc2x3.ifc")
    baseline_issues = run_schema_validation(
        ifcopenshell.open(str(source)), express_rules=False, max_issues=20
    )["issue_count"]
    core = await _core(tmp_path, source, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        global_id = core.session.ifc.by_type("IfcWall")[0].GlobalId
        preview = await core.transactions.preview_classification_assignment(
            global_ids=[global_id],
            classification_name="Company Classification",
            identification="WALL-EXT",
            reference_name="External wall",
        )
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )

        assert commit.result.schema_issue_count == baseline_issues
        reopened = ifcopenshell.open(str(core.session.path))
        reference = next(
            item
            for item in reopened.by_type("IfcClassificationReference")
            if item.ItemReference == "WALL-EXT"
        )
        assert reference.Name == "External wall"

        await core.transactions.restore(commit.commit_id, confirm=True)
    finally:
        core.shutdown()


async def test_commit_requires_edit_mode_and_matching_explicit_approval(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        first = await _preview(core, "F60")
        second = await _preview(core, "F90")
        approval = core.transactions.approve(first.change_set_id, approved_by="bim-manager")
        with pytest.raises(ToolError) as ask_blocked:
            await core.transactions.commit(first.change_set_id, approval_id=approval.approval_id)
        assert ask_blocked.value.code == "ASK_MODE_BLOCKED"

        core.set_mode(Mode.EDIT, by="test")
        with pytest.raises(ToolError) as mismatch:
            await core.transactions.commit(second.change_set_id, approval_id=approval.approval_id)
        assert mismatch.value.code == "APPROVAL_MISMATCH"
    finally:
        core.shutdown()


async def test_commit_and_restore_verify_bytes_and_reload_session(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        assert core.session.path is not None
        target = core.session.path
        original_sha = sha256_file(target)
        _global_id, property_id = _target(core)
        preview = await _preview(core)
        approval = core.transactions.approve(
            preview.change_set_id, approved_by="bim-manager", reason="approved test edit"
        )
        core.set_mode(Mode.EDIT, by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )

        assert commit.result.previous_sha256 == original_sha
        assert commit.result.committed_sha256 == sha256_file(target)
        assert commit.result.schema_valid is True
        assert _disk_value(target, property_id) == "F60"
        assert core.session.ifc.by_id(property_id).NominalValue.wrappedValue == "F60"
        assert core.session.dirty is False
        assert core.transactions.get_commit(commit.commit_id) == commit
        assert preview.change_set_id in commit.result.backup_artifact.references
        assert set(commit.artifact.references) == {
            preview.change_set_id,
            approval.approval_id,
            commit.result.backup_artifact.artifact_id,
        }
        assert (
            sha256_file_bytes(core.artifacts.read_bytes(commit.result.backup_artifact.artifact_id))
            == original_sha
        )

        with pytest.raises(ToolError) as unconfirmed:
            await core.transactions.restore(commit.commit_id)
        assert unconfirmed.value.code == "APPROVAL_REQUIRED"

        restored = await core.transactions.restore(commit.commit_id, confirm=True)
        assert restored.result.restored_sha256 == original_sha
        assert _disk_value(target, property_id) == "F30"
        assert core.session.ifc.by_id(property_id).NominalValue.wrappedValue == "F30"
        assert core.session.dirty is False
        assert set(restored.artifact.references) == {
            commit.commit_id,
            restored.result.safety_artifact.artifact_id,
        }
    finally:
        core.shutdown()


async def test_source_change_after_preview_refuses_commit(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path)
    try:
        assert core.session.path is not None
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        core.session.path.write_bytes(core.session.path.read_bytes() + b"\n")
        changed_sha = sha256_file(core.session.path)
        core.set_mode(Mode.EDIT, by="test")
        with pytest.raises(ToolError) as conflict:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)
        assert conflict.value.code == "REVISION_CONFLICT"
        assert sha256_file(core.session.path) == changed_sha
    finally:
        core.shutdown()


async def test_replace_failure_leaves_source_unchanged(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")

        def fail_replace(_source: Path, _target: Path) -> None:
            raise OSError("injected replace failure")

        monkeypatch.setattr(core.transactions, "_replace_target", fail_replace)
        with pytest.raises(ToolError) as failed:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)
        assert failed.value.code == "COMMIT_FAILED"
        assert sha256_file(core.session.path) == original_sha
        assert core.session.dirty is False
    finally:
        core.shutdown()


def test_target_replace_retries_transient_sharing_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ifc_console.application.transactions as transactions_module

    source = tmp_path / "candidate.ifc"
    target = tmp_path / "model.ifc"
    source.write_bytes(b"after")
    target.write_bytes(b"before")
    real_replace = __import__("os").replace
    attempts = 0

    def flaky_replace(replacement: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("injected sharing violation")
        real_replace(replacement, destination)

    monkeypatch.setattr(transactions_module.os, "replace", flaky_replace)
    transactions_module.TransactionService._replace_target(source, target)

    assert attempts == 3
    assert target.read_bytes() == b"after"


async def test_backup_failure_aborts_before_replacement(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        real_put = core.artifacts.put_file

        def fail_backup(path: Path, **kwargs):
            if kwargs.get("kind") == "ifc-verified-backup":
                raise OSError("injected backup failure")
            return real_put(path, **kwargs)

        monkeypatch.setattr(core.artifacts, "put_file", fail_backup)
        with pytest.raises(ToolError) as failed:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)
        assert failed.value.code == "COMMIT_FAILED"
        assert sha256_file(core.session.path) == original_sha
    finally:
        core.shutdown()


async def test_corrupt_candidate_is_rolled_back_after_reload_failure(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        real_worker = core.transactions._run_worker

        async def corrupt_output(payload, work, *, read_dirs):
            result, worker = await real_worker(payload, work, read_dirs=read_dirs)
            if payload.get("action") == "apply":
                Path(result["output_path"]).write_text("not an IFC", encoding="utf-8")
            return result, worker

        monkeypatch.setattr(core.transactions, "_run_worker", corrupt_output)
        with pytest.raises(ToolError) as failed:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)
        assert failed.value.code == "COMMIT_FAILED"
        assert sha256_file(core.session.path) == original_sha
        assert core.session.dirty is False
    finally:
        core.shutdown()


async def test_schema_regression_is_rejected_before_target_replacement(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        real_worker = core.transactions._run_worker

        async def report_regression(payload, work, *, read_dirs):
            result, worker = await real_worker(payload, work, read_dirs=read_dirs)
            if payload.get("action") == "apply":
                result["schema_regression_count"] = 1
            return result, worker

        monkeypatch.setattr(core.transactions, "_run_worker", report_regression)
        with pytest.raises(ToolError) as failed:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)

        assert failed.value.code == "COMMIT_FAILED"
        assert "new schema validation" in failed.value.message
        assert sha256_file(core.session.path) == original_sha
        assert not core.transactions.journals.list()
    finally:
        core.shutdown()


async def test_restore_refuses_target_modified_after_commit(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )
        core.session.path.write_bytes(core.session.path.read_bytes() + b"\n")
        changed_sha = sha256_file(core.session.path)
        with pytest.raises(ToolError) as conflict:
            await core.transactions.restore(commit.commit_id, confirm=True)
        assert conflict.value.code == "RESTORE_CONFLICT"
        assert sha256_file(core.session.path) == changed_sha
    finally:
        core.shutdown()


async def test_commit_and_restore_do_not_buffer_ifc_paths(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        original_read_bytes = Path.read_bytes

        def reject_ifc_read_bytes(path: Path) -> bytes:
            if path.suffix.lower() == ".ifc":
                raise AssertionError("transaction supervisor buffered an IFC file")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", reject_ifc_read_bytes)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        commit = await core.transactions.commit(
            preview.change_set_id, approval_id=approval.approval_id
        )
        restored = await core.transactions.restore(commit.commit_id, confirm=True)

        assert restored.result.restored_sha256 == commit.result.previous_sha256
    finally:
        core.shutdown()


async def test_receipt_failure_rolls_back_and_records_terminal_journal(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        real_put_text = core.artifacts.put_text

        def fail_receipt(text: str, **kwargs):
            if kwargs.get("kind") == "ifc-commit-receipt":
                raise OSError("injected receipt failure")
            return real_put_text(text, **kwargs)

        monkeypatch.setattr(core.artifacts, "put_text", fail_receipt)
        with pytest.raises(ToolError) as failed:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)

        assert failed.value.code == "COMMIT_FAILED"
        assert sha256_file(core.session.path) == original_sha
        journal = core.transactions.journals.list()[-1]
        assert journal.phase.value == "rolled_back"
        assert journal.expected_receipt_id is not None
    finally:
        core.shutdown()


async def test_cancellation_during_finalization_rolls_back_before_propagating(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        real_reload = core.session.reload
        calls = 0

        async def cancel_first_reload() -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise asyncio.CancelledError
            await real_reload()

        monkeypatch.setattr(core.session, "reload", cancel_first_reload)
        with pytest.raises(asyncio.CancelledError):
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)

        assert sha256_file(core.session.path) == original_sha
        assert core.transactions.journals.list()[-1].phase.value == "rolled_back"
    finally:
        core.shutdown()


async def test_failed_rollback_blocks_later_writes(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    core = await _core(tmp_path, minimal_ifc4_path, mode=Mode.EDIT)
    try:
        assert core.session.path is not None
        original_sha = sha256_file(core.session.path)
        preview = await _preview(core)
        approval = core.transactions.approve(preview.change_set_id, approved_by="test")
        real_put_text = core.artifacts.put_text
        real_replace = core.transactions._replace_file

        def fail_receipt(text: str, **kwargs):
            if kwargs.get("kind") == "ifc-commit-receipt":
                raise OSError("injected receipt failure")
            return real_put_text(text, **kwargs)

        def fail_rollback(target: Path, source: Path, *, expected_sha256: str) -> None:
            if expected_sha256 == original_sha:
                raise OSError("injected rollback failure")
            real_replace(target, source, expected_sha256=expected_sha256)

        monkeypatch.setattr(core.artifacts, "put_text", fail_receipt)
        monkeypatch.setattr(core.transactions, "_replace_file", fail_rollback)
        with pytest.raises(ToolError) as failed:
            await core.transactions.commit(preview.change_set_id, approval_id=approval.approval_id)

        assert failed.value.code == "COMMIT_FAILED"
        assert sha256_file(core.session.path) != original_sha
        journal = core.transactions.journals.list()[-1]
        assert journal.phase.value == "recovery_failed"
        with pytest.raises(ToolError) as blocked:
            core.transactions.journals.ensure_target_ready(core.session.path)
        assert blocked.value.code == "TRANSACTION_RECOVERY_REQUIRED"
    finally:
        core.shutdown()


def sha256_file_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _occurrence_psets(element, name: str) -> list:
    return [
        relation.RelatingPropertyDefinition
        for relation in element.IsDefinedBy
        if relation.is_a("IfcRelDefinesByProperties")
        and relation.RelatingPropertyDefinition.is_a("IfcPropertySet")
        and relation.RelatingPropertyDefinition.Name == name
    ]
