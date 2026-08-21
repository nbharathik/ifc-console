"""WorkspaceIndex: scanning, aliases, discipline hints, ranked search."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.mcp.envelope import ToolError
from ifc_console.workspace.index import WorkspaceIndex, guess_discipline, guess_revision

IDS_FIXTURE = Path(__file__).parents[1] / "fixtures" / "wall_firerating.ids"


def _project(tmp_path: Path, model: Path, names: list[str]) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in names:
        shutil.copy2(model, root / name)
    shutil.copy2(IDS_FIXTURE, root / "employer-requirements.ids")
    return root


def test_scan_indexes_kinds_without_loading(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    root = _project(tmp_path, minimal_ifc4_path, ["arch.ifc", "struct.ifc"])
    (root / "readme.md").write_text("an ingestable project document", encoding="utf-8")
    index = WorkspaceIndex(lambda: [root])
    index.scan()

    kinds = sorted(e.kind for e in index.entries)
    assert kinds == ["ids", "ifc", "ifc", "md"]
    model = index.get("arch")
    assert model is not None and model.detail["schema"] == "IFC4"
    spec = index.get("employer-requirements")
    assert spec is not None and spec.detail["specifications"] == 1


def test_aliases_are_unique_across_folders(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    root = _project(tmp_path, minimal_ifc4_path, ["arch.ifc"])
    sub = root / "revisions"
    sub.mkdir()
    shutil.copy2(minimal_ifc4_path, sub / "arch.ifc")
    index = WorkspaceIndex(lambda: [root])
    index.scan()
    aliases = sorted(e.alias for e in index.entries if e.kind == "ifc")
    assert aliases == ["arch", "arch-2"]


def test_scan_depth_and_symlink_directories(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    root = tmp_path / "deep"
    (root / "a" / "b" / "c").mkdir(parents=True)
    shutil.copy2(minimal_ifc4_path, root / "a" / "b" / "c" / "buried.ifc")
    shutil.copy2(minimal_ifc4_path, root / "a" / "shallow.ifc")
    index = WorkspaceIndex(lambda: [root], depth=2)
    index.scan()
    names = {e.path.name for e in index.entries}
    assert names == {"shallow.ifc"}


def test_scan_cap_is_reported(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    root = _project(tmp_path, minimal_ifc4_path, [f"m{i}.ifc" for i in range(6)])
    index = WorkspaceIndex(lambda: [root], cap=3)
    index.scan()
    assert index.truncated is True
    assert len(index.entries) <= 3


def test_primary_root_limits_a_scan(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    shutil.copy2(minimal_ifc4_path, first / "arch.ifc")
    shutil.copy2(minimal_ifc4_path, second / "struct.ifc")
    index = WorkspaceIndex(lambda: [first, second])
    index.primary_root = second
    index.scan()
    assert {entry.path.name for entry in index.entries if entry.kind == "ifc"} == {
        "struct.ifc"
    }
    assert index.stats()["roots"] == [str(second)]


def test_disabled_workspace_refuses_to_scan(tmp_path: Path) -> None:
    index = WorkspaceIndex(lambda: [tmp_path], enabled=False)
    with pytest.raises(ToolError) as excinfo:
        index.scan()
    assert excinfo.value.code == "WORKSPACE_DISABLED"


def test_scan_rejects_candidates_resolved_outside_root(
    tmp_path: Path, minimal_ifc4_path: Path, monkeypatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.ifc"
    shutil.copy2(minimal_ifc4_path, outside)
    index = WorkspaceIndex(lambda: [root])
    monkeypatch.setattr(index, "_walk", lambda _root, _budget: [outside])
    index.scan()
    assert index.entries == []


def test_find_ranks_and_flags_ambiguity(tmp_path: Path, minimal_ifc4_path: Path) -> None:
    root = _project(
        tmp_path,
        minimal_ifc4_path,
        ["ABC-XY-ZZ-XX-M3-A-0001.ifc", "ABC-XY-ZZ-XX-M3-S-0001.ifc", "old-architecture.ifc"],
    )
    index = WorkspaceIndex(lambda: [root])
    index.scan()

    # "architecture" must reach the ISO 19650 file whose only clue is the
    # role letter A, while the literally-named file still ranks first
    hits, ambiguous = index.find("architecture", ["ifc"])
    assert [h.path.name for h in hits] == [
        "old-architecture.ifc",
        "ABC-XY-ZZ-XX-M3-A-0001.ifc",
    ]
    assert ambiguous is False

    hits, _ = index.find("structural", ["ifc"])
    assert [h.path.name for h in hits] == ["ABC-XY-ZZ-XX-M3-S-0001.ifc"]

    hits, _ = index.find(None, ["ids"])
    assert [h.kind for h in hits] == ["ids"]


def test_find_reports_ambiguity_between_lookalikes(
    tmp_path: Path, minimal_ifc4_path: Path
) -> None:
    root = _project(tmp_path, minimal_ifc4_path, ["tower-r2.ifc", "tower-r3.ifc"])
    index = WorkspaceIndex(lambda: [root])
    index.scan()
    hits, ambiguous = index.find("tower", ["ifc"])
    assert len(hits) == 2
    assert ambiguous is True


def test_discipline_and_revision_hints() -> None:
    assert guess_discipline("ABC-XY-ZZ-XX-M3-A-0001") == "ARC"
    assert guess_discipline("tower-structural-model") == "STR"
    # a bare letter outside an ISO 19650 style name is not a discipline
    assert guess_discipline("a-model") is None
    assert guess_revision("tower-r3") == "r3"
    assert guess_revision("tower-20260803") == "2026-08-03"
    assert guess_revision("tower") is None
