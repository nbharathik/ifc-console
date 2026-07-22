"""Viewer tools: selection, highlight, screenshot.

This is the optional tool category: it is registered only while the viewer
is enabled (AppCore._sync_viewer_tools adds and removes it as /viewer
toggles), so sessions without the viewer expose the lean 11-tool core.

All three need at least one connected browser tab; without one they return
VIEWER_NOT_CONNECTED with a hint that tells the LLM how the user can start
the viewer. They are visual-only: none of them can mutate the model, so they
are allowed in every session mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import Field

from ifc_console.ifc.query import DEFAULT_FIELDS, element_row
from ifc_console.mcp.envelope import ok
from ifc_console.mcp.server import enveloped

if TYPE_CHECKING:
    from ifc_console.app import AppCore

VIEW_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

# The names this module registers; AppCore uses them to unregister on
# /viewer off. Keep in sync with the @mcp.tool functions below.
TOOL_NAMES = ("get_viewer_selection", "highlight_elements", "get_viewer_screenshot")


def register(mcp: FastMCP, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=VIEW_ANN,
        description=(
            "[QUERY] GlobalIds the user has click-selected in the web viewer, with "
            "brief element info. The human's way of pointing at things - check it "
            "when the user says 'this wall' or 'the selected elements'. Requires "
            "the viewer (see get_session_status.viewer)."
        ),
    )
    @enveloped(core, "get_viewer_selection")
    async def get_viewer_selection() -> str:
        hub = core.viewer_hub
        hub.require_connected()
        guids = list(hub.selection)

        def job() -> tuple[list[dict], list[str]]:
            rows, missing = [], []
            for gid in guids:
                try:
                    entity = core.session.ifc.by_guid(gid)
                except Exception:
                    entity = None
                if entity is None:
                    missing.append(gid)
                else:
                    rows.append(element_row(entity, DEFAULT_FIELDS))
            return rows, missing

        rows: list[dict] = []
        missing: list[str] = []
        if guids and core.session.loaded:
            rows, missing = await core.session.run(job, timeout=60)
        data = {
            "connected": hub.connected,
            "guids": guids,
            "elements": rows,
            "missing": missing,
            "selected_at": hub.selected_at,
        }
        if not guids:
            data["note"] = "nothing is selected; ask the user to click elements in the viewer"
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=VIEW_ANN,
        description=(
            "[VIEW] Colour-highlight elements in the web viewer (your way of "
            "pointing at things for the user). Optionally isolate (hide everything "
            "else) and zoom to fit; clear=true resets all highlights. Requires the "
            "viewer."
        ),
    )
    @enveloped(core, "highlight_elements")
    async def highlight_elements(
        global_ids: Annotated[list[str] | None, Field(max_length=500)] = None,
        color: Annotated[str, Field(pattern="^#[0-9a-fA-F]{6}$")] = "#ff3b30",
        isolate: bool = False,
        fit: bool = True,
        clear: bool = False,
    ) -> str:
        hub = core.viewer_hub
        hub.require_connected()
        if clear:
            await hub.send_highlight([], color=color, isolate=False, fit=False, clear=True)
            return ok(
                {"highlighted": 0, "missing": [], "cleared": True},
                core.session_meta(),
                char_limit=limit_,
            )
        guids = list(dict.fromkeys(global_ids or []))  # dedupe, keep order
        core.session.require_loaded()

        def job() -> tuple[list[str], list[str]]:
            found, missing = [], []
            for gid in guids:
                try:
                    entity = core.session.ifc.by_guid(gid)
                except Exception:
                    entity = None
                (missing if entity is None else found).append(gid)
            return found, missing

        found, missing = await core.session.run(job, timeout=60)
        await hub.send_highlight(found, color=color, isolate=isolate, fit=fit, clear=False)
        data: dict[str, Any] = {"highlighted": len(found), "missing": missing}
        if not found:
            data["note"] = "no valid GlobalIds to highlight; nothing changed in the viewer"
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=VIEW_ANN,
        structured_output=False,
        description=(
            "[QUERY] Capture the web-viewer canvas as an image (returned inline), "
            "optionally setting a view preset and fit first. Requires the viewer. "
            "Use highlight_elements + this to visually verify claims about the "
            "model."
        ),
    )
    @enveloped(core, "get_viewer_screenshot")
    async def get_viewer_screenshot(
        view: Literal[
            "top", "bottom", "front", "back", "left", "right", "iso", "current"
        ] = "current",
        fit: Literal["all", "selection", "highlighted"] | None = None,
        max_size: Annotated[int, Field(ge=64, le=2048)] = 800,
        format: Literal["jpeg", "png"] = "jpeg",
        quality: Annotated[int, Field(ge=1, le=100)] = 85,
    ) -> Any:
        hub = core.viewer_hub
        hub.require_connected()
        data, width, height = await hub.request_screenshot(
            view=view, fit=fit, max_size=max_size, format=format, quality=quality
        )
        note = (
            f"viewer screenshot {width}x{height} {format} ({len(data)} bytes); "
            "if no image is visible above, your client dropped the image content"
        )
        return [Image(data=data, format=format), note]
