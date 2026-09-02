"""Analysis tools: schema validation, IDS checking, quantity takeoff,
georeferencing, and CSV export. Everything here is ask-mode safe: reads never
change anything, and the CSV export writes an output file, never the model."""

from __future__ import annotations

import copy
import csv
import time
from contextlib import contextmanager
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
    local_rotation,
    mesh_provider,
    probe_elements,
    resolve_targets,
    tessellation_evidence,
)
from ifc_console.ifc.measure import measure_distance, measure_elements
from ifc_console.ifc.mesh_analysis import (
    directional_extent,
    mesh_health,
    mesh_source,
    ray_intervals,
    slice_mesh,
)
from ifc_console.ifc.profile import analyze_elements
from ifc_console.ifc.quantities import AGGREGATE_BY, build_georeferencing, compute_quantities
from ifc_console.ifc.query import ALLOWED_FIELDS, DEFAULT_FIELDS, element_row
from ifc_console.ifc.report import build_measurement_report
from ifc_console.ifc.units import si_to_file, unit_info
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


@contextmanager
def mesh_cache(core: AppCore, session):
    """Serve the tessellation inside the block from the session's mesh cache.

    Enter it on the model worker, inside the job: the provider is thread-local
    and the cache is keyed to the session that owns the file.
    """

    stats = {
        "mesh_requests": 0,
        "requested_elements": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "tessellation_batches": 0,
        "tessellated_elements": 0,
        "tessellated_triangles": 0,
        "tessellation_failures": 0,
        "tessellation_ms": 0.0,
        "evicted_meshes": 0,
        "uncached_federated_requests": 0,
    }

    def provider(ifc, elements, *, profile="standard", max_triangles=None):
        # a federated job reads a second model on its own worker; only the
        # session's own file is cache-keyed here
        if ifc is not session.ifc:
            started = time.perf_counter()
            built = element_meshes(
                ifc,
                elements,
                profile=profile,
                max_triangles=max_triangles,
            )
            stats["mesh_requests"] += 1
            stats["requested_elements"] += len(elements)
            stats["cache_misses"] += len(elements)
            stats["tessellation_batches"] += bool(elements)
            stats["tessellated_elements"] += len(built)
            stats["tessellated_triangles"] += sum(int(len(mesh[1])) for mesh in built.values())
            stats["tessellation_failures"] += max(0, len(elements) - len(built))
            stats["tessellation_ms"] = round(
                stats["tessellation_ms"] + (time.perf_counter() - started) * 1000.0,
                3,
            )
            stats["uncached_federated_requests"] += 1
            return built
        return core.element_meshes(
            elements,
            session=session,
            profile=profile,
            max_triangles=max_triangles,
            stats=stats,
        )

    with mesh_provider(provider):
        yield stats


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


