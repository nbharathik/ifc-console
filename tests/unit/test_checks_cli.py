"""ifc-console check: exit codes and report formats."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

from ifc_console.cli import main

FIXTURES = Path(__file__).parent.parent / "fixtures"
MODEL = FIXTURES / "generated" / "minimal_ifc4.ifc"
IDS = FIXTURES / "wall_firerating.ids"


def test_check_passes_on_valid_model(capsys):
    assert main(["check", str(MODEL)]) == 0
    out = capsys.readouterr().out
    assert "schema: OK" in out
    assert "result: PASS" in out


def test_check_ids_failure_exits_5(capsys):
    assert main(["check", str(MODEL), "--ids", str(IDS)]) == 5
    out = capsys.readouterr().out
    assert "result: FAIL" in out
    assert "Walls carry a fire rating" in out


def test_check_json_format(capsys):
    assert main(["check", str(MODEL), "--ids", str(IDS), "--format", "json"]) == 5
    report = json.loads(capsys.readouterr().out)
    assert report["passed"] is False
    assert report["checks"]["schema"]["valid"] is True
    assert report["checks"]["ids"][0]["totals"]["failed"] == 1


def test_check_sarif_format(capsys):
    assert main(["check", str(MODEL), "--ids", str(IDS), "--format", "sarif"]) == 5
    doc = json.loads(capsys.readouterr().out)
    assert doc["version"] == "2.1.0"
    results = doc["runs"][0]["results"]
    assert len(results) == 2
    assert all(r["ruleId"].startswith("ids:") for r in results)
    assert doc["runs"][0]["tool"]["driver"]["name"] == "ifc-console"


def test_check_junit_format_and_output_file(tmp_path, capsys):
    target = tmp_path / "report.xml"
    code = main(
        [
            "check",
            str(MODEL),
            "--ids",
            str(IDS),
            "--format",
            "junit",
            "--output",
            str(target),
        ]
    )
    assert code == 5
    root = ET.fromstring(target.read_text(encoding="utf-8"))
    assert root.tag == "testsuites"
    assert root.attrib["failures"] == "2"
    assert "wrote junit report" in capsys.readouterr().out


def test_check_missing_file_exits_4(capsys):
    assert main(["check", "no-such-file.ifc"]) == 4


def test_check_unparseable_exits_4(capsys):
    assert main(["check", str(FIXTURES / "generated" / "broken.ifc")]) == 4


def test_durable_validation_job_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    output = tmp_path / "reports"
    assert (
        main(
            [
                "jobs",
                "validate",
                str(MODEL),
                "--json",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == "succeeded"
    assert len(record["artifacts"]) == 2
    assert {path.suffix for path in output.iterdir()} == {".json", ".sarif"}

    assert main(["jobs", "show", record["job_id"], "--json"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["job_id"] == record["job_id"]

    artifact_id = record["artifacts"][0]["artifact_id"]
    assert main(["artifacts", "show", artifact_id, "--json"]) == 0
    artifact = json.loads(capsys.readouterr().out)
    assert artifact["artifact_id"] == artifact_id

    assert main(["artifacts", "pin", artifact_id]) == 0
    assert "pinned" in capsys.readouterr().out
    assert main(["artifacts", "gc", "--older-than-days", "1", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["candidate_count"] == 0
    assert main(["artifacts", "unpin", artifact_id]) == 0
    assert "unpinned" in capsys.readouterr().out


def test_safe_property_change_cli_roundtrip(tmp_path, monkeypatch, capsys):
    import ifcopenshell

    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model = tmp_path / "model.ifc"
    shutil.copy2(MODEL, model)
    opened = ifcopenshell.open(str(model))
    wall = opened.by_type("IfcWall")[0]
    global_id = wall.GlobalId
    property_id = next(
        prop.id()
        for relation in wall.IsDefinedBy
        if relation.RelatingPropertyDefinition.is_a("IfcPropertySet")
        and relation.RelatingPropertyDefinition.Name == "Pset_WallCommon"
        for prop in relation.RelatingPropertyDefinition.HasProperties
        if prop.Name == "FireRating"
    )

    assert (
        main(
            [
                "changes",
                "preview",
                str(model),
                "--global-id",
                global_id,
                "--pset",
                "Pset_WallCommon",
                "--property",
                "FireRating",
                "--value",
                "F60",
                "--json",
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    change_set_id = preview["change_set_id"]

    assert (
        main(
            [
                "changes",
                "approve",
                change_set_id,
                "--by",
                "cli-test",
                "--json",
            ]
        )
        == 0
    )
    approval = json.loads(capsys.readouterr().out)

    assert (
        main(
            [
                "changes",
                "commit",
                str(model),
                change_set_id,
                "--approval",
                approval["approval_id"],
                "--json",
            ]
        )
        == 0
    )
    commit = json.loads(capsys.readouterr().out)
    assert ifcopenshell.open(str(model)).by_id(property_id).NominalValue.wrappedValue == "F60"

    assert main(["changes", "receipt", commit["commit_id"], "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["commit_id"] == commit["commit_id"]

    assert (
        main(
            [
                "changes",
                "restore",
                str(model),
                commit["commit_id"],
                "--confirm",
                "--json",
            ]
        )
        == 0
    )
    restored = json.loads(capsys.readouterr().out)
    assert restored["result"]["restored_sha256"] == commit["result"]["previous_sha256"]
    assert ifcopenshell.open(str(model)).by_id(property_id).NominalValue.wrappedValue == "F30"
