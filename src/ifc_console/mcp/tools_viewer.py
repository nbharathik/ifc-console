"""Viewer tools: selection, highlight, screenshot.

This is the optional tool category: it is registered only while the viewer
is enabled (AppCore._sync_viewer_tools adds and removes it as /viewer
toggles), so sessions without the viewer expose the lean 24-tool core.

All four need at least one connected browser tab; without one they return
VIEWER_NOT_CONNECTED with a hint that tells the LLM how the user can start
the viewer. They are visual-only: none of them can mutate the model, so they
are allowed in every session mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from ifc_console.branding import categorical_color
from ifc_console.ifc.query import DEFAULT_FIELDS, element_row
from ifc_console.mcp.compat import Image, MCPServer, ToolAnnotations
from ifc_console.mcp.envelope import Envelope, ToolError, ok
from ifc_console.mcp.server import enveloped

if TYPE_CHECKING:
    from ifc_console.app import AppCore

VIEW_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)

# The names this module registers; AppCore uses them to unregister on
# /viewer off. Keep in sync with the @mcp.tool functions below.
TOOL_NAMES = (
    "get_viewer_selection",
    "highlight_elements",
    "apply_color_theme",
    "get_viewer_screenshot",
)


class ColorGroup(BaseModel):
    """One legend entry: a label, its elements, and an optional color."""

    label: str = Field(description="Legend label, e.g. 'F30' or 'Level 2'.")
    global_ids: list[str] = Field(min_length=1, max_length=5000)
    color: str | None = Field(
        default=None,
        pattern="^#[0-9a-fA-F]{6}$",
        description="Optional #rrggbb; a colorblind-safe palette color is assigned when omitted.",
    )


def register(mcp: MCPServer, core: AppCore) -> None:
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
    @core.model_lifecycle_operation
    async def get_viewer_selection() -> Envelope:
        hub = core.viewer_hub
        hub.require_connected()
        guids = list(hub.selection)
        session = core.session
        if hub.selection_model_id:
            selected_session = core.models.get(hub.selection_model_id)
            if selected_session is not None:
                session = selected_session

        def job() -> tuple[list[dict], list[str]]:
            rows, missing = [], []
            for gid in guids:
                try:
                    entity = session.ifc.by_guid(gid)
                except Exception:
                    entity = None
                if entity is None:
                    missing.append(gid)
                else:
                    rows.append(element_row(entity, DEFAULT_FIELDS))
            return rows, missing

        rows: list[dict] = []
        missing: list[str] = []
        if guids and session.loaded:
            rows, missing = await session.run(job, timeout=60)
        data = {
            "connected": hub.connected,
            "model_id": session.model_id,
            "guids": guids,
            "elements": rows,
            "missing": missing,
            "selected_at": hub.selected_at,
        }
        if not guids:
            data["note"] = "nothing is selected; ask the user to click elements in the viewer"
        extra_meta = {} if session is core.session else {"read_from": session.model_id}
        return ok(data, core.session_meta(), char_limit=limit_, **extra_meta)

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
    @core.model_lifecycle_operation
    async def highlight_elements(
        global_ids: Annotated[list[str] | None, Field(max_length=500)] = None,
        color: Annotated[str, Field(pattern="^#[0-9a-fA-F]{6}$")] = "#ff3b30",
        isolate: bool = False,
        fit: bool = True,
        clear: bool = False,
    ) -> Envelope:
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
        session = core.session
        session.require_loaded()

        def job() -> tuple[list[str], list[str]]:
            found, missing = [], []
            for gid in guids:
                try:
                    entity = session.ifc.by_guid(gid)
                except Exception:
                    entity = None
                (missing if entity is None else found).append(gid)
            return found, missing

        found, missing = await session.run(job, timeout=60)
        if not found:
            # Broadcasting an empty set would clear the current highlight, which
            # is the opposite of "nothing changed".
            return ok(
                {
                    "highlighted": 0,
                    "missing": missing,
                    "note": "no valid GlobalIds for the active model; nothing "
                    "changed in the viewer. Ids from an attached model must be "
                    "highlighted after set_active_model.",
                },
                core.session_meta(),
                char_limit=limit_,
            )
        await hub.send_highlight(found, color=color, isolate=isolate, fit=fit, clear=False)
        return ok(
            {"highlighted": len(found), "missing": missing},
            core.session_meta(),
            char_limit=limit_,
        )

    @mcp.tool(
        annotations=VIEW_ANN,
        description=(
            "[VIEW] Paint viewer elements by group with a legend: you compute the "
            "grouping (by storey, type, material, a pset value, pass/fail, "
            "anything), the viewer colors it, the user reads it. Colors come from "
            "a colorblind-safe palette unless a group sets its own. clear=true "
            "removes the theme. Requires the viewer."
        ),
    )
    @enveloped(core, "apply_color_theme")
    @core.model_lifecycle_operation
    async def apply_color_theme(
        groups: Annotated[
            list[ColorGroup] | None,
            Field(max_length=24, description="The groups to paint, in legend order."),
        ] = None,
        title: Annotated[str, Field(max_length=80, description="Legend title.")] = "",
        clear: bool = False,
    ) -> Envelope:
        hub = core.viewer_hub
        hub.require_connected()
        if clear:
            await hub.send_color_theme([], title="", clear=True)
            return ok({"cleared": True}, core.session_meta(), char_limit=limit_)
        if not groups:
            raise ToolError(
                "INVALID_INPUT",
                "groups is required unless clear=true.",
                "Pass groups=[{label, global_ids, color?}] or clear=true.",
            )
        session = core.session
        session.require_loaded()
        wanted = [list(dict.fromkeys(g.global_ids)) for g in groups]

        def job() -> tuple[list[list[str]], list[str]]:
            resolved, missing = [], []
            for gids in wanted:
                found = []
                for gid in gids:
                    try:
                        entity = session.ifc.by_guid(gid)
                    except Exception:
                        entity = None
                    (found if entity is not None else missing).append(gid)
                resolved.append(found)
            return resolved, missing

        resolved, missing = await session.run(job, timeout=60)
        if not any(resolved):
            # An all-empty theme would wipe the current one; say so instead.
            return ok(
                {
                    "title": title,
                    "legend": [],
                    "painted": 0,
                    "missing": missing[:50],
                    "note": "no valid GlobalIds for the active model; nothing was "
                    "painted and the existing theme is unchanged. Ids from an "
                    "attached model need set_active_model first.",
                },
                core.session_meta(),
                char_limit=limit_,
            )
        frame_groups, legend = [], []
        for index, group in enumerate(groups):
            color = group.color or categorical_color(index)
            frame_groups.append(
                {"label": group.label, "color": color, "guids": resolved[index]}
            )
            legend.append(
                {"label": group.label, "color": color, "count": len(resolved[index])}
            )
        await hub.send_color_theme(frame_groups, title=title, clear=False)
        data: dict[str, Any] = {
            "title": title,
            "legend": legend,
            "painted": sum(len(r) for r in resolved),
            "missing": missing[:50],
        }
        if not data["painted"]:
            data["note"] = "no valid GlobalIds; nothing was painted"
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
