"""Analysis tools: schema validation, IDS checking, quantity takeoff,
georeferencing, and CSV export. Everything here is ask-mode safe: reads never
change anything, and the CSV export writes an output file, never the model."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ifc_console.ifc.quantities import AGGREGATE_BY, build_georeferencing, compute_quantities
from ifc_console.ifc.query import ALLOWED_FIELDS, DEFAULT_FIELDS, element_row
from ifc_console.ifc.validation import run_ids_validation, run_schema_validation
from ifc_console.mcp.compat import MCPServer, ToolAnnotations
from ifc_console.mcp.envelope import Envelope, ToolError, ok
from ifc_console.mcp.server import enveloped
from ifc_console.mcp.tools_query import _validate_subset
from ifc_console.policy.modes import OpClass, Verdict

if TYPE_CHECKING:
    from ifc_console.app import AppCore

ANALYSIS_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
ARTIFACT_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

# Express rules and IDS walk every instance: give them the load-scale budget
# (a timeout poisons the worker, so err generous).
_HEAVY_TIMEOUT = 600.0


def register(mcp: MCPServer, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Validate the loaded model against its IFC schema: attribute "
            "types, cardinality, enumerations. express_rules=true adds the EXPRESS "
            "where-rules (much slower on big models). Returns pass/fail, issue "
            "counts by class and severity, and the first max_issues issues."
        ),
    )
    @enveloped(core, "validate_model")
    async def validate_model(
        express_rules: bool = False,
        max_issues: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> Envelope:
        core.session.require_loaded()
        report, cached = await core.cached_read(
            "validate_model",
            lambda: run_schema_validation(
                core.session.ifc, express_rules=express_rules, max_issues=max_issues
            ),
            key=(express_rules, max_issues),
            timeout=_HEAVY_TIMEOUT if express_rules else 120,
        )
        return ok(report, core.session_meta(), char_limit=limit_, cached=cached)

    @mcp.tool(
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Check the loaded model against a buildingSMART IDS "
            "(Information Delivery Specification) file: per-specification "
            "pass/fail, failing elements with GlobalIds and reasons. Needs the "
            "optional ifctester package; the error hint explains the install."
        ),
    )
    @enveloped(core, "validate_ids")
    async def validate_ids(
        ids_path: Annotated[str, Field(description="Path to an IDS XML file.")],
        max_failures: Annotated[
            int, Field(ge=1, le=500, description="Failing elements kept per requirement.")
        ] = 50,
    ) -> Envelope:
        core.session.require_loaded()
        path = core.require_path_allowed(Path(ids_path))
        if not path.is_file():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{path} does not exist.",
                "Pass the path of a buildingSMART IDS XML file.",
            )
        report, cached = await core.cached_read(
            "validate_ids",
            lambda: run_ids_validation(
                core.session.ifc, path, max_failures_per_spec=max_failures
            ),
            key=(str(path), path.stat().st_mtime_ns, max_failures),
            timeout=_HEAVY_TIMEOUT,
        )
        return ok(report, core.session_meta(), char_limit=limit_, cached=cached)

    @mcp.tool(
        name="compute_quantities",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Quantity takeoff for a selector-defined set from stored "
            "quantity sets (Qto_*): per-group sums and grand totals with the "
            "model's units. aggregate_by groups per class, type, storey, "
            "material, or none. Optionally restrict to named quantities, e.g. "
            "['NetVolume', 'GrossArea']."
        ),
    )
    @enveloped(core, "compute_quantities")
    async def compute_quantities_tool(
        selector: Annotated[
            str, Field(description="IfcOpenShell selector, e.g. `IfcWall` or `IfcSlab`.")
        ],
        aggregate_by: Literal["class", "type", "storey", "material", "none"] = "class",
        quantities: Annotated[
            list[str] | None,
            Field(description="Quantity names to include; omit for all numeric ones."),
        ] = None,
    ) -> Envelope:
        core.session.require_loaded()
        if aggregate_by not in AGGREGATE_BY:
            raise ToolError(
                "INVALID_INPUT",
                f"unknown aggregate_by {aggregate_by!r}",
                f"Allowed: {list(AGGREGATE_BY)}",
            )
        wanted = tuple(quantities) if quantities else None
        report, cached = await core.cached_read(
            "compute_quantities",
            lambda: compute_quantities(
                core.session.ifc,
                selector,
                aggregate_by=aggregate_by,
                quantities=wanted,
            ),
            key=(selector, aggregate_by, wanted),
        )
        return ok(report, core.session_meta(), char_limit=limit_, cached=cached)

    @mcp.tool(
        annotations=ARTIFACT_ANN,
        description=(
            "[ARTIFACT] Export a selector query as a CSV file on disk. Allowed in "
            "ask mode: writing a report file is not editing the model (the write "
            "is allowed-dir checked and audited). Columns: global_id, class, the "
            "requested fields, plus dotted 'Pset_Name.Property' columns."
        ),
    )
    @enveloped(core, "export_csv")
    async def export_csv(
        selector: Annotated[str, Field(description="IfcOpenShell selector, e.g. `IfcWall`.")],
        path: Annotated[str, Field(description="Target file path; must end in .csv.")],
        fields: Annotated[
            list[str] | None,
            Field(description=f"Row fields beyond global_id+class. Allowed: {list(ALLOWED_FIELDS)}."),
        ] = None,
        properties: Annotated[
            list[str] | None,
            Field(max_length=20, description="Dotted pset columns, e.g. ['Pset_WallCommon.FireRating']."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
        overwrite: bool = False,
    ) -> Envelope:
        core.session.require_loaded()
        if core.policy.decide(OpClass.ARTIFACT) is not Verdict.ALLOW:
            raise ToolError(
                "ASK_MODE_BLOCKED",
                "artifact writes are disabled.",
                "Ask the user to run /mode edit in the ifc-console terminal.",
            )
        target = core.require_path_allowed(Path(path))
        if target.suffix.lower() != ".csv":
            raise ToolError(
                "INVALID_INPUT",
                f"{target.name} does not end in .csv.",
                "Pass a path ending in .csv.",
            )
        if target.exists() and not overwrite:
            raise ToolError(
                "FILE_EXISTS",
                f"{target} already exists.",
                "Pass overwrite=true to replace it, or pick another path.",
            )
        _validate_subset(fields, ALLOWED_FIELDS, "fields")
        use_fields = tuple(fields) if fields else DEFAULT_FIELDS
        props = list(dict.fromkeys(properties or []))

        def job() -> tuple[list[dict], int]:
            import ifcopenshell.util.element as element_util
            import ifcopenshell.util.selector as selector_util

            try:
                elements = list(selector_util.filter_elements(core.session.ifc, selector))
            except Exception as exc:
                raise ToolError(
                    "INVALID_QUERY",
                    f"selector failed: {exc}",
                    "See query_elements for selector examples.",
                ) from exc
            total = len(elements)
            rows = []
            for element in elements[:limit]:
                row = element_row(element, use_fields)
                if props:
                    try:
                        all_psets = element_util.get_psets(element)
                    except Exception:
                        all_psets = {}
                    for dotted in props:
                        pset, _, prop = dotted.partition(".")
                        row[dotted] = all_psets.get(pset, {}).get(prop) if prop else None
                rows.append(row)
            return rows, total

        rows, total = await core.session.run(job, timeout=300)
        headers: list[str] = []
        for row in rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=headers or ["global_id"])
            writer.writeheader()
            writer.writerows(rows)
        core.audit.record(
            "artifact_write", kind="csv", path=str(target), rows=len(rows), selector=selector
        )
        data: dict = {
            "path": str(target),
            "rows": len(rows),
            "columns": headers,
            "matched": total,
        }
        if total > len(rows):
            data["note"] = f"only the first {limit} of {total} matches were exported"
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Georeferencing of the loaded model: coordinate reference "
            "system, map conversion parameters, true and grid north. Answers "
            "'where is this model really' and diagnoses wrong-location issues."
        ),
    )
    @enveloped(core, "get_georeferencing")
    async def get_georeferencing() -> Envelope:
        core.session.require_loaded()
        data, cached = await core.cached_read(
            "georeferencing", lambda: build_georeferencing(core.session.ifc)
        )
        return ok(data, core.session_meta(), char_limit=limit_, cached=cached)
