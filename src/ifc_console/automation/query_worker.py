"""Subprocess entry point for revision-bound, streaming IFC query jobs."""

from __future__ import annotations

import csv
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import ifcopenshell
import ifcopenshell.util.selector as selector_util

from ifc_console.automation.files import source_matches
from ifc_console.core.jobs import QueryJobSpec
from ifc_console.core.results import ToolError
from ifc_console.ifc.query import element_row
from ifc_console.sandbox.policy import SandboxPolicy


def _emit(type_: str, **payload: Any) -> None:
    print(json.dumps({"type": type_, **payload}, ensure_ascii=False), flush=True)


def _verify(spec: QueryJobSpec) -> None:
    if not source_matches(spec.model):
        raise ToolError(
            "SOURCE_CHANGED",
            f"{Path(spec.model.path).name} changed after the query was submitted.",
            "Submit a new job against the current model revision.",
        )


def _atomic_metadata(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def run(
    spec: QueryJobSpec,
    output_path: Path,
    metadata_path: Path,
    policy: SandboxPolicy | None = None,
) -> None:
    if policy is not None:
        from ifc_console.sandbox import hooks, limits

        applied = limits.apply_self_limits(policy.memory_mb)
        controls = hooks.install(policy)
        _emit("worker_ready", controls=[*applied, *controls])

    _emit("progress", progress=5, message="verifying query input")
    _verify(spec)
    model = ifcopenshell.open(spec.model.path)
    try:
        elements = list(selector_util.filter_elements(model, spec.query))
    except Exception as exc:
        raise ToolError(
            "INVALID_QUERY",
            f"selector parse/evaluation failed: {exc}",
            "Fix the IfcOpenShell selector and submit a new query batch.",
        ) from exc
    if spec.order_by == "name":
        elements.sort(key=lambda item: (getattr(item, "Name", None) or "", item.id()))
    elif spec.order_by == "storey":
        elements.sort(
            key=lambda item: (
                element_row(item, ("storey",)).get("storey") or "",
                item.id(),
            )
        )
    else:
        elements.sort(
            key=lambda item: (item.is_a(), getattr(item, "Name", None) or "", item.id())
        )
    matched = len(elements)
    selected = elements[: spec.limit]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = output_path.with_name(f".{output_path.name}.{secrets.token_hex(6)}.tmp")
    headers = ("global_id", "class", *spec.fields)
    step = max(1, len(selected) // 20)
    try:
        with temp.open("x", encoding="utf-8", newline="") as handle:
            if spec.output_format == "csv":
                writer = csv.DictWriter(handle, fieldnames=headers)
                writer.writeheader()
                for index, element in enumerate(selected, start=1):
                    writer.writerow(element_row(element, spec.fields))
                    if index % step == 0:
                        _emit(
                            "progress",
                            progress=min(90, 10 + int(index * 80 / max(1, len(selected)))),
                            message=f"streamed {index} query row(s)",
                        )
            else:
                for index, element in enumerate(selected, start=1):
                    handle.write(
                        json.dumps(
                            element_row(element, spec.fields),
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    if index % step == 0:
                        _emit(
                            "progress",
                            progress=min(90, 10 + int(index * 80 / max(1, len(selected)))),
                            message=f"streamed {index} query row(s)",
                        )
            handle.flush()
            os.fsync(handle.fileno())
        _verify(spec)
        os.replace(temp, output_path)
    finally:
        if temp.exists():
            temp.unlink()
    metadata = {
        "schema": model.schema,
        "matched": matched,
        "row_count": len(selected),
        "truncated": matched > len(selected),
        "format": spec.output_format,
        "columns": list(headers),
    }
    _atomic_metadata(metadata_path, metadata)
    _emit("result", progress=100, message="query completed", **metadata)


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1:
        _emit("error", code="INVALID_INPUT", message="expected one worker input file")
        return 2
    try:
        payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
        spec = QueryJobSpec.model_validate(payload["spec"])
        policy = SandboxPolicy.from_dict(payload.get("policy") or {})
        run(
            spec,
            Path(payload["output_path"]),
            Path(payload["metadata_path"]),
            policy,
        )
        return 0
    except ToolError as exc:
        _emit("error", code=exc.code, message=exc.message, hint=exc.hint)
        return 1
    except Exception as exc:
        _emit(
            "error",
            code="JOB_WORKER_FAILED",
            message=f"{type(exc).__name__}: {exc}",
            hint="Inspect the query job failure and correct its input.",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
