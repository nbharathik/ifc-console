"""Content-addressed artifact storage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ifc_console.application.artifacts import ArtifactService
from ifc_console.core.results import ToolError


def test_artifacts_are_deduplicated_and_checksum_verified(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    first = service.put_text(
        '{"passed": true}',
        name="report.json",
        kind="validation-report",
        media_type="application/json",
        producer="job-one",
    )
    second = service.put_text(
        '{"passed": true}',
        name="same.json",
        kind="validation-report",
        media_type="application/json",
        producer="job-two",
    )

    assert first.artifact_id == second.artifact_id
    assert second == first
    assert service.read_text(first.artifact_id) == '{"passed": true}'
    assert service.get(first.artifact_id).sha256 == first.sha256
    assert len(service.list()) == 1


def test_corrupt_artifact_content_is_refused(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    ref = service.put_text(
        "trusted",
        name="report.txt",
        kind="report",
        media_type="text/plain",
        producer="test",
    )
    service._content_path(ref.sha256).write_text("changed", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        service.read_bytes(ref.artifact_id)
    assert excinfo.value.code == "ARTIFACT_CORRUPT"


def test_artifact_metadata_cannot_change_content_identity(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    ref = service.put_text(
        "trusted",
        name="report.txt",
        kind="report",
        media_type="text/plain",
        producer="test",
    )
    metadata_path = service._metadata_path(ref.sha256)
    payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        service.get(ref.artifact_id)
    assert excinfo.value.code == "ARTIFACT_CORRUPT"


def test_artifact_export_does_not_overwrite_by_default(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    ref = service.put_text(
        "report",
        name="report.txt",
        kind="report",
        media_type="text/plain",
        producer="test",
    )
    target = tmp_path / "out" / "report.txt"
    assert service.export(ref.artifact_id, target) == target.resolve()
    with pytest.raises(ToolError) as excinfo:
        service.export(ref.artifact_id, target)
    assert excinfo.value.code == "FILE_EXISTS"


def test_file_ingest_and_export_stream_without_path_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    source = tmp_path / "large.ifc"
    source.write_bytes(b"ifc-data" * 200_000)

    def reject_read_bytes(_path: Path) -> bytes:
        raise AssertionError("whole-file buffering is not allowed")

    monkeypatch.setattr(Path, "read_bytes", reject_read_bytes)
    ref = service.put_file(
        source,
        name="large.ifc",
        kind="ifc-model",
        media_type="application/x-step",
        producer="test",
    )
    target = tmp_path / "export" / "large.ifc"
    assert service.export(ref.artifact_id, target) == target.resolve()
    assert service.verify(ref.artifact_id) == ref
    assert target.stat().st_size == source.stat().st_size


def test_file_ingest_refuses_an_unexpected_checksum(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    source = tmp_path / "source.ifc"
    source.write_text("content", encoding="utf-8")

    with pytest.raises(ToolError) as excinfo:
        service.put_file(
            source,
            name="source.ifc",
            kind="ifc-model",
            media_type="application/x-step",
            producer="test",
            expected_sha256="0" * 64,
        )
    assert excinfo.value.code == "SOURCE_CHANGED"
    assert service.list() == []


def test_deduplicated_artifacts_conservatively_merge_references(tmp_path: Path) -> None:
    service = ArtifactService(tmp_path / "artifacts")
    child_one = service.put_text(
        "one", name="one", kind="test", media_type="text/plain", producer="test"
    )
    child_two = service.put_text(
        "two", name="two", kind="test", media_type="text/plain", producer="test"
    )
    parent = service.put_text(
        "same",
        name="parent",
        kind="test",
        media_type="text/plain",
        producer="first",
        references=(child_one.artifact_id,),
    )
    repeated = service.put_text(
        "same",
        name="alias",
        kind="test",
        media_type="text/plain",
        producer="second",
        references=(child_two.artifact_id,),
    )

    assert repeated.artifact_id == parent.artifact_id
    assert set(service.get(parent.artifact_id).references) == {
        child_one.artifact_id,
        child_two.artifact_id,
    }
