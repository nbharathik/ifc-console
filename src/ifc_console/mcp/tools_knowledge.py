"""Knowledge tools: search the offline reference index, read API docs.

The index covers the IFC schema documentation, the property set templates,
every ifcopenshell.api function, and the verified recipe cookbook. It is built
from the installed ifcopenshell, so these tools never touch the network.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import Field

from ifc_console.mcp.compat import MCPServer, ToolAnnotations
from ifc_console.mcp.envelope import Envelope, ToolError, ok
from ifc_console.mcp.server import enveloped

if TYPE_CHECKING:
    from ifc_console.app import AppCore

KNOWLEDGE_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

_NOT_READY = (
    "The reference index is still building (it takes a few seconds on first "
    "use). Retry shortly, or use get_schema_docs, which needs no index."
)


def _require(core: AppCore) -> None:
    if not core.settings.knowledge.enabled:
        raise ToolError(
            "KNOWLEDGE_DISABLED",
            "the reference index is turned off.",
            "The user can turn it on with /settings knowledge.enabled true.",
        )
    if not core.knowledge.ready:
        core.start_knowledge()
        raise ToolError("KNOWLEDGE_NOT_READY", "the reference index is not built yet.", _NOT_READY)


def register(mcp: MCPServer, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=KNOWLEDGE_ANN,
        description=(
            "[QUERY] Search the offline IFC reference: schema entities, property "
            "sets and their properties, every ifcopenshell.api function, and "
            "verified code recipes. Ask in plain words ('which property set "
            "carries fire rating', 'how do I assign a material', 'IfcWall'). "
            "Returns ranked hits with a key; call get_knowledge_record for the "
            "full text. Use this before writing execute_ifc_code so the API "
            "names and property names are real ones."
        ),
    )
    @enveloped(core, "search_ifc_knowledge")
    async def search_ifc_knowledge(
        query: Annotated[str, Field(description="What you want to know, in plain words.")],
        kind: Annotated[
            list[Literal["entity", "pset", "property", "type", "api", "recipe"]] | None,
            Field(description="Restrict to these record kinds."),
        ] = None,
        schema: Annotated[
            str | None,
            Field(description="IFC2X3, IFC4, or IFC4X3. Defaults to the loaded model."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Maximum hits.")] = 10,
    ) -> Envelope:
        _require(core)
        kinds = tuple(kind) if kind else None
        target = schema or (core.session.schema if core.session.loaded else None)
        hits = core.knowledge.search(
            query, kind=kinds, schema=_normalize_schema(target), limit=limit
        )
        return ok(
            {"query": query, "hits": hits},
            core.session_meta(),
            char_limit=limit_,
            returned=len(hits),
        )

    @mcp.tool(
        annotations=KNOWLEDGE_ANN,
        description=(
            "[QUERY] Full text of one knowledge record by its key, as returned "
            "by search_ifc_knowledge (for example api:pset.add_pset, "
            "recipe:rename-elements, entity:IFC4:IfcWall)."
        ),
    )
    @enveloped(core, "get_knowledge_record")
    async def get_knowledge_record(
        key: Annotated[str, Field(description="The key from a search hit.")],
    ) -> Envelope:
        _require(core)
        record = core.knowledge.get(key)
        if record is None:
            raise ToolError(
                "NOT_FOUND",
                f"no knowledge record with key {key!r}.",
                "Keys come from search_ifc_knowledge hits; run a search first.",
            )
        return ok(record, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=KNOWLEDGE_ANN,
        description=(
            "[QUERY] Documentation for an ifcopenshell.api function: the exact "
            "call signature, argument meanings, and usage notes. Name it as "
            "module.function, e.g. 'pset.add_pset' or 'root.create_entity'. "
            "Omit the name to list the modules. Inside execute_ifc_code these "
            "are reachable as ifc_api.<module>.<function>(ifc, ...) and only in "
            "edit mode."
        ),
    )
    @enveloped(core, "get_api_docs")
    async def get_api_docs(
        function: Annotated[
            str | None,
            Field(description="module.function, e.g. pset.add_pset."),
        ] = None,
        search: Annotated[
            str | None,
            Field(description="Plain-words search when you do not know the name."),
        ] = None,
    ) -> Envelope:
        _require(core)
        if not function and not search:
            modules: dict[str, int] = {}
            for hit in core.knowledge.search("api", kind="api", limit=500):
                modules[hit["name"].split(".")[0]] = modules.get(hit["name"].split(".")[0], 0) + 1
            return ok(
                {"modules": sorted(modules), "hint": "call again with function='module.name'"},
                core.session_meta(),
                char_limit=limit_,
            )
        if search and not function:
            hits = core.knowledge.search(search, kind="api", limit=10)
            return ok(
                {"search": search, "hits": hits},
                core.session_meta(),
                char_limit=limit_,
                returned=len(hits),
            )
        record = core.knowledge.get(f"api:{function}")
        if record is None:
            hits = core.knowledge.search(function or "", kind="api", limit=5)
            raise ToolError(
                "NOT_FOUND",
                f"no ifcopenshell.api function named {function!r}.",
                "Closest matches: "
                + (", ".join(h["name"] for h in hits) or "none")
                + ". Names look like pset.add_pset or root.create_entity.",
            )
        data: dict[str, Any] = dict(record)
        data["usage"] = (
            f"ifc_api.{record['name']}(ifc, ...)  # edit mode only; ask mode blocks ifc_api"
        )
        return ok(data, core.session_meta(), char_limit=limit_)


def _normalize_schema(schema: str | None) -> str | None:
    """Map a file's schema identifier onto the three documented families."""
    if not schema:
        return None
    text = schema.upper().replace(" ", "")
    for known in ("IFC4X3", "IFC2X3", "IFC4"):
        if text.startswith(known):
            return known
    return None