def _element_identity(element) -> dict:
    return {
        "global_id": getattr(element, "GlobalId", None),
        "class": element.is_a(),
        "name": getattr(element, "Name", None),
    }


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
        name="inspect_element_mesh",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Inspect the untouched IFC triangle mesh for a small element set. "
            "Returns watertightness, winding, valid-volume status, connected components, "
            "boundary/non-manifold edges, degenerate/duplicate faces, Euler characteristic, "
            "a source hash and the exact tessellation settings. No repair is applied. "
            "backend='auto' uses optional Trimesh when installed and otherwise the built-in "
            "deterministic checks. Pass selector or global_ids."
        ),
    )
    @enveloped(core, "inspect_element_mesh")
    async def inspect_element_mesh_tool(
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None, Field(max_length=100, description="Explicit GlobalIds to inspect.")
        ] = None,
        backend: Literal["auto", "builtin", "trimesh"] = "auto",
        tessellation: Literal["standard", "analysis"] = "analysis",
        max_triangles: Annotated[
            int | None,
            Field(
                ge=1,
                le=1_000_000,
                description="Optional per-element triangle budget; defaults to the profile cap.",
            ),
        ] = None,
        tolerance: Annotated[
            float,
            Field(
                gt=0.0,
                le=0.01,
                description="SI-metre tolerance for degenerate geometry checks.",
            ),
        ] = 1e-9,
        include_filtered_preview: Annotated[
            bool,
            Field(
                description="Also inspect a copy with invalid, degenerate and duplicate faces removed."
            ),
        ] = False,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0, description=OFFSET_ARG)] = 0,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None

        def job() -> dict:
            elements = resolve_targets(
                s.ifc,
                selector=selector,
                global_ids=list(gids) if gids else None,
                physical_only=physical_only,
                max_elements=max_elements,
                offset=offset,
            )
            evidence = tessellation_evidence(tessellation, max_triangles=max_triangles)
            with mesh_cache(core, s):
                meshes = element_meshes(
                    s.ifc,
                    elements,
                    profile=tessellation,
                    max_triangles=max_triangles,
                )
            records = []
            missing = []
            for element in elements:
                mesh = meshes.get(element.id())
                if mesh is None:
                    missing.append(element.GlobalId)
                    continue
                vertices, faces = mesh
                records.append(
                    {
                        **_element_identity(element),
                        "source": mesh_source(vertices, faces, tessellation=evidence),
                        "mesh_health": mesh_health(
                            vertices,
                            faces,
                            backend=backend,
                            tolerance=tolerance,
                            include_filtered_preview=include_filtered_preview,
                        ),
                    }
                )
            if not records:
                raise ToolError(
                    "NO_GEOMETRY",
                    "none of the matched elements produced a mesh within the triangle budget",
                    "Raise max_triangles, use tessellation='standard', or inspect the IFC representation.",
                )
            return {
                "definition": "raw_ifc_tessellation_health",
                "selector": selector,
                "units": {**unit_info(s.ifc), "mesh_coordinates": "SI metres"},
                "tessellation": evidence,
                "matched": len(elements),
                "returned": len(records),
                "without_geometry": missing,
                "elements": records,
            }

        report, cached = await core.cached_read(
            "inspect_element_mesh",
            job,
            key=(
                selector,
                gids,
                backend,
                tessellation,
                max_triangles,
                tolerance,
                include_filtered_preview,
                physical_only,
                max_elements,
                offset,
            ),
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
        name="measure_directional_extent",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Measure the complete outside-to-outside mesh extent along any "
            "3D direction. This is a support projection, not wall/material thickness, "
            "and remains meaningful for an open mesh. frame='local' interprets the "
            "direction in IFC placement axes; frame='principal' returns the PCA basis "
            "and ambiguity flags. Results include both support points and a source hash."
        ),
    )
    @enveloped(core, "measure_directional_extent")
    async def measure_directional_extent_tool(
        direction: Annotated[
            list[float],
            Field(min_length=3, max_length=3, description="Three-component direction vector."),
        ],
        selector: Annotated[
            str | None, Field(description="IfcOpenShell selector; or pass global_ids.")
        ] = None,
        global_ids: Annotated[
            list[str] | None, Field(max_length=100, description="Explicit GlobalIds to measure.")
        ] = None,
        frame: Literal["world", "local", "principal"] = "world",
        tessellation: Literal["standard", "analysis"] = "analysis",
        max_triangles: Annotated[
            int | None, Field(ge=1, le=1_000_000, description="Per-element triangle budget.")
        ] = None,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=100)] = 25,
        offset: Annotated[int, Field(ge=0, description=OFFSET_ARG)] = 0,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None
        vector = tuple(direction)

        def job() -> dict:
            elements = resolve_targets(
                s.ifc,
                selector=selector,
                global_ids=list(gids) if gids else None,
                physical_only=physical_only,
                max_elements=max_elements,
                offset=offset,
            )
            evidence = tessellation_evidence(tessellation, max_triangles=max_triangles)
            with mesh_cache(core, s):
                meshes = element_meshes(
                    s.ifc,
                    elements,
                    profile=tessellation,
                    max_triangles=max_triangles,
                )
            units = unit_info(s.ifc)
            factor = units["to_si_factor"]
            records = []
            missing = []
            for element in elements:
                mesh = meshes.get(element.id())
                if mesh is None:
                    missing.append(element.GlobalId)
                    continue
                measured = directional_extent(
                    mesh[0],
                    mesh[1],
                    vector,
                    frame=frame,
                    local_rotation=local_rotation(element),
                    tessellation=evidence,
                )
                measured["extent_file"] = round(si_to_file(measured["extent_si"], factor), 6)
                records.append({**_element_identity(element), **measured})
            if not records:
                raise ToolError(
                    "NO_GEOMETRY",
                    "none of the matched elements produced a mesh within the triangle budget",
                    "Raise max_triangles or inspect the IFC representation.",
                )
            return {
                "definition": "outside_to_outside_extent",
                "selector": selector,
                "units": {**units, "si_values": "metres"},
                "tessellation": evidence,
                "matched": len(elements),
                "returned": len(records),
                "without_geometry": missing,
                "elements": records,
            }

        report, cached = await core.cached_read(
            "measure_directional_extent",
            job,
            key=(
                vector,
                selector,
                gids,
                frame,
                tessellation,
                max_triangles,
                physical_only,
                max_elements,
                offset,
            ),
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
        name="slice_element_mesh",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Cut one element mesh with an arbitrary plane. origin is a world "
            "point in SI metres (omit it for the referenced-vertex centroid); frame "
            "controls how normal is interpreted. Returns closure, area, perimeter, "
            "thickness distribution and an optional bounded 2D outline with a world-space "
            "origin/basis so a viewer can reconstruct it. Includes mesh prerequisites, "
            "source hash and tessellation settings; no repair is applied."
        ),
    )
    @enveloped(core, "slice_element_mesh")
    async def slice_element_mesh_tool(
        global_id: Annotated[str, Field(min_length=1, max_length=64)],
        normal: Annotated[
            list[float],
            Field(min_length=3, max_length=3, description="Three-component plane normal."),
        ],
        origin: Annotated[
            list[float] | None,
            Field(
                min_length=3,
                max_length=3,
                description="World plane origin [x,y,z] in SI metres; omit for mesh centroid.",
            ),
        ] = None,
        frame: Literal["world", "local", "principal"] = "world",
        backend: Literal["auto", "builtin", "trimesh"] = "auto",
        include_outline: bool = True,
        outline_points: Annotated[int, Field(ge=12, le=256)] = 160,
        tessellation: Literal["standard", "analysis"] = "analysis",
        max_triangles: Annotated[
            int | None, Field(ge=1, le=1_000_000, description="Per-element triangle budget.")
        ] = None,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        plane_origin = tuple(origin) if origin is not None else None
        plane_normal = tuple(normal)

        def job() -> dict:
            element = resolve_targets(s.ifc, global_ids=[global_id], max_elements=1)[0]
            evidence = tessellation_evidence(tessellation, max_triangles=max_triangles)
            with mesh_cache(core, s):
                meshes = element_meshes(
                    s.ifc,
                    [element],
                    profile=tessellation,
                    max_triangles=max_triangles,
                )
            mesh = meshes.get(element.id())
            if mesh is None:
                raise ToolError(
                    "NO_GEOMETRY",
                    "the element produced no mesh within the triangle budget",
                    "Raise max_triangles or inspect the IFC representation.",
                )
            result = slice_mesh(
                mesh[0],
                mesh[1],
                plane_normal,
                origin=plane_origin,
                frame=frame,
                local_rotation=local_rotation(element),
                backend=backend,
                include_outline=include_outline,
                outline_points=outline_points,
                tessellation=evidence,
            )
            units = unit_info(s.ifc)
            factor = units["to_si_factor"]
            cut = result["section"]
            if cut is not None:
                for name in ("width", "height", "perimeter"):
                    cut[f"{name}_file"] = round(si_to_file(cut[name], factor), 6)
                cut["area_file"] = (
                    round(si_to_file(cut["area"], factor, 2), 6)
                    if cut["area"] is not None
                    else None
                )
            return {
                **_element_identity(element),
                "units": {**units, "si_values": "metres"},
                **result,
            }

        report, cached = await core.cached_read(
            "slice_element_mesh",
            job,
            key=(
                global_id,
                plane_normal,
                plane_origin,
                frame,
                backend,
                include_outline,
                outline_points,
                tessellation,
                max_triangles,
            ),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
        )

    @mcp.tool(
        name="measure_local_thickness",
        annotations=ANALYSIS_ANN,
        description=(
            "[QUERY] Cast an infinite line through one element and return every "
            "deduplicated surface hit, material interval and internal void/gap. origin "
            "is always a world-coordinate point in SI metres; frame controls how the "
            "direction is interpreted. Material intervals are returned only for a valid "
            "closed, consistently wound volume with balanced oriented crossings; otherwise "
            "the raw intersections and an explicit refusal are preserved."
        ),
    )
    @enveloped(core, "measure_local_thickness")
    async def measure_local_thickness_tool(
        global_id: Annotated[str, Field(min_length=1, max_length=64)],
        origin: Annotated[
            list[float],
            Field(min_length=3, max_length=3, description="World point [x,y,z] in SI metres."),
        ],
        direction: Annotated[
            list[float],
            Field(min_length=3, max_length=3, description="Three-component line direction."),
        ],
        frame: Literal["world", "local", "principal"] = "world",
        backend: Literal["auto", "builtin", "trimesh"] = "auto",
        tolerance: Annotated[
            float,
            Field(gt=0.0, le=0.01, description="SI-metre hit merge tolerance."),
        ] = 1e-6,
        max_intersections: Annotated[
            int,
            Field(
                ge=2,
                le=128,
                description="Safety cap for compact line-intersection evidence.",
            ),
        ] = 64,
        tessellation: Literal["standard", "analysis"] = "analysis",
        max_triangles: Annotated[
            int | None, Field(ge=1, le=1_000_000, description="Per-element triangle budget.")
        ] = None,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        point = tuple(origin)
        vector = tuple(direction)

        def job() -> dict:
            element = resolve_targets(s.ifc, global_ids=[global_id], max_elements=1)[0]
            evidence = tessellation_evidence(tessellation, max_triangles=max_triangles)
            with mesh_cache(core, s):
                meshes = element_meshes(
                    s.ifc,
                    [element],
                    profile=tessellation,
                    max_triangles=max_triangles,
                )
            mesh = meshes.get(element.id())
            if mesh is None:
                raise ToolError(
                    "NO_GEOMETRY",
                    "the element produced no mesh within the triangle budget",
                    "Raise max_triangles or inspect the IFC representation.",
                )
            result = ray_intervals(
                mesh[0],
                mesh[1],
                point,
                vector,
                frame=frame,
                local_rotation=local_rotation(element),
                backend=backend,
                tolerance=tolerance,
                tessellation=evidence,
                max_intersections=max_intersections,
            )
            units = unit_info(s.ifc)
            factor = units["to_si_factor"]
            for interval in result["material_intervals"]:
                interval["thickness_file"] = round(si_to_file(interval["thickness_si"], factor), 6)
            for interval in result["non_material_intervals"]:
                interval["clear_width_file"] = round(
                    si_to_file(interval["clear_width_si"], factor), 6
                )
            result["overall_width_file"] = (
                round(si_to_file(result["overall_width_si"], factor), 6)
                if result["overall_width_si"] is not None
                else None
            )
            return {
                **_element_identity(element),
                "units": {**units, "si_values": "metres"},
                **result,
            }

        report, cached = await core.cached_read(
            "measure_local_thickness",
            job,
            key=(
                global_id,
                point,
                vector,
                frame,
                backend,
                tolerance,
                max_intersections,
                tessellation,
                max_triangles,
            ),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        return ok(
            report, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s)
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
            Field(
                description="stored_qto: restrict to one quantity set, e.g. 'Qto_WallBaseQuantities'."
            ),
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
            key=(
                selector,
                gids,
                method,
                metric,
                qto_set,
                quantity,
                include,
                exclude,
                axis,
                physical_only,
                max_elements,
                offset,
            ),
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
            "distance estimate for the nearest AABB pair, in SI metres and file units. "
            "The result names its sampling/selection methods and flags the surface value "
            "as an upper bound. AABB overlap is never called a solid overlap unless "
            "sampled occupancy confirms it on two valid-volume meshes. Each "
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
            "[QUERY] The high-level, versioned geometry analysis for a few objects, "
            "made for 'measure everything about the selected object'. It combines "
            "exact IFC representation parameters, semantic object frames, mesh "
            "health, adaptive cross sections, topology, material thicknesses and "
            "source reconciliation into stable namespaced measurements. Every value "
            "carries units, method, source, frame, confidence and alternatives; "
            "coverage lists unavailable, ambiguous and conflicting requests. detail "
            "controls response size. Legacy dimensions, box and cross_section fields "
            "remain for compatibility. Pass model from get_viewer_selection.model_id "
            "when analyzing a viewer selection. Use measure_elements for one simple "
            "metric over many elements."
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
                max_length=17,
                description=(
                    "Explicit 0..1 section fractions. Passing these without "
                    "station_strategy selects fixed mode for compatibility."
                ),
            ),
        ] = None,
        detail: Annotated[
            Literal["compact", "standard", "full"],
            Field(
                description=(
                    "compact returns preferred measurements and coverage; standard "
                    "adds alternatives and section summaries; full adds bounded "
                    "representation, sampling and outline evidence."
                )
            ),
        ] = "standard",
        measurement_set: Annotated[
            Literal["standard", "profile", "envelope", "fabrication"],
            Field(description="Supported measurement inventory to evaluate."),
        ] = "standard",
        measurement_ids: Annotated[
            list[str] | None,
            Field(
                max_length=80,
                description=(
                    "Optional stable namespaced measurement ids. When provided, "
                    "coverage reports every requested id."
                ),
            ),
        ] = None,
        frame: Annotated[
            Literal["semantic", "placement", "principal", "world"],
            Field(description="Frame used for directional and envelope measurements."),
        ] = "semantic",
        station_strategy: Annotated[
            Literal["auto", "fixed", "none"],
            Field(
                description=(
                    "auto adaptively discovers profile regions; fixed uses stations; "
                    "none skips mesh sections. Passing stations with auto selects fixed "
                    "mode for compatibility."
                )
            ),
        ] = "auto",
        precision: Annotated[
            Literal["standard", "high"],
            Field(description="Documented tessellation and adaptive sampling budget."),
        ] = "standard",
        include_alternatives: Annotated[
            bool,
            Field(description="Keep independent evidence and source disagreements."),
        ] = True,
        include_sections: Annotated[
            bool,
            Field(description="Include bounded representative section summaries."),
        ] = True,
        include_outline: Annotated[
            bool,
            Field(description=("Return bounded 2D section outlines with standard or full detail.")),
        ] = False,
        physical_only: bool = True,
        max_elements: Annotated[int, Field(ge=1, le=25)] = 10,
        offset: Annotated[int, Field(ge=0, description=OFFSET_ARG)] = 0,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        gids = tuple(global_ids) if global_ids else None
        cuts = tuple(stations) if stations else None
        requested = tuple(dict.fromkeys(measurement_ids or ())) or None
        resolved_station_strategy = (
            "fixed" if cuts and station_strategy == "auto" else station_strategy
        )

        def job() -> dict:
            with mesh_cache(core, s) as mesh_stats:
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
                    detail=detail,
                    measurement_set=measurement_set,
                    measurement_ids=requested,
                    frame=frame,
                    station_strategy=resolved_station_strategy,
                    precision=precision,
                    include_alternatives=include_alternatives,
                    include_sections=include_sections,
                    include_outline=include_outline,
                    physical_only=physical_only,
                    max_elements=max_elements,
                )
                report["selector"] = selector
                report["model_revision"] = {
                    "model_id": s.model_id,
                    "fingerprint": s.fingerprint,
                    "revision": s.revision,
                }
                report.setdefault("performance", {})["mesh_cache"] = dict(mesh_stats)
                report["performance"]["read_cache_hit"] = False
                return report

        report, cached = await core.cached_read(
            "analyze_element_geometry",
            job,
            key=(
                selector,
                gids,
                cuts,
                detail,
                measurement_set,
                requested,
                frame,
                resolved_station_strategy,
                precision,
                include_alternatives,
                include_sections,
                include_outline,
                physical_only,
                max_elements,
                offset,
            ),
            timeout=_HEAVY_TIMEOUT,
            session=s,
        )
        if cached:
            report = copy.deepcopy(report)
            report.setdefault("performance", {})["read_cache_hit"] = True
            report["performance"]["mesh_cache"] = {
                "mesh_requests": 0,
                "requested_elements": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "tessellation_batches": 0,
                "tessellated_elements": 0,
                "tessellated_triangles": 0,
                "tessellation_failures": 0,
                "tessellation_ms": 0.0,
                "skipped_due_to_read_cache": True,
            }
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
            analysis_version=analysis.get("analysis_version"),
            model_revision={
                "model_id": session.model_id,
                "fingerprint": session.fingerprint,
                "revision": session.revision,
            },
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
