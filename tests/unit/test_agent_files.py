"""Project-local reference storage and image access for built-in agents."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console import LocalRuntime
from ifc_console.agents.files import AgentReferenceStore
from ifc_console.core.results import ToolError
from ifc_console.knowledge.project import ProjectKnowledge


def test_reference_store_is_local_atomic_and_collision_safe(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    store = AgentReferenceStore(project)

    first = store.save_upload("manual.md", b"# First")
    second = store.save_upload("../manual.md", b"# Second")

    assert first.parent == project / ".ifc-console" / "agents" / "references"
    assert first.name == "manual.md"
    assert second.name == "manual-2.md"
    assert first.read_bytes() == b"# First"
    assert second.read_bytes() == b"# Second"


def test_reference_store_rejects_unsupported_or_empty_uploads(tmp_path: Path) -> None:
    store = AgentReferenceStore(tmp_path)
    with pytest.raises(ToolError):
        store.save_upload("program.exe", b"data")
    with pytest.raises(ToolError):
        store.save_upload("empty.txt", b"")


def test_sync_indexes_files_copied_into_the_folder_by_hand(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    references = AgentReferenceStore(project)
    references.directory.mkdir(parents=True)
    (references.directory / "rules.txt").write_text(
        "Measure wall thickness from structural layers.", encoding="utf-8"
    )
    knowledge = ProjectKnowledge(project)
    try:
        report = references.sync(knowledge)
        assert report["changed"] is True
        assert report["files"][0]["indexed"] is True
        assert knowledge.search("structural wall thickness")
        assert references.sync(knowledge)["changed"] is False
    finally:
        knowledge.close()


@pytest.mark.asyncio
async def test_indexed_reference_image_is_available_as_sdk_vision(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    references = AgentReferenceStore(project)
    image = references.save_upload("detail.png", b"\x89PNG\r\n\x1a\nreference pixels")
    knowledge = ProjectKnowledge(project)
    try:
        knowledge.ingest([image])
    finally:
        knowledge.close()

    async with await LocalRuntime.open(
        home=tmp_path / "home", project_dir=project
    ) as runtime:
        documents = await runtime.workbench.project_documents(media="image")
        assert documents[0]["path"].endswith("detail.png")
        result = await runtime.workbench.project_reference_image(documents[0]["path"])

    assert result["images"][0]["media_type"] == "image/png"
    assert result["images"][0]["data"]
