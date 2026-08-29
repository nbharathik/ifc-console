"""ifc-console check: exit codes and report formats."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

import ifc_console.cli as cli_module
from ifc_console.cli import build_parser, main

FIXTURES = Path(__file__).parent.parent / "fixtures"
MODEL = FIXTURES / "generated" / "minimal_ifc4.ifc"
IDS = FIXTURES / "wall_firerating.ids"


def test_transaction_job_and_journal_commands_are_registered() -> None:
    parser = build_parser()
    commit = parser.parse_args(
        [
            "jobs",
            "commit",
            "model.ifc",
            "sha256:" + "a" * 64,
            "--approval",
            "sha256:" + "b" * 64,
        ]
    )
    journal = parser.parse_args(["transactions", "show", "txn-0123456789abcdef"])

    assert commit.func.__name__ == "_cmd_jobs_commit"
    assert journal.func.__name__ == "_cmd_transactions_show"


def test_serve_run_flags_work_before_or_after_the_subcommand() -> None:
    parser = build_parser()
    flags = [
        "--file",
        "model.ifc",
        "--mode",
        "edit",
        "--port",
        "9000",
        "--viewer",
        "--agent",
        "--allow-dir",
        "models",
        "--log-level",
        "debug",
    ]

    before = parser.parse_args([*flags, "serve", "--stdio"])
    after = parser.parse_args(["serve", "--stdio", *flags])

    for name in ("file", "mode", "port", "viewer", "agent", "allow_dir", "log_level"):
        assert getattr(before, name) == getattr(after, name)


@pytest.mark.parametrize(
    ("argv", "runner_name", "transport"),
    [
        (["--file", "broken.ifc", "--no-tui"], "_run_headless_http", "http"),
        (["serve", "--stdio", "--file", "broken.ifc"], "_cmd_serve", "stdio"),
    ],
)
def test_server_startup_model_failure_closes_core(
    argv, runner_name, transport, monkeypatch
) -> None:
    from ifc_console import preload

    events: list[str] = []
    transports: list[str] = []
    core = SimpleNamespace(
        start_audit=lambda: events.append("audit"),
        start_knowledge=lambda: events.append("knowledge"),
        shutdown=lambda: events.append("shutdown"),
    )
    store = SimpleNamespace(settings=SimpleNamespace(logging=SimpleNamespace(level="info")))

    def make_core(_args, _store, *, transport: str):
        transports.append(transport)
        return core

    monkeypatch.setattr(preload, "start", lambda: None)
    monkeypatch.setattr(preload, "release", lambda: None)
    monkeypatch.setattr(cli_module, "_make_store", lambda _args: store)
    monkeypatch.setattr(cli_module, "_setup_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "_make_core", make_core)
    monkeypatch.setattr(cli_module, "_load_model_blocking", lambda _core, _path: 4)

    args = build_parser().parse_args(argv)
    assert getattr(cli_module, runner_name)(args) == 4
    assert transports == [transport]
    assert events == ["audit", "knowledge", "shutdown"]


def test_batch_commands_are_registered() -> None:
    parser = build_parser()
    validate = parser.parse_args(["batch", "validate", "one.ifc", "two.ifc", "--concurrency", "2"])
    resume = parser.parse_args(["batch", "resume", "batch-0123456789abcdef"])
    query = parser.parse_args(
        ["batch", "query", "one.ifc", "--selector", "IfcWall", "--format", "csv"]
    )

    assert validate.func.__name__ == "_cmd_batch_validate"
    assert validate.models == ["one.ifc", "two.ifc"]
    assert resume.func.__name__ == "_cmd_batch_resume"
    assert query.func.__name__ == "_cmd_batch_query"


def test_workflow_commands_are_registered() -> None:
    parser = build_parser()
    run = parser.parse_args(["run", "workflow.yaml", "--plan"])
    watch = parser.parse_args(["workflows", "watch", "workflow-0123456789abcdef"])
    resume = parser.parse_args(["workflows", "resume", "workflow-0123456789abcdef"])
    schema = parser.parse_args(["workflows", "schema"])

    assert run.func.__name__ == "_cmd_workflow_run"
    assert run.plan is True
    assert watch.func.__name__ == "_cmd_workflows_watch"
    assert resume.func.__name__ == "_cmd_workflows_resume"
    assert schema.func.__name__ == "_cmd_workflows_schema"


def test_workflow_schema_command_prints_the_versioned_contract(capsys) -> None:
    assert main(["workflows", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["title"] == "WorkflowSpec"
    assert schema["properties"]["version"]["const"] == "1"
    assert set(schema["properties"]) >= {"name", "inputs", "steps"}


def test_structured_creation_and_classification_cli_options_are_registered() -> None:
    parser = build_parser()
    create = parser.parse_args(
        [
            "changes",
            "preview",
            "model.ifc",
            "--global-id",
            "gid",
            "--pset",
            "Company_QA",
            "--property",
            "Status",
            "--value",
            "Checked",
            "--create-missing",
            "--nominal-type",
            "IfcLabel",
        ]
    )
    classify = parser.parse_args(
        [
            "changes",
            "classify",
            "model.ifc",
            "--global-id",
            "gid",
            "--system",
            "Company Classification",
            "--identification",
            "WALL-EXT",
            "--name",
            "External wall",
        ]
    )

    assert create.create_missing is True
    assert create.nominal_type == "IfcLabel"
    assert classify.func.__name__ == "_cmd_changes_classify"


def test_sessions_verify_reports_integrity(tmp_path, monkeypatch, capsys):
    from ifc_console.audit import AuditLog

    home = tmp_path / "home"
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(home))
    audit = AuditLog(home / "sessions")
    session_id = audit.start({"interface": "test"})
    audit.record("checked")

    assert main(["sessions", "verify", session_id, "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "session_id": session_id,
        "valid": True,
        "event_count": 2,
        "error": None,
    }


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


def test_durable_validation_batch_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    first = tmp_path / "first.ifc"
    second = tmp_path / "second.ifc"
    shutil.copy2(MODEL, first)
    shutil.copy2(MODEL, second)
    output = tmp_path / "batch-reports"

    assert (
        main(
            [
                "batch",
                "validate",
                str(first),
                str(second),
                "--concurrency",
                "1",
                "--json",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == "succeeded"
    assert len(record["children"]) == 2
    assert record["aggregate_artifact"]["kind"] == "batch-manifest"
    assert len(list(output.iterdir())) == 5

    assert main(["batch", "show", record["batch_id"], "--json"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["batch_id"] == record["batch_id"]

    query_output = tmp_path / "query-results"
    assert (
        main(
            [
                "batch",
                "query",
                str(first),
                str(second),
                "--selector",
                "IfcWall",
                "--format",
                "csv",
                "--limit",
                "2",
                "--json",
                "--output-dir",
                str(query_output),
            ]
        )
        == 0
    )
    query = json.loads(capsys.readouterr().out)
    assert query["state"] == "succeeded"
    assert query["spec"]["operation"]["kind"] == "query"
    assert all(child["summary"]["row_count"] == 2 for child in query["children"])
    assert len(list(query_output.iterdir())) == 3


def test_workflow_cli_plans_executes_exports_and_restores(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(home))
    model = tmp_path / "model.ifc"
    shutil.copy2(MODEL, model)
    manifest = tmp_path / "workflow.yaml"
    manifest.write_text(
        """version: '1'
