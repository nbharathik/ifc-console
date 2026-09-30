"""find_tools and call_tool: reach any operation without listing them all.

The lean profile lists a small core plus these two. A model asks find_tools in
plain words, reads the argument shapes it returns, and runs the tool with
call_tool. Nothing here widens what a model may do: call_tool goes through the
same OperationService as every other call, so mode, capabilities and audit
apply to the tool it runs, and it refuses tools that delete or overwrite so
that a client's approval prompt always sees those by name.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.mcp.catalog import DISCOVERY_TOOLS, ToolIndex, compact_schema

if TYPE_CHECKING:
    from ifc_console.app import AppCore

FIND_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
CALL_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

_FIND_DESCRIPTION = (
    "[QUERY] Find tools by what you want to do, in plain words. Returns the best "
    "matches with their argument shapes; run one with call_tool."
)
_CALL_DESCRIPTION = (
    "[EDIT-capable] Run a tool that find_tools returned, by name. Mode and "
    "permissions apply to that tool exactly as if you called it directly. Tools "
    "that delete or overwrite are not run here."
)


def register(mcp: OperationRegistry, core: AppCore) -> None:
    cached: dict[str, Any] = {"size": -1, "index": None}

    def index() -> ToolIndex:
        # Extensions and the viewer tools register after startup, so the index
        # follows the registry rather than being built once.
        definitions = core.operation_service.definitions()
        if cached["size"] != len(definitions):
            cached["index"] = ToolIndex(definitions)
            cached["size"] = len(definitions)
        return cached["index"]

    @mcp.tool(
        annotations=FIND_ANN,
        description=_FIND_DESCRIPTION,
        required_capabilities=(Capability.MODEL_READ,),
    )
    @enveloped(core, "find_tools")
    async def find_tools(
        query: Annotated[str, Field(min_length=1, max_length=300, description="What you want to do.")],
        limit: Annotated[int, Field(ge=1, le=15)] = 6,
    ) -> Envelope:
        hits = index().search(query, limit=limit, exclude=DISCOVERY_TOOLS)
        tools = []
        for card in hits:
            entry: dict[str, Any] = {"name": card.name, "summary": card.summary}
            entry.update(compact_schema(card.schema))
            if card.destructive:
                entry["direct_only"] = True
            elif not card.read_only:
                entry["edits"] = True
            tools.append(entry)
        data: dict[str, Any] = {"query": query, "tools": tools}
        if not tools:
            data["hint"] = "no match; try other words, or describe the result you want"
        else:
            data["use"] = "call_tool(name, arguments={...})"
        return ok(data, core.session_meta(), char_limit=lambda: core.settings.exec.output_char_limit)

    @mcp.tool(
        annotations=CALL_ANN,
        description=_CALL_DESCRIPTION,
        # The tool that runs is checked on its own; this only needs to be reachable.
        required_capabilities=(Capability.MODEL_READ,),
    )
    @enveloped(core, "call_tool")
    async def call_tool(
        name: Annotated[str, Field(min_length=1, max_length=80, description="A name from find_tools.")],
        arguments: Annotated[
            dict[str, Any] | None, Field(description="The tool's arguments, as find_tools showed them.")
        ] = None,
    ) -> Any:
        if name in DISCOVERY_TOOLS:
            raise ToolError(
                "INVALID_INPUT",
                f"{name} is not run through call_tool.",
                "Call find_tools or call_tool directly.",
            )
        card = index().cards.get(name)
        if card is None:
            raise ToolError(
                "NOT_FOUND",
                f"no tool named {name!r}.",
                "Use find_tools to search, and pass a name exactly as it returns it.",
            )
        if card.destructive:
            raise ToolError(
                "CAPABILITY_DENIED",
                f"{name} can delete or overwrite, so it is not run through call_tool.",
                f"Call {name} directly if your client lists it; otherwise ask the user "
                "to run /tools profile full, which lists every tool.",
            )
        return await core.operation_service.call(name, dict(arguments or {}))
