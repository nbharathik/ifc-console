"""Read-only query tools: session status, project info, spatial tree,
selector queries, element detail, psets, schema docs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from ifc_console import __version__
from ifc_console.application.operations import enveloped
from ifc_console.core.operation_data import (
    QueryElementsData,
    SearchElementsData,
    SessionStatusData,
)
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.ifc.elements import INCLUDE_ALLOWED, INCLUDE_DEFAULT, element_detail
from ifc_console.ifc.info import build_project_info
from ifc_console.ifc.query import (
    ALLOWED_FIELDS,
    DEFAULT_FIELDS,
    element_row,
    run_query,
    unknown_classes,
)
from ifc_console.ifc.query import (
    search_elements as search_model_elements,
)
from ifc_console.ifc.schema_docs import build_pset_docs, build_schema_docs, find_property
from ifc_console.ifc.spatial import build_spatial_tree

if TYPE_CHECKING:
    from ifc_console.app import AppCore

QUERY_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

MODEL_ARG = "model_id of an attached model (see list_models); omit for the active model."


def read_meta(core: AppCore, session: Any) -> dict[str, Any]:
    """`read_from` only when a read targeted something other than the active
    model; meta.model keeps meaning the active model, always."""
    if session is core.session:
        return {}
    return {"read_from": session.model_id}


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


def register(mcp: OperationRegistry, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=QUERY_ANN,
        data_model=SessionStatusData,
        description=(
            "[QUERY] Return server/session state: loaded model (name, path, schema, "
            "size), mode (ask = query-only, edit = mutations allowed), "
            "unsaved-changes flag, fingerprint, viewer connection state, server "
            "version. Call this first if unsure about session state."
        ),
    )
    @enveloped(core, "get_session_status")
    async def get_session_status() -> Envelope:
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
            "[QUERY] One-call orientation: session status, project summary, and "
            "the spatial tree to depth 2, in a single round-trip. Start here on "
            "a fresh connection; it replaces three separate calls."
        ),
    )
    @enveloped(core, "orient")
    async def orient() -> Envelope:
        s = core.session
        status: dict[str, Any] = {
            "model": {"loaded": s.loaded, "name": s.name, "schema": s.schema},
            "mode": core.policy.mode.value,
            "dirty": s.dirty,
            "viewer": {"enabled": core.viewer.enabled, "connected": core.viewer.connected},
        }
        # Attached models and companion files are session state the LLM cannot
        # guess; surface them here rather than making it call list_models.
        if len(core.models.sessions) > 1 or core.models.attachments:
            status["workspace"] = {
                "active": core.models.active_id,
                "attached_models": core.models.attached_ids,
                "attachments": [a.to_dict() for a in core.models.attachments.values()],
            }
        data: dict[str, Any] = {"status": status}
        if s.loaded:

            def job() -> tuple[dict, dict]:
                return (
                    build_project_info(s.ifc, s.path),
                    build_spatial_tree(s.ifc, None, 2, True),
                )

            (info, tree), cached = await core.cached_read("orient", job)
            data["project"] = info
            data["spatial_tree"] = tree
            return ok(data, core.session_meta(), char_limit=limit_, cached=cached)
        data["hint"] = "no model loaded; call list_ifc_files then open_ifc_file"
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Live capability map: every currently registered tool with a "
            "one-line purpose, the session mode and what it permits, viewer "
            "state, and worked examples. The tool list changes at runtime (the "
            "viewer category comes and goes), so this is the ground truth."
        ),
    )
    @enveloped(core, "describe_capabilities")
    async def describe_capabilities() -> Envelope:
        def purpose(description: str | None) -> str:
            text = description or ""
            if text.startswith("["):  # drop the [QUERY]/[VIEW]/[ARTIFACT] tag
                _, _, text = text.partition("] ")
            return text.partition(". ")[0]

        tools = sorted(await mcp.list_tools(), key=lambda t: t.name)
        listing = []
        for tool in tools:
            decision = core.policy.evaluate(list(tool.required_capabilities))
            listing.append(
                {
                    "name": tool.name,
                    "purpose": purpose(tool.description),
                    "required_capabilities": [
                        item.value for item in tool.required_capabilities
                    ],
                    "permitted": decision.allowed,
                }
            )
        mode = core.policy.mode.value
        ai_save = core.policy.allow_ai_save
        data = {
            "server": {"name": "ifc-console", "version": __version__},
            "mode": {
                "current": mode,
                "meaning": (
                    "queries only; mutations and saves are blocked"
                    if mode == "ask"
                    else (
                        "mutations run; AI saves are enabled and make automatic backups"
                        if ai_save
                        else "mutations stay in memory; only the user can save or discard them"
                    )
                ),
                "note": (
                    "only the user can change the mode or AI-save setting in their terminal"
                ),
                "ai_save_allowed": ai_save,
                "profile": f"{mode}:tool",
                "granted_capabilities": [
                    item.value for item in core.policy.granted_capabilities()
                ],
            },
            "viewer": {
                "enabled": core.viewer.enabled,
                "connected": core.viewer.connected,
                "note": "viewer tools join the tool list only while the viewer is on",
            },
            "tools": listing,
            "examples": [
                'query_elements(query="IfcWall, Pset_WallCommon.FireRating=F30")',
                'compute_quantities(selector="IfcSlab", aggregate_by="storey")',
                'validate_ids(ids_path="requirements.ids")',
                'find_files(query="architecture", kinds=["ifc"])',
                'export_csv(selector="IfcDoor", path="doors.csv", '
                'properties=["Pset_DoorCommon.FireRating"])',
            ],
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
    async def get_ifc_project_info(
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        data, cached = await core.cached_read(
            "project_info",
            lambda: build_project_info(s.ifc, s.path),
            session=s,
        )
        return ok(data, core.session_meta(), char_limit=limit_, cached=cached, **read_meta(core, s))

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
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        tree, cached = await core.cached_read(
            "spatial_tree",
            lambda: build_spatial_tree(s.ifc, root_global_id, depth, include_element_counts),
            key=(root_global_id, depth, include_element_counts),
            session=s,
        )
        return ok(
            {"tree": tree},
            core.session_meta(),
            char_limit=limit_,
            cached=cached,
            **read_meta(core, s),
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        data_model=SearchElementsData,
        description=(
            "[QUERY] Resolve a human reference to IFC elements without making the "
            "model write selector syntax. A name, partial name, class, type name, or "
            "GlobalId is accepted. Bare IFC classes and expressions containing '=' "
            "use IfcOpenShell selector syntax; other input is a case-insensitive text "
            "search. Use get_viewer_selection instead when the human points at the 3D view."
        ),
    )
    @enveloped(core, "search_elements")
    async def search_elements(
        term: Annotated[str, Field(min_length=1, max_length=500)],
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)

        def job() -> tuple[list[dict[str, Any]], str, int]:
            matches, mode = search_model_elements(s.ifc, term)
            rows = [element_row(item, DEFAULT_FIELDS) for item in matches[:limit]]
            return rows, mode, len(matches)

        rows, mode, total = await s.run(job, timeout=120)
        return ok(
            {"query": term, "mode": mode, "results": rows},
            core.session_meta(),
            char_limit=limit_,
            total=total,
            returned=len(rows),
            **read_meta(core, s),
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        data_model=QueryElementsData,
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
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        _validate_subset(fields, ALLOWED_FIELDS, "fields")
        use_fields = tuple(fields) if fields else DEFAULT_FIELDS
        rows, total = await s.run(
            lambda: run_query(
                s.ifc,
                query,
                limit=limit,
                offset=offset,
                fields=use_fields,
                order_by=order_by,
            ),
            timeout=120,
        )
        data: dict[str, Any] = {"rows": rows}
        if not total:
            # An empty result and a misspelled class look identical; say which.
            unknown = await s.run(lambda: unknown_classes(s.ifc, query), timeout=30)
            if unknown:
                data["unknown_classes"] = unknown
                data["schema"] = s.schema
                data["note"] = (
                    f"{', '.join(unknown)} is not defined in {s.schema}; check the "
                    "spelling with get_schema_docs."
                )
        return ok(
            data,
            core.session_meta(),
            char_limit=limit_,
            total=total,
            returned=len(rows),
            offset=offset,
            **read_meta(core, s),
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
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
        _validate_subset(include, INCLUDE_ALLOWED, "include")
        use_include = tuple(include) if include else INCLUDE_DEFAULT

        def job() -> tuple[list[dict], list[str]]:
            found, missing = [], []
            for gid in global_ids:
                try:
                    e = s.ifc.by_guid(gid)
                except Exception:
                    e = None
                if e is None:
                    missing.append(gid)
                else:
                    found.append(element_detail(e, use_include))
            return found, missing

        elements, missing = await s.run(job, timeout=120)
        return ok(
            {"elements": elements, "missing": missing},
            core.session_meta(),
            char_limit=limit_,
            returned=len(elements),
            **read_meta(core, s),
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
        model: Annotated[str | None, Field(description=MODEL_ARG)] = None,
    ) -> Envelope:
        s = core.resolve_session(model)
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
                    e = s.ifc.by_guid(gid)
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

        results = await s.run(job, timeout=120)
        return ok(
            {"results": results},
            core.session_meta(),
            char_limit=limit_,
            returned=len(results),
            **read_meta(core, s),
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Official IFC schema documentation, three ways. `entity` "
            "gives the definition, attributes with types and optionality, the "
            "supertype chain, predefined-type values, and the property sets that "
            "apply to it. `pset` gives one property set: every property with its "
            "data type, enumerated values, and which entities it applies to. "
            "`property` is the reverse lookup, naming the property sets that "
            "define a property such as FireRating. Use before writing "
            "execute_ifc_code or a selector against unfamiliar classes. Works "
            "without a loaded model (defaults to IFC4)."
        ),
    )
    @enveloped(core, "get_schema_docs")
    async def get_schema_docs(
        entity: Annotated[str | None, Field(description="e.g. IfcWall")] = None,
        attribute: Annotated[str | None, Field(description="Optional attribute name.")] = None,
        pset: Annotated[
            str | None,
            Field(description="Property or quantity set name, e.g. Pset_WallCommon."),
        ] = None,
        property: Annotated[
            str | None,
            Field(description="Property name to look up, e.g. FireRating."),
        ] = None,
        schema: Annotated[
            str | None,
            Field(description="IFC2X3, IFC4, or IFC4X3. Defaults to the loaded model."),
        ] = None,
    ) -> Envelope:
        target = schema or (core.session.schema if core.session.loaded else "IFC4") or "IFC4"
        if not (entity or pset or property):
            raise ToolError(
                "INVALID_INPUT",
                "name one of entity, pset, or property.",
                "get_schema_docs(entity='IfcWall'), get_schema_docs(pset='Pset_WallCommon'), "
                "or get_schema_docs(property='FireRating').",
            )
        data: dict[str, Any] = {}
        if entity:
            data.update(build_schema_docs(target, entity, attribute))
        if pset:
            data["property_set"] = build_pset_docs(target, pset)
        if property:
            data["property_lookup"] = find_property(target, property)
        return ok(data, core.session_meta(), char_limit=limit_)
