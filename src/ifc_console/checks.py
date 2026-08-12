"""The `check` subcommand engine: one-shot validation with CI-friendly output.

Runs outside any session: open the file, run schema validation and any IDS
checks, and render text, JSON, SARIF 2.1.0, or JUnit XML. Exit code 5 means
the model failed a check; the other CLI codes keep their meanings.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.sax.saxutils import quoteattr

from ifc_console import __version__

FORMATS = ("text", "json", "sarif", "junit")

_REPO_URL = "https://github.com/nbharathik/ifc-console"


def run_check(
    model_path: Path,
    *,
    ids_paths: list[Path] | None = None,
    express_rules: bool = False,
    max_issues: int = 200,
    progress: Callable[[str, int, str], None] | None = None,
) -> dict[str, Any]:
    """Open the file directly (no session, no worker) and run every check."""
    import ifcopenshell

    from ifc_console.ifc.validation import run_ids_validation, run_schema_validation

    notify = progress or (lambda _phase, _amount, _message: None)
    notify("open", 10, "opening IFC model")
    ifc = ifcopenshell.open(str(model_path))
    notify("schema", 25, "validating IFC schema")
    checks: dict[str, Any] = {
        "schema": run_schema_validation(ifc, express_rules=express_rules, max_issues=max_issues)
    }
    notify("schema", 60, "schema validation completed")
    ids_reports: list[dict[str, Any]] = []
    requirements = ids_paths or []
    for index, ids_path in enumerate(requirements):
        amount = 60 + int(30 * index / max(1, len(requirements)))
        notify("ids", amount, f"validating {ids_path.name}")
        entry = run_ids_validation(ifc, ids_path)
        entry["path"] = str(ids_path)
        ids_reports.append(entry)
    if ids_reports:
        checks["ids"] = ids_reports
    notify("report", 90, "building validation report")
    return {
        "model": str(model_path),
        "schema": getattr(ifc, "schema", None),
        "checks": checks,
        "passed": checks["schema"]["valid"] and all(r["passed"] for r in ids_reports),
    }


def _findings(report: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten the report into (rule, level, message, target) findings."""
    out: list[dict[str, str]] = []
    for issue in report["checks"]["schema"]["issues"]:
        target = issue.get("global_id") or issue.get("instance") or "(file)"
        out.append(
            {
                "rule": "schema",
                "level": "error" if issue.get("severity") != "warning" else "warning",
                "message": issue.get("message", ""),
                "target": str(target),
            }
        )
    for ids_report in report["checks"].get("ids", []):
        for spec in ids_report["specifications"]:
            if spec["status"]:
                continue
            for req in spec["requirements"]:
                if req["status"]:
                    continue
                failed = req.get("failed") or [{}]
                for item in failed:
                    target = item.get("global_id") or item.get("name") or "(unresolved)"
                    reason = item.get("reason") or "requirement failed"
                    out.append(
                        {
                            "rule": f"ids:{spec['name']}",
                            "level": "error",
                            "message": f"{req['description']}: {reason}",
                            "target": str(target),
                        }
                    )
    return out


def render(report: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, ensure_ascii=False)
    if fmt == "sarif":
        return _render_sarif(report)
    if fmt == "junit":
        return _render_junit(report)
    return _render_text(report)


def _render_text(report: dict[str, Any]) -> str:
    lines = [f"{Path(report['model']).name} ({report.get('schema') or '?'})"]
    schema = report["checks"]["schema"]
    if schema["valid"]:
        lines.append("schema: OK")
    else:
        worst = ", ".join(f"{c} x{n}" for c, n in list(schema["by_class"].items())[:5])
        lines.append(f"schema: FAIL ({schema['issue_count']} issues; {worst})")
        for issue in schema["issues"][:10]:
            where = issue.get("global_id") or issue.get("class") or "(file)"
            lines.append(f"  - {where}: {issue.get('message', '')[:160]}")
    for ids_report in report["checks"].get("ids", []):
        verdict = "OK" if ids_report["passed"] else "FAIL"
        totals = ids_report["totals"]
        lines.append(
            f"ids {Path(ids_report['path']).name}: {verdict} "
            f"({totals['passed']}/{totals['specifications']} specifications pass)"
        )
        for spec in ids_report["specifications"]:
            if spec["status"]:
                continue
            lines.append(
                f"  - {spec['name']}: {spec['fail_count']} of "
                f"{spec['applicable_count']} applicable elements fail"
            )
            for req in spec["requirements"]:
                for item in (req.get("failed") or [])[:5]:
                    name = item.get("name") or item.get("global_id") or "?"
                    lines.append(f"      {name}: {item.get('reason', '')}")
    lines.append(f"result: {'PASS' if report['passed'] else 'FAIL'}")
    return "\n".join(lines)


def _render_sarif(report: dict[str, Any]) -> str:
    results = [
        {
            "ruleId": finding["rule"],
            "level": finding["level"],
            "message": {"text": finding["message"]},
            "locations": [
                {
                    "physicalLocation": {"artifactLocation": {"uri": Path(report["model"]).name}},
                    "logicalLocations": [{"name": finding["target"]}],
                }
            ],
        }
        for finding in _findings(report)
    ]
    doc = {
        "$schema": (
            "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/"
            "sarif-schema-2.1.0.json"
        ),
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "ifc-console",
                        "informationUri": _REPO_URL,
                        "version": __version__,
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2)


def _render_junit(report: dict[str, Any]) -> str:
    findings = _findings(report)
    tests = max(len(findings), 1)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<testsuites tests="{tests}" failures="{len(findings)}">',
        f'<testsuite name="ifc-console check" tests="{tests}" failures="{len(findings)}">',
    ]
    if not findings:
        lines.append(
            f'<testcase classname="ifc-console.check" name={quoteattr(Path(report["model"]).name)} />'
        )
    for finding in findings:
        classname = quoteattr(f"ifc-console.{finding['rule']}")
        lines.append(f"<testcase classname={classname} name={quoteattr(finding['target'])}>")
        lines.append(f"<failure message={quoteattr(finding['message'])} />")
        lines.append("</testcase>")
    lines.append("</testsuite>")
    lines.append("</testsuites>")
    return "\n".join(lines)
