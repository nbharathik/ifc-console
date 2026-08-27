"""Analysis tools: schema validation, IDS checking, quantity takeoff,
georeferencing, and CSV export. Everything here is ask-mode safe: reads never
change anything, and the CSV export writes an output file, never the model."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.operation_data import ValidationData
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.ifc.clash import MAX_ELEMENTS, MAX_RESULTS, compare_sets, prepare_set
from ifc_console.ifc.geometry import (
    element_meshes,
    mesh_provider,
    probe_elements,
    resolve_targets,
)
from ifc_console.ifc.measure import measure_distance, measure_elements
from ifc_console.ifc.profile import analyze_elements
from ifc_console.ifc.quantities import AGGREGATE_BY, build_georeferencing, compute_quantities
from ifc_console.ifc.query import ALLOWED_FIELDS, DEFAULT_FIELDS, element_row
from ifc_console.ifc.report import build_measurement_report
from ifc_console.ifc.validation import run_ids_validation, run_schema_validation
from ifc_console.mcp.tools_query import MODEL_ARG, _validate_subset, read_meta
from ifc_console.policy.modes import OpClass, Verdict

if TYPE_CHECKING:
    from ifc_console.app import AppCore

ANALYSIS_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
ARTIFACT_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

# Express rules and IDS walk every instance: give them the load-scale budget
# (a timeout poisons the worker, so err generous).
_HEAVY_TIMEOUT = 600.0

# Shared by every geometry tool that resolves a target set, so paging reads the
# same way whichever one the caller reaches for.
OFFSET_ARG = "Elements to skip in the deterministic order, for paging a large match."


def mesh_cache(core: AppCore, session):
    """Serve the tessellation inside the block from the session's mesh cache.

    Enter it on the model worker, inside the job: the provider is thread-local
    and the cache is keyed to the session that owns the file.
    """

    def provider(ifc, elements):
        # a federated job reads a second model on its own worker; only the
        # session's own file is cache-keyed here
        if ifc is not session.ifc:
            return element_meshes(ifc, elements)
        return core.element_meshes(elements, session=session)

    return mesh_provider(provider)


def page_global_ids(
    ifc,
    *,
    selector: str | None,
    global_ids: list[str] | None,
    physical_only: bool,
    max_elements: int,
    offset: int,
) -> list[str]:
    """The requested page as ids, for the readers that resolve their own targets."""
    page = resolve_targets(
        ifc,
        selector=selector,
        global_ids=global_ids,
        physical_only=physical_only,
        max_elements=max_elements,
        offset=offset,
    )
    return [element.GlobalId for element in page]


def register(mcp: OperationRegistry, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=ANALYSIS_ANN,
        data_model=ValidationData,
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
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        report, cached = await core.cached_read(
            "validate_model",
            lambda: run_schema_validation(
                s.ifc, express_rules=express_rules, max_issues=max_issues
            ),
            key=(express_rules, max_issues),
            timeout=_HEAVY_TIMEOUT if express_rules else 120,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )

    @mcp.tool(
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Check a model against a buildingSMART IDS (Information "
            "Delivery Specification) file: per-specification pass/fail, failing "
            "elements with GlobalIds and reasons. Needs the optional ifctester "
            "package; the error hint explains the install."
        ),
    )
    @enveloped(core, "validate_ids")
    async def validate_ids(
        ids_path: Annotated[
            str,
            Field(description="Path to an IDS XML file, or the alias of an attached one."),
        ],
        max_failures: Annotated[
            int, Field(ge=1, le=500, description="Failing elements kept per requirement.")
        ] = 50,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        path = core.resolve_attachment(ids_path, kind="ids")
        report, cached = await core.cached_read(
            "validate_ids",
            lambda: run_ids_validation(s.ifc, path, max_failures_per_spec=max_failures),
            key=(str(path), path.stat().st_mtime_ns, max_failures),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report,
            core.session_meta(),
            char_limit=limit_,
            cached=cached,
            **read_meta(core, s),
            ids=str(path),
        )

    @mcp.tool(
        name="compute_quantities",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Quantity takeoff for a selector-defined set from stored "
            "quantity sets (Qto_*): per-group sums and grand totals with the "
            "model's units. aggregate_by groups per class, type, storey, "
            "material, or none. Optionally restrict to named quantities, e.g. "
            "['NetVolume', 'GrossArea']. source='derived' fills elements that "
            "have no stored values from their mesh geometry, marked as such."
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
        source: Literal["stored", "derived"] = "stored",
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        if aggregate_by not in AGGREGATE_BY:
            raise ToolError(
                "INVALID_INPUT",
                f"unknown aggregate_by {aggregate_by!r}",
                f"Allowed: {list(AGGREGATE_BY)}",
            )
        wanted = tuple(quantities) if quantities else None

        def job() -> dict:
            with mesh_cache(core, s):
                return compute_quantities(
                    s.ifc,
                    selector,
                    aggregate_by=aggregate_by,
                    quantities=wanted,
                    source=source,
                )

        report, cached = await core.cached_read(
            "compute_quantities",
            job,
            key=(selector, aggregate_by, wanted, source),
            timeout=_HEAVY_TIMEOUT if source == "derived" else 120,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )

    @mcp.tool(
        name="get_element_geometry",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Derived geometry per element from its triangle mesh, in SI "
            "metres: world bounding box, length/width/height extents along the "
            "placement axes, plan footprint area, volume, centroid, and a "
            "prismatic-confidence rating. The local y extent is the geometric "
            "thickness of a placement-aligned wall. Pass a selector or explicit "
            "global_ids from search_elements or get_viewer_selection."
        ),
    )
    @enveloped(core, "get_element_geometry")
    async def get_element_geometry(
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None, Field(max_length=500, description="Explicit GlobalIds to probe.")
        ] = None,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=2000)] = 500,
        offset: Annotated[int, Field(ge=0, description=OFFSET_ARG)] = 0,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None

        def job() -> dict:
            with mesh_cache(core, s):
                return probe_elements(
                    s.ifc,
                    selector=selector,
                    global_ids=list(gids) if gids else None,
                    physical_only=physical_only,
                    max_elements=max_elements,
                    offset=offset,
                )

        report, cached = await core.cached_read(
            "get_element_geometry",
            job,
            key=(selector, gids, physical_only, max_elements, offset),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report,
            core.session_meta(),
            char_limit=limit_,
            cached=cached,
            offset=offset,
            **read_meta(core, s),
        )

    @mcp.tool(
        name="measure_elements",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Measure one metric for a set of elements with one named, "
            "auditable method: stored_qto reads a quantity-set value, layer_sum "
            "adds material layer thicknesses (include/exclude glob filters on "
            "layer and material names), geometry_extent measures the mesh along "
            "a placement or world axis. Values come back in the file's units "
            "and SI, per element plus a summary. Pass a selector or global_ids."
        ),
    )
    @enveloped(core, "measure_elements")
    async def measure_elements_tool(
        method: Literal["stored_qto", "layer_sum", "geometry_extent"],
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None, Field(max_length=500, description="Explicit GlobalIds to measure.")
        ] = None,
        metric: Annotated[
            str | None,
            Field(max_length=80, description="Label for the report, e.g. 'thickness'."),
        ] = None,
        quantity: Annotated[
            str | None,
            Field(description="stored_qto: the quantity name, e.g. 'Width'."),
        ] = None,
        qto_set: Annotated[
            str | None,
            Field(description="stored_qto: restrict to one quantity set, e.g. 'Qto_WallBaseQuantities'."),
        ] = None,
        include_layers: Annotated[
            list[str] | None,
            Field(max_length=20, description="layer_sum: keep only layers matching these globs."),
        ] = None,
        exclude_layers: Annotated[
            list[str] | None,
            Field(max_length=20, description="layer_sum: drop layers matching these globs."),
        ] = None,
        axis: Literal["local_x", "local_y", "local_z", "world_x", "world_y", "world_z"] = "local_y",
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=2000)] = 500,
        offset: Annotated[int, Field(ge=0, description=OFFSET_ARG)] = 0,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None
        include = tuple(include_layers) if include_layers else None
        exclude = tuple(exclude_layers) if exclude_layers else None

        def job() -> dict:
            with mesh_cache(core, s):
                target = selector
                ids = list(gids) if gids else None
                if offset:
                    # measure_elements resolves its own targets, so the page is
                    # resolved here and handed over as ids
                    ids = page_global_ids(
                        s.ifc,
                        selector=target,
                        global_ids=ids,
                        physical_only=physical_only,
                        max_elements=max_elements,
                        offset=offset,
                    )
                    target = None
                return measure_elements(
                    s.ifc,
                    selector=target,
                    global_ids=ids,
                    method=method,
                    metric=metric,
                    qto_set=qto_set,
                    quantity=quantity,
                    include_layers=list(include) if include else None,
                    exclude_layers=list(exclude) if exclude else None,
                    axis=axis,
                    physical_only=physical_only,
                    max_elements=max_elements,
                )

        report, cached = await core.cached_read(
            "measure_elements",
            job,
            key=(selector, gids, method, metric, qto_set, quantity, include, exclude, axis, physical_only, max_elements, offset),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report,
            core.session_meta(),
            char_limit=limit_,
            cached=cached,
            offset=offset,
            **read_meta(core, s),
        )

    @mcp.tool(
        name="measure_distance",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Closest approach between two element sets: centroid "
            "distance, axis-aligned bounding-box gap, and closest-point surface "
            "distance for the nearest pair, in SI metres and file units. Each "
            "side is a selector or a GlobalId list. Overlapping pairs report a "
            "surface distance of zero; detect_clashes quantifies the overlap."
        ),
    )
    @enveloped(core, "measure_distance")
    async def measure_distance_tool(
        set_a: Annotated[
            str | None, Field(description="Selector for side A; or pass global_ids_a.")
        ] = None,
        global_ids_a: Annotated[list[str] | None, Field(max_length=200)] = None,
        set_b: Annotated[
            str | None, Field(description="Selector for side B; or pass global_ids_b.")
        ] = None,
        global_ids_b: Annotated[list[str] | None, Field(max_length=200)] = None,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=1000)] = 200,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids_a = tuple(global_ids_a) if global_ids_a else None
        gids_b = tuple(global_ids_b) if global_ids_b else None

        def job() -> dict:
            with mesh_cache(core, s):
                return measure_distance(
                    s.ifc,
                    set_a=set_a,
                    global_ids_a=list(gids_a) if gids_a else None,
                    set_b=set_b,
                    global_ids_b=list(gids_b) if gids_b else None,
                    physical_only=physical_only,
                    max_elements=max_elements,
                )

        report, cached = await core.cached_read(
            "measure_distance",
            job,
            key=(set_a, gids_a, set_b, gids_b, physical_only, max_elements),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )

    @mcp.tool(
        name="analyze_element_geometry",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] The full measurement probe for a few elements, made for "
            "'measure everything about this object'. Reads exact profile "
            "parameters from the IFC definition (widths, depths, flange and web "
            "thicknesses, extrusion length) and independently slices the "
            "triangle mesh across the element's long axis to measure the cross "
            "section: width, height, wall-thickness distribution, perimeter, "
            "and area. Both merge into `dimensions` with a named source per "
            "value; disagreement over 5% is flagged. include_outline=true adds "
            "the 2D section outline points. Works without the viewer and "
            "without vision. Use measure_elements for one metric over many "
            "elements."
        ),
    )
    @enveloped(core, "analyze_element_geometry")
    async def analyze_element_geometry_tool(
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None,
            Field(max_length=25, description="Explicit GlobalIds, e.g. the viewer selection."),
        ] = None,
        stations: Annotated[
            list[float] | None,
            Field(
                max_length=7,
                description="Cut positions as 0..1 fractions of the long axis; default 0.3, 0.5, 0.7.",
            ),
        ] = None,
        include_outline: Annotated[
            bool, Field(description="Return the 2D outline points of the measured section.")
        ] = False,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=25)] = 10,
        offset: Annotated[int, Field(ge=0, description=OFFSET_ARG)] = 0,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None
        cuts = tuple(stations) if stations else None

        def job() -> dict:
            with mesh_cache(core, s):
                target = selector
                ids = list(gids) if gids else None
                if offset:
                    # analyze_elements resolves its own targets, so the page is
                    # resolved here and handed over as ids
                    ids = page_global_ids(
                        s.ifc,
                        selector=target,
                        global_ids=ids,
                        physical_only=physical_only,
                        max_elements=max_elements,
                        offset=offset,
                    )
                    target = None
                report = analyze_elements(
                    s.ifc,
                    selector=target,
                    global_ids=ids,
                    stations=cuts,
                    include_outline=include_outline,
                    physical_only=physical_only,
                    max_elements=max_elements,
                )
                report["selector"] = selector
                return report

        report, cached = await core.cached_read(
            "analyze_element_geometry",
            job,
            key=(selector, gids, cuts, include_outline, physical_only, max_elements, offset),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report,
            core.session_meta(),
            char_limit=limit_,
            cached=cached,
            offset=offset,
            **read_meta(core, s),
        )

    @mcp.tool(
        name="detect_clashes",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Geometric clash detection between two selector-defined sets, "
            "optionally across two open models for federated coordination. "
            "mode='overlap' reports solids that share space, with the shared "
            "volume in cubic metres; mode='clearance' reports pairs closer than "
            "tolerance. Openings, spaces and annotations are skipped unless "
            "physical_only=false. precision='fast' does bounding boxes only and "
            "over-reports. Feed the returned global_ids straight to "
            "highlight_elements to see the clashes in the viewer."
        ),
    )
    @enveloped(core, "detect_clashes")
    async def detect_clashes_tool(
        set_a: Annotated[
            str, Field(description="Selector for the first set, e.g. `IfcDuctSegment`.")
        ],
        set_b: Annotated[
            str | None,
            Field(description="Selector for the second set; omit to clash set_a with itself."),
        ] = None,
        mode: Literal["overlap", "clearance"] = "overlap",
        tolerance: Annotated[
            float,
            Field(ge=0.0, le=10.0, description="Metres. Overlap tolerance, or the clearance gap."),
        ] = 0.01,
        precision: Literal["sampled", "fast", "exact"] = "sampled",
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=MAX_ELEMENTS)] = 1000,
        max_results: Annotated[int, Field(ge=1, le=MAX_RESULTS)] = 200,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
        other_model: Annotated[
            str | None,
            Field(description="Model that set_b comes from; omit to use the same model."),
        ] = None,
    ) -> Envelope:
        session_a = core.resolve_session(model)
        session_b = core.resolve_session(other_model) if other_model else session_a
        self_check = set_b is None and session_b is session_a

        # Each set is read on the worker that owns its model; the comparison is
        # plain numpy, so it can then run on either.
        async def prepared(session, selector: str):
            def run():
                with mesh_cache(core, session):
                    return prepare_set(
                        session.ifc,
                        selector,
                        physical_only=physical_only,
                        max_elements=max_elements,
                    )

            return await session.run(run, timeout=_HEAVY_TIMEOUT)

        prep_a = await prepared(session_a, set_a)
        prep_b = prep_a if self_check else await prepared(session_b, set_b or set_a)
        report = await session_a.run(
            lambda: compare_sets(
                prep_a,
                prep_b,
                self_check=self_check,
                same_model=session_b is session_a,
                mode=mode,
                tolerance=tolerance,
                precision=precision,
                max_results=max_results,
            ),
            timeout=_HEAVY_TIMEOUT,
        )
        report["models"] = {"set_a": session_a.model_id, "set_b": session_b.model_id}
        return ok(report, core.session_meta(), char_limit=limit_, **read_meta(core, session_a))

    @mcp.tool(
        name="get_measurement_recipe",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] The project's measurement recipe for one element class and "
            "property: the company's method, its parameters, tolerance, and the "
            "document citation it came from. Type-specific recipes beat "
            "class-level ones; pass type_name when you know it. The result "
            "includes suggested_arguments ready for measure_elements. A miss is "
            "not a failure: fall back to search_ifc_knowledge(corpus='project') "
            "and pick a method yourself, saying so in the report."
        ),
    )
    @enveloped(core, "get_measurement_recipe")
    async def get_measurement_recipe(
        ifc_class: Annotated[str, Field(description="Element class, e.g. IfcWall.")],
        property_name: Annotated[
            str, Field(description="The measured property, e.g. 'thickness'.")
        ],
        type_name: Annotated[
            str | None,
            Field(description="The element's type name, from get_element or query_elements."),
        ] = None,
    ) -> Envelope:
        from ifc_console.knowledge.project_recipes import find_recipe

        result = find_recipe(
            core.store.project_dir,
            ifc_class=ifc_class,
            property_name=property_name,
            type_name=type_name,
        )
        return ok(result, core.session_meta(), char_limit=limit_)

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
    @core.active_model_operation
    async def export_csv(
        selector: Annotated[str, Field(description="IfcOpenShell selector, e.g. `IfcWall`.")],
        path: Annotated[str, Field(description="Target file path; must end in .csv.")],
        fields: Annotated[
            list[str] | None,
            Field(
                description=f"Row fields beyond global_id+class. Allowed: {list(ALLOWED_FIELDS)}. "
                "Pass [] for the smallest row (global_id + class only)."
            ),
        ] = None,
        properties: Annotated[
            list[str] | None,
            Field(
                max_length=20,
                description="Dotted pset columns, e.g. ['Pset_WallCommon.FireRating'].",
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=100_000)] = 10_000,
        overwrite: bool = False,
    ) -> Envelope:
        session = core.session
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
        use_fields = tuple(fields) if fields is not None else DEFAULT_FIELDS
        props = list(dict.fromkeys(properties or []))
        for dotted in props:
            pset, dot, prop = dotted.partition(".")
            if not (dot and pset and prop):
                raise ToolError(
                    "INVALID_INPUT",
                    f"property column {dotted!r} is not in 'Pset_Name.Property' form.",
                    "Use the dotted form, e.g. Pset_WallCommon.FireRating. "
                    "get_element shows the psets an element carries.",
                )

        def job() -> tuple[list[dict], int]:
            import ifcopenshell.util.element as element_util
            import ifcopenshell.util.selector as selector_util

            try:
                elements = list(selector_util.filter_elements(session.ifc, selector))
            except Exception as exc:
                raise ToolError(
                    "INVALID_QUERY",
                    f"selector failed: {exc}",
                    "See query_elements for selector examples.",
                ) from exc
            total = len(elements)
            # filter_elements returns a set; an unsorted slice would export a
            # different subset on every run
            elements.sort(key=lambda e: e.id())
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

        rows, total = await session.run(job, timeout=300)
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
        annotations=ARTIFACT_ANN,
        description=(
            "[ARTIFACT] Write a markdown measurement report for a few elements: "
            "identity, merged dimensions with sources, measured cross section, "
            "profile definition, bounding box, and stored quantities, using "
            "analyze_element_geometry under the hood. Allowed in ask mode: it "
            "writes a report file, never the model. The report is registered as "
            "an artifact; give the user the path and the artifact id."
        ),
    )
    @enveloped(core, "export_measurement_report")
    @core.active_model_operation
    async def export_measurement_report(
        path: Annotated[str, Field(description="Target file path; must end in .md.")],
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None,
            Field(max_length=25, description="Explicit GlobalIds, e.g. the viewer selection."),
        ] = None,
        title: Annotated[
            str | None, Field(max_length=120, description="Report title; defaults from the model.")
        ] = None,
        notes: Annotated[
            str | None,
            Field(max_length=2000, description="Context to include, e.g. method or caveats."),
        ] = None,
        include_qtos: Annotated[
            bool, Field(description="Include each element's stored quantity sets.")
        ] = True,
        stations: Annotated[
            list[float] | None,
            Field(max_length=7, description="Cut positions as 0..1 fractions of the long axis."),
        ] = None,
        overwrite: bool = False,
    ) -> Envelope:
        session = core.session
        if core.policy.decide(OpClass.ARTIFACT) is not Verdict.ALLOW:
            raise ToolError(
                "ASK_MODE_BLOCKED",
                "artifact writes are disabled.",
                "Ask the user to run /mode edit in the ifc-console terminal.",
            )
        target = core.require_path_allowed(Path(path))
        if target.suffix.lower() != ".md":
            raise ToolError(
                "INVALID_INPUT",
                f"{target.name} does not end in .md.",
                "Pass a path ending in .md.",
            )
        if target.exists() and not overwrite:
            raise ToolError(
                "FILE_EXISTS",
                f"{target} already exists.",
                "Pass overwrite=true to replace it, or pick another path.",
            )
        gids = list(global_ids) if global_ids else None
        cuts = tuple(stations) if stations else None

        def job() -> tuple[dict, list[dict], dict]:
            import ifcopenshell.util.element as element_util

            with mesh_cache(core, session):
                analysis = analyze_elements(
                    session.ifc,
                    selector=selector,
                    global_ids=gids,
                    stations=cuts,
                    max_elements=25,
                )
            entries = []
            for record in analysis["elements"]:
                entry: dict = {"analysis": record}
                if include_qtos and record.get("global_id"):
                    try:
                        element = session.ifc.by_guid(record["global_id"])
                        entry["qtos"] = element_util.get_psets(element, qtos_only=True)
                    except Exception:
                        entry["qtos"] = {}
                entries.append(entry)
            project = next(iter(session.ifc.by_type("IfcProject")), None)
            model_info = {
                "project": getattr(project, "Name", None),
                "file": session.path.name if session.path else None,
                "schema": session.ifc.schema,
            }
            return analysis, entries, model_info

        analysis, entries, model_info = await session.run(job, timeout=_HEAVY_TIMEOUT)
        text = build_measurement_report(
            title=title or f"Measurement report: {model_info.get('file') or 'model'}",
            model=model_info,
            units=analysis["units"],
            entries=entries,
            notes=notes,
            generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        ref = core.artifacts.put_text(
            text,
            name=target.name,
            kind="measurement-report",
            media_type="text/markdown",
            producer="export_measurement_report",
            metadata={"elements": len(entries), "selector": selector},
        )
        core.audit.record(
            "artifact_write",
            kind="measurement-report",
            path=str(target),
            elements=len(entries),
            artifact_id=ref.artifact_id,
        )
        data = {
            "path": str(target),
            "elements": len(entries),
            "artifact_id": ref.artifact_id,
            "flags": sorted({flag for e in entries for flag in e["analysis"].get("flags", [])}),
        }
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
    async def get_georeferencing(
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        data, cached = await core.cached_read(
            "georeferencing", lambda: build_georeferencing(s.ifc), session=s
        )
        return ok(data, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s))