name: cli-gate
inputs:
  - id: models
    paths: ['*.ifc']
steps:
  - id: validate
    operation:
      kind: validation
      version: '1'
  - id: walls
    needs: [validate]
    operation:
      kind: query
      version: '1'
      query: IfcWall
      output_format: csv
      limit: 2
""",
        encoding="utf-8",
    )

    assert main(["run", str(manifest), "--plan", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["plan_id"].startswith("sha256:")
    assert plan["total_children"] == 2
    assert not list((home / "workflows" / "records").glob("workflow-*.json"))

    output = tmp_path / "workflow-output"
    assert (
        main(
            [
                "run",
                str(manifest),
                "--json",
                "--output-dir",
                str(output),
            ]
        )
        == 0
    )
    record = json.loads(capsys.readouterr().out)
    assert record["state"] == "succeeded"
    assert record["summary"]["passed"] is True
    assert len(record["steps"]) == 2
    assert len(list(output.iterdir())) == 6

    assert main(["workflows", "show", record["workflow_id"], "--json"]) == 0
    restored = json.loads(capsys.readouterr().out)
    assert restored["workflow_id"] == record["workflow_id"]
    assert main(["workflows", "list", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["workflow_id"] == record["workflow_id"]


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

    assert main(["jobs", "list", "--json"]) == 0
    jobs = json.loads(capsys.readouterr().out)
    assert {record["kind"] for record in jobs} >= {"commit", "restore"}
    assert main(["transactions", "list", "--json"]) == 0
    journals = json.loads(capsys.readouterr().out)
    assert {journal["kind"] for journal in journals} >= {"commit", "restore"}
    assert all(journal["phase"] == "receipt_persisted" for journal in journals)
