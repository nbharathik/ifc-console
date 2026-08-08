"""MCP resources: ambient model context without spending a tool call.

Read-only JSON views over the same builders the tools use. Clients that
surface resources get the model summary, spatial tree, audit trail, and
per-element detail as attachable context.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ifc_console.ifc.elements import INCLUDE_DEFAULT, element_detail
from ifc_console.ifc.info import build_project_info
from ifc_console.ifc.spatial import build_spatial_tree
from ifc_console.mcp.compat import MCPServer
from ifc_console.mcp.envelope import dump

if TYPE_CHECKING:
    from ifc_console.app import AppCore

_NO_MODEL = {
    "loaded": False,
    "hint": "no model is open; call open_ifc_file or pick one in the ifc-console terminal",
}


def register(mcp: MCPServer, core: AppCore) -> None:
    @mcp.resource(
        "ifc://model/summary",
        name="model-summary",
        description="Project summary of the loaded IFC: schema, units, counts, materials.",
        mime_type="application/json",
    )
    async def model_summary() -> str:
        s = core.session
        if not s.loaded:
            return dump(_NO_MODEL)
        info, _ = await core.cached_read("project_info", lambda: build_project_info(s.ifc, s.path))
        return dump({"loaded": True, "revision": s.revision, **info})

    @mcp.resource(
        "ifc://model/spatial-tree",
        name="spatial-tree",
        description="The spatial containment tree with per-node element counts.",
        mime_type="application/json",
    )
    async def spatial_tree() -> str:
        s = core.session
        if not s.loaded:
            return dump(_NO_MODEL)
        tree, _ = await core.cached_read(
            "spatial_tree",
            lambda: build_spatial_tree(s.ifc, None, 10, True),
            key=(None, 10, True),
        )
        return dump({"loaded": True, "revision": s.revision, "tree": tree})

    @mcp.resource(
        "ifc://session/audit",
        name="session-audit",
        description="The last 50 audit records of this session (tool calls, saves, mode changes).",
        mime_type="application/json",
    )
    async def session_audit() -> str:
        return dump({"records": core.audit.tail(50)})

    @mcp.resource(
        "ifc://element/{global_id}",
        name="element",
        description="Full detail for one element by GlobalId: attributes, psets, type, container.",
        mime_type="application/json",
    )
    async def element(global_id: str) -> str:
        s = core.session
        if not s.loaded:
            return dump(_NO_MODEL)

        def job() -> dict | None:
            try:
                entity = s.ifc.by_guid(global_id)
            except Exception:
                return None
            if entity is None:
                return None
            return element_detail(entity, INCLUDE_DEFAULT)

        detail = await s.run(job, timeout=60)
        if detail is None:
            return dump({"found": False, "global_id": global_id})
        return dump({"found": True, **detail})
