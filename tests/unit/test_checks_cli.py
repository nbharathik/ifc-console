"""ifc-console check: exit codes and report formats."""

from __future__ import annotations

import json
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
