"""Revision-bound structured property commits and verified restore."""

from __future__ import annotations

import shutil
from pathlib import Path

import ifcopenshell
import pytest

from ifc_console.app import AppCore
from ifc_console.automation.files import sha256_file
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


def sha256_file_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()
