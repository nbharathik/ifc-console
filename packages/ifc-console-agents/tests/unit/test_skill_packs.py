"""Skill packs: staging a zip, installing into the user home, uninstalling."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from ifc_console.core.results import ToolError
from ifc_console.knowledge.project import ProjectKnowledge

from ifc_console_agents.skill_packs import SkillPackStore, inspect_pack
from ifc_console_agents.skills import AgentSkillStore

SKILL = """---
name: sheet-pile-parameters
description: Identify a sheet pile profile and return its catalogue parameters.
applies_to: IfcPile
kind: prose
---

## When to use
Sheet piles.

## Steps
1. get_viewer_selection
"""

KNOWLEDGE = (
    "# Z sections\n\n## Z-section: width b\nSystem width between interlocks.\n\nSource: page 6\n"
)
ROWS = (
    '{"id": "az-26-700:per_m_wall", "designation": "AZ 26-700", "basis": "per_m_wall", '
    '"width_b_mm": 700, "source_page": 9, "verified": true}\n'
)


def _zip(files: dict[str, str | bytes], *, top: str | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(f"{top}/{name}" if top else name, data)
    return buffer.getvalue()


def _pack_files(name: str = "sheet-piles-test") -> dict[str, str]:
    return {
        "pack.json": json.dumps(
            {
                "name": name,
                "version": "1",
                "title": "Sheet piles",
                "task": "Identify profiles",
                "applies_to": "IfcPile",
                "source": {"title": "Catalogue 2027", "publisher": "Test"},
            }
        ),
        "SKILL.md": SKILL,
        "knowledge/z-sections.md": KNOWLEDGE,
        "data/profiles.jsonl": ROWS,
    }


@pytest.fixture
def stores(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    packs = SkillPackStore(home)
    skills = AgentSkillStore(project, user_dir=home)
    knowledge = ProjectKnowledge.for_library(home)
    try:
        yield home, packs, skills, knowledge
    finally:
        knowledge.close()


def _install(packs, skills, knowledge, files, filename="pack.zip", **kwargs):
    preview = packs.stage(_zip(files), filename)
    return packs.commit(preview["staging_id"], skills=skills, knowledge=knowledge, **kwargs)


def test_stage_previews_the_pack(stores):
    _home, packs, _skills, _knowledge = stores
    preview = packs.stage(_zip(_pack_files(), top="sheet-piles-test"), "sheet-piles-test.zip")
    assert preview["name"] == "sheet-piles-test"
    assert [skill["name"] for skill in preview["skills"]] == ["sheet-pile-parameters"]
    assert preview["skills"][0]["text"].startswith("---")
    assert [entry["path"] for entry in preview["knowledge"]] == ["knowledge/z-sections.md"]
    assert preview["tables"] == [
        {"path": "data/profiles.jsonl", "size_bytes": len(ROWS), "rows": 1}
    ]
    assert preview["synthesized_manifest"] is False
    assert "replaces" not in preview
    assert (packs.staging / preview["staging_id"] / "SKILL.md").is_file()


def test_install_registers_skill_and_indexes_knowledge(stores):
    home, packs, skills, knowledge = stores
    record = _install(packs, skills, knowledge, _pack_files(), agent="measure")
    assert record["skills"] == ["sheet-pile-parameters"]
    assert record["documents"] == [
        "agents/packs/sheet-piles-test/knowledge/z-sections.md",
        "agents/packs/sheet-piles-test/data/profiles.jsonl",
    ]
    assert record["tables"] == [{"path": "data/profiles.jsonl", "rows": 1}]
    # the table row is searchable by its designation
    assert knowledge.search("AZ 26-700", kind="row")[0]["meta"]["row"]["width_b_mm"] == 700
    assert record["agent"] == "measure"
    folder = home / "agents" / "packs" / "sheet-piles-test"
    assert (folder / "installed.json").is_file()
    assert (folder / "data" / "profiles.jsonl").is_file()
    assert not any(packs.staging.iterdir())

    entries = skills.entries()
    assert [(e["name"], e["scope"], e["pack"]) for e in entries] == [
        ("sheet-pile-parameters", "user", "sheet-piles-test")
    ]
    assert Path(entries[0]["path"]).parent == home / "agents" / "skills"
    assert skills.read("sheet-pile-parameters")["content"].startswith("## When to use")

    hits = knowledge.search("interlocks")
    assert hits and hits[0]["corpus"] == "library"
    source, target = knowledge.resolve("agents/packs/sheet-piles-test/knowledge/z-sections.md")
    assert target == (folder / "knowledge" / "z-sections.md").resolve()
    assert source["path"] == "agents/packs/sheet-piles-test/knowledge/z-sections.md"
    assert [pack["name"] for pack in packs.installed()] == ["sheet-piles-test"]


def test_project_skill_hides_user_skill_and_saves_stay_local(stores):
    _home, packs, skills, knowledge = stores
    _install(packs, skills, knowledge, _pack_files())
    skills.save("sheet-pile-parameters", "## Steps\nlocal override\n", description="local")
    rows = skills.entries()
    assert [(row["name"], row["scope"]) for row in rows] == [("sheet-pile-parameters", "project")]
    assert (skills.directory / "sheet-pile-parameters.md").is_file()
    assert skills.read("sheet-pile-parameters")["description"] == "local"


def test_reinstall_replaces_and_uninstall_removes_everything(stores):
    home, packs, skills, knowledge = stores
    _install(packs, skills, knowledge, _pack_files())
    files = _pack_files()
    files["pack.json"] = json.dumps({"name": "sheet-piles-test", "version": 2})
    files["knowledge/u-sections.md"] = "# U sections\n\n## U-section: height h\nOuter.\n"
    del files["knowledge/z-sections.md"]
    preview = packs.stage(_zip(files), "pack.zip")
    assert preview["replaces"]["version"] == "1"
    second = packs.commit(preview["staging_id"], skills=skills, knowledge=knowledge)
    assert second["replaced"] == "1"
    assert second["version"] == "2"
    assert [entry["name"] for entry in skills.entries()] == ["sheet-pile-parameters"]
    assert {source["path"] for source in knowledge.sources()} == {
        "agents/packs/sheet-piles-test/knowledge/u-sections.md",
        "agents/packs/sheet-piles-test/data/profiles.jsonl",
    }

    removed = packs.uninstall("sheet-piles-test", skills=skills, knowledge=knowledge)
    assert removed["skills"] == ["sheet-pile-parameters"]
    assert skills.entries() == []
    assert knowledge.sources() == []
    assert not (home / "agents" / "packs" / "sheet-piles-test").exists()
    assert packs.installed() == []


def test_uninstall_keeps_other_packs(stores):
    _home, packs, skills, knowledge = stores
    _install(packs, skills, knowledge, _pack_files("one"), "one.zip")
    other = _pack_files("two")
    other["SKILL.md"] = SKILL.replace("sheet-pile-parameters", "other-skill")
    _install(packs, skills, knowledge, other, "two.zip")
    assert sorted(entry["name"] for entry in skills.entries()) == [
        "other-skill",
        "sheet-pile-parameters",
    ]
    packs.uninstall("one", skills=skills, knowledge=knowledge)
    assert [entry["name"] for entry in skills.entries()] == ["other-skill"]
    assert {source["path"] for source in knowledge.sources()} == {
        "agents/packs/two/knowledge/z-sections.md",
        "agents/packs/two/data/profiles.jsonl",
    }
    assert knowledge.search("interlocks")


def test_partial_pack_with_synthesized_manifest(stores):
    home, packs, skills, knowledge = stores
    preview = packs.stage(_zip({"SKILL.md": SKILL}), "My Pack.zip")
    assert preview["synthesized_manifest"] is True
    assert preview["name"] == "my-pack"
    assert any("pack.json is missing" in warning for warning in preview["warnings"])
    record = packs.commit(preview["staging_id"], skills=skills, knowledge=knowledge)
    assert record["documents"] == []
    assert record["skills"] == ["sheet-pile-parameters"]
    written = json.loads((home / "agents" / "packs" / "my-pack" / "pack.json").read_text())
    assert written["name"] == "my-pack"
    assert knowledge.sources() == []


def test_knowledge_only_pack_warns_but_installs(stores):
    _home, packs, skills, knowledge = stores
    files = {"pack.json": json.dumps({"name": "docs-only"}), "knowledge/a.md": KNOWLEDGE}
    preview = packs.stage(_zip(files), "p.zip")
    assert preview["skills"] == []
    assert any("no SKILL.md" in warning for warning in preview["warnings"])
    record = packs.commit(preview["staging_id"], skills=skills, knowledge=knowledge)
    assert record["skills"] == []
    assert record["documents"] == ["agents/packs/docs-only/knowledge/a.md"]


@pytest.mark.parametrize("bad", ["../evil.md", "C:/evil.md"])
def test_unsafe_zip_paths_are_refused(stores, bad):
    _home, packs, *_ = stores
    with pytest.raises(ToolError) as info:
        packs.stage(_zip({"pack.json": "{}", bad: "x"}), "bad.zip")
    assert info.value.code == "INVALID_INPUT"
    assert not packs.staging.exists() or not any(packs.staging.iterdir())


def test_bad_uploads_are_refused(stores):
    _home, packs, *_ = stores
    with pytest.raises(ToolError):
        packs.stage(b"not a zip", "x.zip")
    with pytest.raises(ToolError):
        packs.stage(b"", "x.zip")
    with pytest.raises(ToolError) as info:
        packs.stage(_zip({"pack.json": "{}"}), "empty.zip")
    assert "nothing to install" in str(info.value)


def test_oversized_skill_is_refused(stores):
    _home, packs, *_ = stores
    files = {"pack.json": json.dumps({"name": "big"}), "SKILL.md": SKILL + "x" * (64 * 1024)}
    with pytest.raises(ToolError) as info:
        packs.stage(_zip(files), "big.zip")
    assert "larger than 64 KB" in str(info.value)


def test_inspect_rejects_a_bad_manifest_name(tmp_path: Path):
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "pack.json").write_text('{"name": "Bad Name"}', encoding="utf-8")
    (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
    with pytest.raises(ToolError) as info:
        inspect_pack(folder)
    assert "pack.json name" in str(info.value)


def test_user_scope_import_and_delete_are_pack_aware(tmp_path: Path):
    home = tmp_path / "home"
    project = tmp_path / "proj"
    project.mkdir()
    store = AgentSkillStore(project, user_dir=home)
    row = store.import_file("SKILL.md", SKILL.encode(), scope="user", pack="one")
    assert (row["scope"], row["pack"]) == ("user", "one")
    again = store.import_file("SKILL.md", SKILL.encode(), scope="user", pack="one")
    assert again["name"] == row["name"]
    other = store.import_file("SKILL.md", SKILL.encode(), scope="user", pack="two")
    assert other["name"] == "sheet-pile-parameters-2"
    assert store.delete("sheet-pile-parameters", scope="user", pack="two") is False
    assert store.delete("sheet-pile-parameters", scope="user", pack="one") is True
    assert [entry["name"] for entry in store.entries()] == ["sheet-pile-parameters-2"]
    with pytest.raises(ToolError):
        AgentSkillStore(project).import_file("SKILL.md", SKILL.encode(), scope="user")


def test_project_knowledge_label_keeps_indexes_apart(tmp_path: Path):
    home = tmp_path / "home"
    (home / "agents" / "packs" / "p" / "knowledge").mkdir(parents=True)
    (home / "agents" / "packs" / "p" / "knowledge" / "a.md").write_text(KNOWLEDGE)
    packs = ProjectKnowledge.for_library(home)
    project = ProjectKnowledge(home)
    try:
        packs.ingest([home / "agents" / "packs" / "p"])
        assert packs.ready and not project.ready
        assert packs.path.name.startswith("library-kb-")
        assert packs.manifest_path.name == "library-sources.json"
        assert packs.sources()[0]["path"] == "agents/packs/p/knowledge/a.md"
    finally:
        packs.close()
        project.close()
