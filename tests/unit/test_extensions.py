"""The extension store: catalog, install record, install flow, scaffold."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ifc_console import extensions
from ifc_console.extensions.scaffold import generate
from ifc_console.mcp.envelope import ToolError

CATALOG = {
    "version": 1,
    "extensions": [
        {
            "name": "measure",
            "kind": "agent",
            "description": "measurement agent",
            "package": "ifc-agent-measure",
            "command": "ifc-measure",
        },
        {
            "name": "checks",
            "kind": "plugin",
            "description": "operation plugin",
            "package": "company-ifc-checks",
        },
    ],
}


@pytest.fixture
def catalog_file(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(CATALOG), encoding="utf-8")
    return path


class TestCatalog:
    def test_file_catalog_loads(self, catalog_file: Path):
        catalog, source = extensions.fetch_catalog(str(catalog_file))
        assert [e.name for e in catalog.extensions] == ["measure", "checks"]
        assert source == str(catalog_file)

    def test_bad_file_is_a_clear_error(self, tmp_path: Path):
        bad = tmp_path / "catalog.json"
        bad.write_text("not json", encoding="utf-8")
        with pytest.raises(ToolError) as excinfo:
            extensions.fetch_catalog(str(bad))
        assert excinfo.value.code == "INVALID_INPUT"

    def test_unreachable_url_falls_back_to_the_seed(self):
        catalog, source = extensions.fetch_catalog("http://127.0.0.1:1/catalog.json")
        assert any(e.name == "measure" for e in catalog.extensions)
        assert "seed" in source

    def test_search_matches_name_and_description(self, catalog_file: Path):
        hits, _ = extensions.search("measurement", catalog_url=str(catalog_file))
        assert [e.name for e in hits] == ["measure"]

    def test_resolve_by_name_and_by_requirement(self, catalog_file: Path):
        catalog, _ = extensions.fetch_catalog(str(catalog_file))
        assert extensions.resolve_entry(catalog, "measure").package == "ifc-agent-measure"
        direct = extensions.resolve_entry(catalog, "git+https://example.com/acme/ifc-agent-acme.git")
        assert direct.name == "acme"
        assert direct.package.startswith("git+")
        with pytest.raises(ToolError) as excinfo:
            extensions.resolve_entry(catalog, "nope")
        assert excinfo.value.code == "NOT_FOUND"
        assert "measure" in excinfo.value.hint


class TestInstall:
    def test_install_records_and_names_the_command(
        self, tmp_path: Path, catalog_file: Path, monkeypatch
    ):
        monkeypatch.setattr(extensions, "_run_uv_tool", lambda args: (True, "ok"))
        record = extensions.install(tmp_path, "measure", catalog_url=str(catalog_file))
        assert record["command"] == "ifc-measure"
        installed = extensions.InstallRecord(tmp_path).load()
        assert installed["measure"]["package"] == "ifc-agent-measure"

    def test_failed_uv_run_surfaces_the_output(
        self, tmp_path: Path, catalog_file: Path, monkeypatch
    ):
        monkeypatch.setattr(extensions, "_run_uv_tool", lambda args: (False, "boom\nlast line"))
        with pytest.raises(ToolError) as excinfo:
            extensions.install(tmp_path, "measure", catalog_url=str(catalog_file))
        assert excinfo.value.code == "EXEC_ERROR"
        assert "last line" in excinfo.value.hint

    def test_plugins_are_not_tool_installed(self, tmp_path: Path, catalog_file: Path):
        with pytest.raises(ToolError) as excinfo:
            extensions.install(tmp_path, "checks", catalog_url=str(catalog_file))
        assert excinfo.value.code == "INVALID_INPUT"
        assert "plugins" in excinfo.value.hint

    def test_uninstall_needs_a_record(self, tmp_path: Path):
        with pytest.raises(ToolError) as excinfo:
            extensions.uninstall(tmp_path, "measure")
        assert excinfo.value.code == "NOT_FOUND"

    def test_uninstall_round_trip(self, tmp_path: Path, catalog_file: Path, monkeypatch):
        monkeypatch.setattr(extensions, "_run_uv_tool", lambda args: (True, "ok"))
        extensions.install(tmp_path, "measure", catalog_url=str(catalog_file))
        removed = extensions.uninstall(tmp_path, "measure")
        assert removed["package"] == "ifc-agent-measure"
        assert extensions.InstallRecord(tmp_path).load() == {}


class TestScaffold:
    def test_generates_a_complete_compilable_project(self, tmp_path: Path):
        files = generate(tmp_path, "acme-measure")
        root = tmp_path / "ifc-agent-acme-measure"
        assert (root / "pyproject.toml").is_file()
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        assert 'name = "ifc-agent-acme-measure"' in pyproject
        assert 'ifc-acme-measure = "ifc_agent_acme_measure.__main__:main"' in pyproject
        for path in files:
            if path.suffix == ".py":
                compile(path.read_text(encoding="utf-8"), str(path), "exec")
        agent_code = (root / "src" / "ifc_agent_acme_measure" / "agent.py").read_text(
            encoding="utf-8"
        )
        assert 'name="acme-measure"' in agent_code
        assert "@EXT_" not in agent_code

    def test_prefixes_are_normalized(self, tmp_path: Path):
        generate(tmp_path, "ifc-agent-takeoff")
        assert (tmp_path / "ifc-agent-takeoff").is_dir()

    def test_existing_directory_is_refused(self, tmp_path: Path):
        generate(tmp_path, "acme")
        with pytest.raises(ToolError) as excinfo:
            generate(tmp_path, "acme")
        assert excinfo.value.code == "FILE_EXISTS"

    def test_bad_name_is_a_clear_error(self, tmp_path: Path):
        with pytest.raises(ToolError) as excinfo:
            generate(tmp_path, "Not A Name!")
        assert excinfo.value.code == "INVALID_INPUT"
