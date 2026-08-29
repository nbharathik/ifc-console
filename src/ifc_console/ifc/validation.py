"""Schema and IDS validation: the shared checks engine behind the
validate_model / validate_ids tools and the `check` CLI subcommand.

Schema validation uses the bundled ifcopenshell.validate (no new dependency).
IDS validation needs the optional ifctester package and degrades to a hint
naming the install command when it is absent.
"""

from __future__ import annotations

import contextlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from ifc_console.core.results import ToolError

MAX_ISSUES = 200

VALIDATION_EXTRA_HINT = (
    "Install the optional IDS engine with: pip install ifctester "
    "(or pip install 'ifc-console[validation]'), restart ifc-console, then retry."
)


def _issue(statement: Any) -> dict[str, Any]:
    """Normalize one json_logger statement into a plain, serializable dict."""
    if not isinstance(statement, dict):
        return {"severity": "error", "message": str(statement)}
    entry: dict[str, Any] = {
        "severity": str(statement.get("level", "error")).lower(),
        "message": str(statement.get("message", "")),
    }
    attribute = statement.get("attribute")
    if attribute:
        entry["attribute"] = str(attribute)
    instance = statement.get("instance")
    if instance is not None:
        try:
            entry["class"] = instance.is_a()
        except Exception:
            entry["class"] = type(instance).__name__
        with contextlib.suppress(Exception):
            entry["id"] = instance.id()
        try:
            entry["global_id"] = getattr(instance, "GlobalId", None)
        except Exception:
            entry["global_id"] = None
        entry["instance"] = str(instance)[:300]
    return entry


def _issue_class(statement: Any) -> str:
    """Just the grouping key, without serializing the instance."""
    if not isinstance(statement, dict):
        return "(file)"
    instance = statement.get("instance")
    if instance is None:
        return "(file)"
    try:
        return str(instance.is_a()) or "(file)"
    except Exception:
        return type(instance).__name__


def _issue_severity(statement: Any) -> str:
    if not isinstance(statement, dict):
        return "error"
    return str(statement.get("level", "error")).lower() or "error"


def run_schema_validation(
    ifc: Any, *, express_rules: bool = False, max_issues: int = MAX_ISSUES
) -> dict[str, Any]:
    """Validate against the IFC schema; group issues for triage."""
    from ifcopenshell import validate as _validate

    logger = _validate.json_logger()
    _validate.validate(ifc, logger, express_rules=express_rules)
    issues: list[dict[str, Any]] = []
    by_class: Counter[str] = Counter()
    by_severity: Counter[str] = Counter()
    for statement in logger.statements:
        # Past the cap only the counters matter, and serializing an instance
        # to build the full entry is the expensive part of this loop.
        if len(issues) < max_issues:
            entry = _issue(statement)
            issues.append(entry)
            by_class[entry.get("class") or "(file)"] += 1
            by_severity[entry.get("severity") or "error"] += 1
            continue
        by_class[_issue_class(statement)] += 1
        by_severity[_issue_severity(statement)] += 1
    total = len(logger.statements)
    return {
        "valid": total == 0,
        "issue_count": total,
        "returned": len(issues),
        "express_rules": express_rules,
        "by_class": dict(by_class.most_common()),
        "by_severity": dict(by_severity),
        "issues": issues,
    }


def _cap_entities(entities: Any, cap: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(entities, list):
        return out
    for item in entities[:cap]:
        if isinstance(item, dict):
            keep = {
                k: item[k]
                for k in ("global_id", "class", "name", "tag", "reason", "description")
                if item.get(k) not in (None, "")
            }
            out.append(keep or {"entity": str(item)[:120]})
        else:
            out.append({"entity": str(item)[:120]})
    return out


def run_ids_validation(
    ifc: Any, ids_path: Path, *, max_failures_per_spec: int = 50
) -> dict[str, Any]:
    """Check the model against a buildingSMART IDS file (optional ifctester)."""
    try:
        from ifctester import ids as ids_mod
        from ifctester import reporter
    except ImportError:
        raise ToolError(
            "EXTRA_NOT_INSTALLED",
            "IDS validation needs the optional ifctester package, which is not installed.",
            VALIDATION_EXTRA_HINT,
        ) from None
    try:
        spec_set = ids_mod.open(str(ids_path))
    except Exception as exc:
        raise ToolError(
            "INVALID_INPUT",
            f"could not read {ids_path.name} as an IDS file: {exc}",
            "Point ids_path at a buildingSMART IDS XML file.",
        ) from exc
    spec_set.validate(ifc)
    engine = reporter.Json(spec_set)
    engine.report()
    raw = json.loads(engine.to_string())

    specifications: list[dict[str, Any]] = []
    all_pass = True
    totals = {"specifications": 0, "passed": 0, "failed": 0}
    for spec in raw.get("specifications", []):
        status = bool(spec.get("status"))
        totals["specifications"] += 1
        totals["passed" if status else "failed"] += 1
        all_pass = all_pass and status
        requirements: list[dict[str, Any]] = []
        for req in spec.get("requirements", []):
            entry: dict[str, Any] = {
                "description": req.get("description") or req.get("label") or "",
                "status": bool(req.get("status")),
                "failed_count": len(req.get("failed_entities") or []),
            }
            failed = _cap_entities(req.get("failed_entities"), max_failures_per_spec)
            if failed:
                entry["failed"] = failed
            requirements.append(entry)
        specifications.append(
            {
                "name": spec.get("name", ""),
                "status": status,
                "applicable_count": spec.get("total_applicable", 0),
                "pass_count": spec.get("total_applicable_pass", 0),
                "fail_count": spec.get("total_applicable_fail", 0),
                "requirements": requirements,
            }
        )
    return {
        "passed": all_pass,
        "ids": {"title": (raw.get("info") or {}).get("title") or raw.get("title") or ids_path.name},
        "totals": totals,
        "specifications": specifications,
    }
