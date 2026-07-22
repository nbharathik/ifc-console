"""Read-only query tools: session status, project info, spatial tree,
selector queries, element detail, psets, schema docs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from ifc_console import __version__
from ifc_console.ifc.elements import INCLUDE_ALLOWED, INCLUDE_DEFAULT, element_detail
from ifc_console.ifc.info import build_project_info
from ifc_console.ifc.query import ALLOWED_FIELDS, DEFAULT_FIELDS, run_query
from ifc_console.ifc.schema_docs import build_schema_docs
from ifc_console.ifc.spatial import build_spatial_tree
from ifc_console.mcp.envelope import ToolError, ok
from ifc_console.mcp.server import enveloped

if TYPE_CHECKING:
    from ifc_console.app import AppCore

QUERY_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)


def _validate_subset(values: list[str] | None, allowed: tuple[str, ...], param: str) -> None:
    if not values:
        return
    unknown = [v for v in values if v not in allowed]
    if unknown:
        raise ToolError(
            "INVALID_INPUT",
            f"unknown {param} value(s): {unknown}",
            f"Allowed {param} values: {list(allowed)}",
        )


def register(mcp: FastMCP, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Return server/session state: loaded model (name, path, schema, "
            "size), mode (ask = query-only, edit = mutations allowed), "
            "unsaved-changes flag, fingerprint, viewer connection state, server "
            "version. Call this first if unsure about session state."
        ),
    )
    @enveloped(core, "get_session_status")
    async def get_session_status() -> str:
        s = core.session
        model: dict[str, Any] = {"loaded": s.loaded}
        if s.loaded:
            model.update(
                path=str(s.path),
                name=s.name,
                schema=s.schema,
                size_bytes=s.size_bytes,
                loaded_at=s.loaded_at,
            )
        data = {
            "server": {"name": "ifc-console", "version": __version__},
            "model": model,
            "mode": core.policy.mode.value,
            "dirty": s.dirty,
            "viewer": {
                "enabled": core.viewer.enabled,
                "connected": core.viewer.connected,
                "url": core.viewer.url,
            },
        }
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Summary of the loaded IFC: schema, project name/description, "
            "units, counts of sites/buildings/storeys/spaces, entity counts for "
            "common classes, top materials and classifications, authoring-tool "
            "header info. Start here to orient yourself in a model."
        ),
    )
    @enveloped(core, "get_ifc_project_info")
    async def get_ifc_project_info() -> str:
        core.session.require_loaded()
        data = await core.session.run(
            lambda: build_project_info(core.session.ifc, core.session.path), timeout=120
        )
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] The spatial containment tree (Project→Site→Building→Storey→"
            "Space) with direct element counts per node. Use root_global_id and "
            "depth to zoom into a branch."
        ),
    )
    @enveloped(core, "get_spatial_structure")
    async def get_spatial_structure(
        root_global_id: Annotated[
            str | None, Field(description="Start node GlobalId; omit for IfcProject.")
        ] = None,
        depth: Annotated[int, Field(ge=1, le=20)] = 10,
        include_element_counts: bool = True,
    ) -> str:
        core.session.require_loaded()
        tree = await core.session.run(
            lambda: build_spatial_tree(
                core.session.ifc, root_global_id, depth, include_element_counts
            ),
            timeout=120,
        )
        return ok({"tree": tree}, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Find elements with the IfcOpenShell selector syntax and return "
            "one summary row each. Examples: `IfcWall` (all walls); `IfcWall, IfcSlab` "
            "(both); `IfcWall, material=concrete`; "
            "`IfcWall, Pset_WallCommon.FireRating=F30`; `IfcElement, Name=/W.*1/` "
            "(regex). Paginate with limit/offset; then use get_element or get_psets "
            "on interesting GlobalIds. On syntax errors the response includes a "
            "cheat-sheet."
        ),
    )
    @enveloped(core, "query_elements")
    async def query_elements(
        query: Annotated[str, Field(description="IfcOpenShell selector expression.")],
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
        fields: Annotated[
            list[str] | None,
            Field(
                description="Extra columns beyond global_id+class. "
                f"Allowed: {list(ALLOWED_FIELDS)}. Default: {list(DEFAULT_FIELDS)}."
            ),
        ] = None,
        order_by: Literal["class", "name", "storey"] = "class",
    ) -> str:
        core.session.require_loaded()
        _validate_subset(fields, ALLOWED_FIELDS, "fields")
        use_fields = tuple(fields) if fields else DEFAULT_FIELDS
        rows, total = await core.session.run(
            lambda: run_query(
                core.session.ifc,
                query,
                limit=limit,
                offset=offset,
                fields=use_fields,
                order_by=order_by,
            ),
            timeout=120,
        )
        return ok(
            {"rows": rows},
            core.session_meta(),
            char_limit=limit_,
            total=total,
            returned=len(rows),
            offset=offset,
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Full detail for up to 50 elements by GlobalId: direct "
            "attributes, property sets, quantity sets, materials, type, spatial "
            "container chain, and optionally openings/decomposition. Choose "
            "sections with `include` to save tokens."
        ),
    )
    @enveloped(core, "get_element")
    async def get_element(
        global_ids: Annotated[list[str], Field(min_length=1, max_length=50)],
        include: Annotated[
            list[str] | None,
            Field(
                description=f"Sections to include. Allowed: {list(INCLUDE_ALLOWED)}. "
                f"Default: {list(INCLUDE_DEFAULT)}."
            ),
        ] = None,
    ) -> str:
        core.session.require_loaded()
        _validate_subset(include, INCLUDE_ALLOWED, "include")
        use_include = tuple(include) if include else INCLUDE_DEFAULT

        def job() -> tuple[list[dict], list[str]]:
            found, missing = [], []
            for gid in global_ids:
                try:
                    e = core.session.ifc.by_guid(gid)
                except Exception:
                    e = None
                if e is None:
                    missing.append(gid)
                else:
                    found.append(element_detail(e, use_include))
            return found, missing

        elements, missing = await core.session.run(job, timeout=120)
        return ok(
            {"elements": elements, "missing": missing},
            core.session_meta(),
            char_limit=limit_,
            returned=len(elements),
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Property sets and quantity sets for up to 100 elements by "
            "GlobalId. Response preserves input order in `results`. Lighter than "
            "get_element when you only need psets."
        ),
    )
    @enveloped(core, "get_psets")
    async def get_psets(
        global_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        psets_only: bool = False,
        qtos_only: bool = False,
    ) -> str:
        core.session.require_loaded()
        if psets_only and qtos_only:
            raise ToolError(
                "INVALID_INPUT",
                "psets_only and qtos_only are mutually exclusive.",
                "Set at most one of them.",
            )

        def job() -> list[dict]:
            import ifcopenshell.util.element as element_util

            results = []
            for gid in global_ids:
                try:
                    e = core.session.ifc.by_guid(gid)
                except Exception:
                    e = None
                if e is None:
                    results.append({"global_id": gid, "found": False})
                    continue
                entry: dict[str, Any] = {
                    "global_id": gid,
                    "found": True,
                    "class": e.is_a(),
                    "name": getattr(e, "Name", None),
                }
                if not qtos_only:
                    entry["psets"] = element_util.get_psets(e, psets_only=True)
                if not psets_only:
                    entry["qtos"] = element_util.get_psets(e, qtos_only=True)
                results.append(entry)
            return results

        results = await core.session.run(job, timeout=120)
        return ok(
            {"results": results},
            core.session_meta(),
            char_limit=limit_,
            returned=len(results),
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Official IFC schema documentation for an entity (and "
            "optionally one attribute): definition text, attribute list with types "
            "and optionality, supertype chain, predefined-type values. Use before "
            "writing execute_ifc_code against unfamiliar classes. Works without a "
            "loaded model (defaults to IFC4)."
        ),
    )
    @enveloped(core, "get_schema_docs")
    async def get_schema_docs(
        entity: Annotated[str, Field(description="e.g. IfcWall")],
        attribute: Annotated[str | None, Field(description="Optional attribute name.")] = None,
    ) -> str:
        schema = core.session.schema if core.session.loaded else "IFC4"
        data = build_schema_docs(schema or "IFC4", entity, attribute)
        return ok(data, core.session_meta(), char_limit=limit_)
