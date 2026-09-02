"""Whole-model insight tools: revision diff, geometric spatial relations, the
model health check, the property gap audit, and the quality scorecard. All of
them are reads: nothing here changes a model."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.ifc.compare import MAX_ELEMENTS as COMPARE_MAX
from ifc_console.ifc.compare import compare_snapshots, snapshot
from ifc_console.ifc.health import CHECKS, check_model_health
from ifc_console.ifc.health import MAX_ELEMENTS as HEALTH_MAX
from ifc_console.ifc.property_audit import MAX_ELEMENTS as AUDIT_MAX
from ifc_console.ifc.property_audit import audit_element_properties
from ifc_console.ifc.quality import MAX_EXAMPLES as QUALITY_EXAMPLES
from ifc_console.ifc.quality import assess_model_quality
from ifc_console.ifc.spatial_query import MAX_ELEMENTS as SPATIAL_MAX
from ifc_console.ifc.spatial_query import query_spatial
from ifc_console.mcp.tools_analysis import mesh_cache
from ifc_console.mcp.tools_query import MODEL_ARG, read_meta

if TYPE_CHECKING:
    from ifc_console.app import AppCore

INSIGHT_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

GEOMETRY_READ = (Capability.MODEL_READ, Capability.GEOMETRY)
MODEL_READ = (Capability.MODEL_READ,)

# Every tool here tessellates a whole model, so they get the load-scale budget
# for the same reason the analysis family does: a timeout poisons the worker.
_HEAVY_TIMEOUT = 600.0

TOOL_NAMES = (
    "compare_models",
    "query_spatial",
    "check_model_health",
    "audit_element_properties",
    "assess_model_quality",
)


def register(mcp: OperationRegistry, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        name="compare_models",
        annotations=INSIGHT_ANN,
        required_capabilities=GEOMETRY_READ,
        description=(
            "[QUERY] Diff two open revisions of the same project: what was "
            "added, removed, moved, reshaped, retyped, re-containered, or had "
            "properties edited. `model` is the older baseline and `other_model` "
            "the newer revision, so a positive move is where the element went. "
            "Attach the second file first (attach, then list_models). Elements "
            "are paired by GlobalId; when the two files barely share ids the "
            "diff falls back to matching on class, type and name and says so in "
            "`matcher`. Positions and volumes come from the triangle meshes, so "
            "a move is a real move and not just an edited placement. Feed "
            "`global_ids` straight to apply_color_theme to see the change set."
        ),
    )
    @enveloped(core, "compare_models")
    async def compare_models_tool(
        other_model: Annotated[
            str,
            Field(description="model_id of the newer revision to compare against; see list_models."),
        ],
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
        selector: Annotated[
            str | None,
            Field(description="Limit the diff to one set, e.g. `IfcWall`; omit for every element."),
        ] = None,
        move_tolerance: Annotated[
            float,
            Field(ge=0.0, le=100.0, description="Metres an element may shift before it counts as moved."),
        ] = 0.01,
        volume_tolerance: Annotated[
            float,
            Field(ge=0.0, le=1.0, description="Fractional volume change before geometry counts as changed."),
        ] = 0.01,
        physical_only: bool = True,
        include_geometry: Annotated[
            bool, Field(description="Compare meshes; false skips moves and reshapes but is far faster.")
        ] = True,
        include_properties: Annotated[
            bool, Field(description="Compare each element's own property sets, type values excluded.")
        ] = True,
        max_elements: Annotated[int, Field(ge=1, le=COMPARE_MAX)] = 5000,
        max_changes: Annotated[int, Field(ge=1, le=1000)] = 100,
    ) -> Envelope:
        before = core.resolve_session(model)
        after = core.resolve_session(other_model)
        if before is after:
            raise ToolError(
                "INVALID_INPUT",
                "both sides resolve to the same model",
                "Attach the other revision first (attach path=...), then pass its "
                "model_id as other_model; list_models shows what is resident.",
            )
        options = {
            "selector": selector,
            "physical_only": physical_only,
            "max_elements": max_elements,
            "include_properties": include_properties,
            "include_geometry": include_geometry,
        }

        async def snap(session):
            def job() -> dict:
                # each side is read on the worker that owns its file; the diff
                # itself is plain data and can then run on either
                with mesh_cache(core, session):
                    return snapshot(session.ifc, **options)

            return await core.cached_read(
                "compare_models.snapshot",
                job,
                key=(selector, physical_only, max_elements, include_properties, include_geometry),
                timeout=_HEAVY_TIMEOUT,
                session=session,
            )

        snapshot_a, cached_a = await snap(before)
        snapshot_b, cached_b = await snap(after)
        report = await before.run(
            lambda: compare_snapshots(
                snapshot_a,
                snapshot_b,
                move_tolerance=move_tolerance,
                volume_tolerance=volume_tolerance,
                max_changes=max_changes,
            ),
            timeout=_HEAVY_TIMEOUT,
        )
        report["models"] = {"before": before.model_id, "after": after.model_id}
        return ok(
            report,
            core.session_meta(),
            char_limit=limit_,
            cached=cached_a and cached_b,
            **read_meta(core, before),
        )

    @mcp.tool(
        name="query_spatial",
        annotations=INSIGHT_ANN,
        required_capabilities=GEOMETRY_READ,
        description=(
            "[QUERY] Which elements stand in a geometric relation to one target "
            "or to a box: 'inside' (what is in this space or room), 'crosses' "
            "(what this duct or pipe passes through), 'above' and 'below' (what "
            "sits directly over or under it, nearest first), 'within_distance' "
            "(what is within N metres, by real surface distance), and "
            "'within_box' (what falls inside explicit bounds, or the target's "
            "own bounding box). Answers containment that the selector's "
            "`location=` facet cannot, because most exporters contain elements "
            "in the storey rather than the space. Every result names the method "
            "it used and a confidence; solid tests are unreliable on meshes that "
            "are not closed, which the target block reports."
        ),
    )
    @enveloped(core, "query_spatial")
    async def query_spatial_tool(
        relation: Literal["inside", "crosses", "above", "below", "within_distance", "within_box"],
        target_global_id: Annotated[
            str | None,
            Field(description="The element the relation is about, e.g. the space or the duct."),
        ] = None,
        box: Annotated[
            list[float] | None,
            Field(
                min_length=6,
                max_length=6,
                description="within_box only: [min_x, min_y, min_z, max_x, max_y, max_z] in SI metres.",
            ),
        ] = None,
        selector: Annotated[
            str,
            Field(description="The candidate set to test, e.g. `IfcSprinkler` or `IfcWall`."),
        ] = "IfcElement",
        distance: Annotated[
            float,
            Field(ge=0.0, le=1000.0, description="Metres: the reach for within_distance, the gap cap for above/below."),
        ] = 1.0,
        tolerance: Annotated[
            float, Field(ge=0.0, le=10.0, description="Metres of slack on a boundary.")
        ] = 0.01,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=SPATIAL_MAX)] = 1000,
        max_results: Annotated[int, Field(ge=1, le=500)] = 100,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        bounds = tuple(box) if box else None

        def job() -> dict:
            with mesh_cache(core, s):
                return query_spatial(
                    s.ifc,
                    relation=relation,
                    target_global_id=target_global_id,
                    box=list(bounds) if bounds else None,
                    selector=selector,
                    distance=distance,
                    tolerance=tolerance,
                    physical_only=physical_only,
                    max_elements=max_elements,
                    max_results=max_results,
                )

        report, cached = await core.cached_read(
            "query_spatial",
            job,
            key=(
                relation,
                target_global_id,
                bounds,
                selector,
                distance,
                tolerance,
                physical_only,
                max_elements,
                max_results,
            ),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )

    @mcp.tool(
        name="check_model_health",
        annotations=INSIGHT_ANN,
        required_capabilities=GEOMETRY_READ,
        description=(
            "[QUERY] Is this file any good? Reports duplicate GlobalIds, "
            "elements in no spatial container, representations with no usable "
            "solid, elements placed far outside the rest of the model, "
            "double-modelled solids, a model extent that contradicts the "
            "declared unit, empty storeys, and unused types. This is the "
            "data-quality question validate_model does not answer: that one "
            "checks the schema, and a file can satisfy every rule in it and "
            "still be unusable. Each finding carries a severity, examples, and "
            "global_ids for highlight_elements. The geometry checks are skipped "
            "with a stated reason above max_elements."
        ),
    )
    @enveloped(core, "check_model_health")
    async def check_model_health_tool(
        checks: Annotated[
            list[str] | None,
            Field(
                max_length=len(CHECKS),
                description=f"Run only these checks. Allowed: {list(CHECKS)}.",
            ),
        ] = None,
        max_findings: Annotated[
            int, Field(ge=1, le=200, description="Elements listed per check.")
        ] = 20,
        max_elements: Annotated[
            int,
            Field(ge=1, le=HEALTH_MAX, description="Above this the geometry checks are skipped."),
        ] = 5000,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        wanted = tuple(checks) if checks else None

        def job() -> dict:
            with mesh_cache(core, s):
                return check_model_health(
                    s.ifc,
                    checks=list(wanted) if wanted else None,
                    max_findings=max_findings,
                    max_elements=max_elements,
                )

        report, cached = await core.cached_read(
            "check_model_health",
            job,
            key=(wanted, max_findings, max_elements),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )

    @mcp.tool(
        name="audit_element_properties",
        annotations=INSIGHT_ANN,
        required_capabilities=MODEL_READ,
        description=(
            "[QUERY] What is an element missing? For a few elements, reads the "
            "schema's property set templates for the class and predefined type "
            "and reports, per applicable property and quantity set, which "
            "properties are filled (on the occurrence or inherited from the "
            "type), which are empty or absent, and where each gap is usually "
            "filled from: geometry, material, spatial position, documents, the "
            "type, or a person. `psets='core'` (default) covers the class's "
            "Common set and BaseQuantities plus anything already present; "
            "`'all'` lists every applicable template. `detail='full'` adds data "
            "types and enumerations for every property. Call this before "
            "deriving or proposing values; it is the gap list, not the answer."
        ),
    )
    @enveloped(core, "audit_element_properties")
    async def audit_element_properties_tool(
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None,
            Field(max_length=AUDIT_MAX, description="Explicit GlobalIds, e.g. the viewer selection."),
        ] = None,
        psets: Annotated[
            Literal["core", "present", "all"],
            Field(description="Which applicable sets to audit."),
        ] = "core",
        pset_names: Annotated[
            list[str] | None,
            Field(max_length=20, description="Audit exactly these property set names instead."),
        ] = None,
        detail: Annotated[
            Literal["compact", "full"],
            Field(description="full adds a row per property with data type and enumeration."),
        ] = "compact",
        max_elements: Annotated[int, Field(ge=1, le=AUDIT_MAX)] = 10,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None
        names = tuple(pset_names) if pset_names else None

        async def audit(level: str) -> tuple[dict, bool]:
            def job() -> dict:
                return audit_element_properties(
                    s.ifc,
                    selector=selector,
                    global_ids=list(gids) if gids else None,
                    psets=psets,
                    pset_names=list(names) if names else None,
                    detail=level,
                    max_elements=max_elements,
                )

            return await core.cached_read(
                "audit_element_properties",
                job,
                key=(selector, gids, psets, names, level, max_elements),
                timeout=120,
                session=s,
            )

        def envelope(report: dict, cached: bool) -> Envelope:
            return ok(
                report,
                core.session_meta(),
                char_limit=limit_,
                cached=cached,
                returned=report.get("returned"),
                **read_meta(core, s),
            )

        report, cached = await audit(detail)
        result = envelope(report, cached)
        truncation = result.data.get("truncation") if isinstance(result.data, dict) else None
        if detail == "full" and isinstance(truncation, dict) and truncation.get("kept") == 0:
            # One element at full detail can outgrow a response; compact rows
            # still answer the question, and pset_names narrows the rest.
            report, cached = await audit("compact")
            report["note"] = (
                "full detail did not fit in one response, so this is compact detail; "
                "pass pset_names or psets='core' to get property rows for fewer sets"
            )
            result = envelope(report, cached)
        return result

    @mcp.tool(
        name="assess_model_quality",
        annotations=INSIGHT_ANN,
        required_capabilities=MODEL_READ,
        description=(
            "[QUERY] How good is this file, and what would improve it? Scores "
            "the open model 0-100 with a letter grade over ten dimensions: "
            "project identity, units and georeferencing, spatial containment, "
            "naming, types and predefined types, classification, common "
            "property sets and their key properties per class, base "
            "quantities, materials, and shape representations. Every dimension "
            "carries its metrics, findings with example global_ids, and the "
            "concrete improvements, and `top_improvements` orders them worst "
            "first. `text` is a short digest for a report. Pair it with "
            "check_model_health for geometry defects and validate_model for "
            "schema rules; this scores usefulness, not conformance."
        ),
    )
    @enveloped(core, "assess_model_quality")
    async def assess_model_quality_tool(
        max_examples: Annotated[
            int,
            Field(ge=1, le=QUALITY_EXAMPLES, description="Example elements listed per finding."),
        ] = 5,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)

        report, cached = await core.cached_read(
            "assess_model_quality",
            lambda: assess_model_quality(s.ifc, max_examples=max_examples),
            key=(max_examples,),
            timeout=120,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )
