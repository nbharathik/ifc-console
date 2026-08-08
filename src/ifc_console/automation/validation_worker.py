"""Subprocess entry point for revision-bound IFC validation jobs."""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

from ifc_console.automation.files import source_matches
from ifc_console.checks import run_check
from ifc_console.core.jobs import ValidationJobSpec
from ifc_console.core.results import ToolError
from ifc_console.sandbox.policy import SandboxPolicy


def _emit(type_: str, **payload: Any) -> None:
    print(json.dumps({"type": type_, **payload}, ensure_ascii=False), flush=True)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _verify(source: Any) -> None:
    if not source_matches(source):
        raise ToolError(
            "SOURCE_CHANGED",
            f"{Path(source.path).name} changed after the job was submitted.",
            "Submit a new job against the current model revision.",
        )


def run(spec: ValidationJobSpec, output_path: Path, policy: SandboxPolicy | None = None) -> None:
    if policy is not None:
        from ifc_console.sandbox import hooks, limits

        applied = limits.apply_self_limits(policy.memory_mb)
        import ifcopenshell  # noqa: F401

        if spec.ids_files:
            import ifctester  # noqa: F401
        controls = hooks.install(policy)
        _emit("worker_ready", controls=[*applied, *controls])
    _emit("progress", progress=5, message="verifying input files")
    _verify(spec.model)
    for source in spec.ids_files:
        _verify(source)

    def progress(phase: str, amount: int, message: str) -> None:
        _emit("progress", phase=phase, progress=amount, message=message)

    report = run_check(
        Path(spec.model.path),
        ids_paths=[Path(source.path) for source in spec.ids_files],
        express_rules=spec.express_rules,
        max_issues=spec.max_issues,
        progress=progress,
    )
    _emit("progress", progress=95, message="verifying validated revision")
    _verify(spec.model)
    for source in spec.ids_files:
        _verify(source)
    _write_report(output_path, report)
    _emit(
        "result",
        progress=100,
        message="validation completed",
        passed=bool(report["passed"]),
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1:
        _emit("error", code="INVALID_INPUT", message="expected one worker input file")
        return 2
    try:
        payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        spec = ValidationJobSpec.model_validate(payload["spec"])
        policy = SandboxPolicy.from_dict(payload.get("policy") or {})
        run(spec, Path(payload["output_path"]), policy)
        return 0
    except ToolError as exc:
        _emit("error", code=exc.code, message=exc.message, hint=exc.hint)
        return 1
    except Exception as exc:
        _emit(
            "error",
            code="JOB_WORKER_FAILED",
            message=f"{type(exc).__name__}: {exc}",
            hint="Inspect the job failure and retry after correcting the input.",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
