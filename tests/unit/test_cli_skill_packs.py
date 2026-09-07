"""The `ifc-console agents pack` commands install packs under the console home."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

pytest.importorskip("ifc_console_agents")

from ifc_console.cli import _cmd_agents_pack  # noqa: E402

SKILL = "---\nname: demo-skill\ndescription: A demo.\nkind: prose\n---\n\n## Steps\n1. look\n"


def _pack_folder(root: Path) -> Path:
    folder = root / "demo-pack"
    (folder / "knowledge").mkdir(parents=True)
    (folder / "pack.json").write_text(json.dumps({"name": "demo-pack", "version": "1"}))
    (folder / "SKILL.md").write_text(SKILL, encoding="utf-8")
    (folder / "knowledge" / "a.md").write_text("# A\n\n## A: thing\nText.\n\nSource: page 1\n")
    return folder


def _args(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**{"json": False, "agent": None, **kwargs})


def test_pack_check_install_list_uninstall(home: Path, tmp_path: Path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    folder = _pack_folder(tmp_path)

    assert _cmd_agents_pack(_args(pack_action="check", path=folder)) == 0
    out = capsys.readouterr().out
    assert "pack     demo-pack v1" in out and "#demo-skill" in out
    assert not (home / "agents" / "packs" / "demo-pack").exists()

    assert _cmd_agents_pack(_args(pack_action="install", path=folder)) == 0
    out = capsys.readouterr().out
    assert "installed into" in out and "#demo-skill" in out
    assert (home / "agents" / "packs" / "demo-pack" / "installed.json").is_file()
    assert (home / "agents" / "skills" / "demo-skill.md").is_file()
    assert not (project / ".ifc-console" / "agents" / "skills").exists()

    assert _cmd_agents_pack(_args(pack_action="list", json=True)) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [row["name"] for row in listed] == ["demo-pack"]
    assert listed[0]["documents"] == ["agents/packs/demo-pack/knowledge/a.md"]

    assert _cmd_agents_pack(_args(pack_action="uninstall", name="demo-pack")) == 0
    assert "removed demo-pack: 1 skills, 1 documents" in capsys.readouterr().out
    assert not (home / "agents" / "packs" / "demo-pack").exists()
    assert not (home / "agents" / "skills" / "demo-skill.md").exists()


def test_pack_install_reports_a_missing_path(home: Path, tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert _cmd_agents_pack(_args(pack_action="install", path=tmp_path / "nope.zip")) == 1
    assert "no such pack" in capsys.readouterr().out
