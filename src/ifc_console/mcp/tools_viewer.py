"""Viewer tools: selection, highlight, screenshot, viewport control.

This is the optional tool category: it is registered only while the viewer
is enabled (AppCore._sync_viewer_tools adds and removes it as /viewer
toggles), so sessions without the viewer expose the lean core tool set.

All of them need at least one connected browser tab; without one they return
VIEWER_NOT_CONNECTED with a hint that tells the LLM how the user can start
the viewer. They are visual-only: none of them can mutate the model, so they
are allowed in every session mode.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from ifc_console.application.operations import enveloped
from ifc_console.branding import categorical_color
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationImage, OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.ifc.geometry import selected
from ifc_console.ifc.query import DEFAULT_FIELDS, element_row

if TYPE_CHECKING:
    from ifc_console.app import AppCore

VIEW_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
LAUNCH_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

# Registered unconditionally (register_launcher), so an MCP client can turn
# the viewer on itself; never in TOOL_NAMES, so /viewer off keeps it.
LAUNCHER_TOOL = "open_viewer"

# The names this module registers; AppCore uses them to unregister on
# /viewer off. Keep in sync with the @mcp.tool functions below.
TOOL_NAMES = (
    "get_viewer_selection",
    "get_viewer_measurements",
    "highlight_elements",
    "apply_color_theme",
    "get_viewer_screenshot",
    "control_viewer",
)

VIEWER_ACTIONS = (
    "context",
    "set_view",
    "set_camera",
    "fit",
    "set_projection",
    "section",
    "select",
    "isolate",
    "hide",
    "show_all",
    "focus",
    "unfocus",
    "measure_elements",
    "measure_clearance",
    "measure_points",
    "clear_measurements",
    "save_view",
    "restore_view",
    "list_views",
)

# The actions that address elements, so a selector may stand in for the ids.
SELECTOR_ACTIONS = ("select", "isolate", "hide", "focus", "fit")

# A selector is meant to replace pasting ids, so it is not held to the guids
# cap; this only stops a runaway match from becoming a megabyte-wide frame.
MAX_SELECTOR_ELEMENTS = 20_000

# Camera fields the viewer understands; anything else is a typo worth naming.
_CAMERA_KEYS = ("position", "target", "up", "fov", "projection", "transition")
_CAMERA_VECTORS = ("position", "target", "up")
# Below this the look direction is numerically meaningless.
_MIN_CAMERA_SPAN = 0.001


class ColorGroup(BaseModel):
    """One legend entry: a label, its elements, and an optional color."""

    label: str = Field(description="Legend label, e.g. 'F30' or 'Level 2'.")
    global_ids: list[str] = Field(min_length=1, max_length=5000)
    color: str | None = Field(
        default=None,
        pattern="^#[0-9a-fA-F]{6}$",
        description="Optional #rrggbb; a colorblind-safe palette color is assigned when omitted.",
    )


def _vector(value: Any, key: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ToolError(
            "INVALID_INPUT",
            f"camera.{key} must be [x, y, z].",
            "Coordinates are metres in the model's own axes, z up.",
        )
    out = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ToolError(
                "INVALID_INPUT",
                f"camera.{key} must be three numbers.",
                "Coordinates are metres in the model's own axes, z up.",
            )
        component = float(component)
        if not math.isfinite(component):
            raise ToolError(
                "INVALID_INPUT",
                f"camera.{key} contains a value that is not a finite number.",
                "Coordinates are metres in the model's own axes, z up.",
            )
        out.append(component)
    return out


def _camera_params(camera: Any) -> dict:
    """Validate a camera object here, so a bad one is refused in our words."""
    if not isinstance(camera, dict) or not camera:
        raise ToolError(
            "INVALID_INPUT",
            "set_camera needs a camera object.",
            "Pass camera={'position': [x, y, z], 'target': [x, y, z]}; metres "
            "in the model's own axes, z up. control_viewer(action='context') "
            "returns the current camera in the same shape.",
        )
    unknown = [key for key in camera if key not in _CAMERA_KEYS]
    if unknown:
        raise ToolError(
            "INVALID_INPUT",
            f"camera has unknown field(s): {', '.join(sorted(unknown))}.",
            f"Use only: {', '.join(_CAMERA_KEYS)}.",
        )
    params: dict = {}
    for key in _CAMERA_VECTORS:
        if camera.get(key) is not None:
            params[key] = _vector(camera[key], key)
    if "position" in params and "target" in params:
        span = math.dist(params["position"], params["target"])
        if span < _MIN_CAMERA_SPAN:
            raise ToolError(
                "INVALID_INPUT",
                f"camera position and target are {span:.4f} m apart, which is no direction.",
                "Put the camera back from what it looks at; both are metres in "
                "the model's own axes.",
            )
    fov = camera.get("fov")
    if fov is not None:
        if isinstance(fov, bool) or not isinstance(fov, (int, float)) or not 1 <= fov <= 179:
            raise ToolError(
                "INVALID_INPUT",
                "camera.fov must be between 1 and 179 degrees.",
                "Around 50 is a natural perspective; fov is ignored in "
                "orthographic projection.",
            )
        params["fov"] = float(fov)
    projection = camera.get("projection")
    if projection is not None:
        if projection not in ("perspective", "orthographic"):
            raise ToolError(
                "INVALID_INPUT",
                f"camera.projection {projection!r} is not a projection.",
                "Pass 'perspective' or 'orthographic'.",
            )
        params["projection"] = projection
    if not params:
        raise ToolError(
            "INVALID_INPUT",
            "camera says nothing to change.",
            "Give at least one of position, target, up, fov or projection.",
        )
    if camera.get("transition") is not None:
        params["transition"] = bool(camera["transition"])
    return params


def _viewer_call(action: str, **kw: Any) -> tuple[str, dict]:
    """Translate one control_viewer call into a viewer command and arguments.

    The tool's vocabulary is the user's (a section, a viewpoint, a clearance);
    the viewer's is the viewport's. Keeping the translation here means a bad
    argument is refused with the tool's own words rather than the viewer's.
    """
    guids = kw.get("guids")
    if action == "context":
        return "get-context", {}
    if action == "set_view":
        if not kw.get("view"):
            raise ToolError(
                "INVALID_INPUT",
                "set_view needs a view direction.",
                "Pass view='top', 'bottom', 'front', 'back', 'left', 'right' or 'iso'.",
            )
        return "set-view", {"view": kw["view"], "selection": bool(guids)}
    if action == "set_camera":
        return "set-camera", _camera_params(kw.get("camera"))
    if action == "fit":
        params = {}
        if guids:
            params["guids"] = guids
        elif kw.get("selection"):
            params["selection"] = True
        if kw.get("padding") is not None:
            params["padding"] = kw["padding"]
        if kw.get("view"):
            params["view"] = kw["view"]
        return "fit", params
    if action == "set_projection":
        if not kw.get("projection"):
            raise ToolError(
                "INVALID_INPUT",
                "set_projection needs a projection.",
                "Pass projection='orthographic' or 'perspective'.",
            )
        return "set-projection", {"projection": kw["projection"]}
    if action == "section":
        params: dict = {"clear": bool(kw.get("clear"))}
        if kw.get("section") is not None:
            params["axes"] = kw["section"]
        if kw.get("slice_depth") is not None:
            params["slice"] = kw["slice_depth"]
        if len(params) == 1 and not params["clear"]:
            raise ToolError(
                "INVALID_INPUT",
                "section needs axes, a slice depth, or clear=true.",
                "Try section={'z': {'at': 1.2, 'keep': 'below'}}, slice_depth=0.3.",
            )
        return "set-section", params
    if action == "select":
        if not guids:
            raise ToolError(
                "INVALID_INPUT",
                "select needs GlobalIds.",
                "Pass guids from query_elements or search_elements; the "
                "selection drives the properties panel the user sees.",
            )
        return "set-selection", {"guids": guids, "additive": False}
    if action == "isolate":
        return "isolate", {"guids": guids or []}
    if action == "hide":
        return "hide", {"guids": guids or []}
    if action == "show_all":
        return "show-all", {}
    if action == "focus":
        return "focus", {"guids": guids or [], "name": kw.get("name") or ""}
    if action == "unfocus":
        return "unfocus", {"name": kw.get("name") or ""}
    if action == "measure_elements":
        return "measure-element", {"guids": guids or []}
    if action == "measure_clearance":
        return "measure-laser", ({"guid": guids[0]} if guids else {})
    if action == "measure_points":
        points = kw.get("points") or []
        if any(len(point) != 3 for point in points):
            raise ToolError(
                "INVALID_INPUT",
                "every measure_points entry must be [x, y, z].",
                "Points are metres in the model's axes, z up.",
            )
        if len(points) == 2:
            return "measure-points", {"from": points[0], "to": points[1]}
        if len(points) == 3:
            return "measure-angle", {"from": points[0], "at": points[1], "to": points[2]}
        if len(points) >= 4:
            return "measure-area", {"points": points}
        raise ToolError(
            "INVALID_INPUT",
            "measure_points needs at least two points.",
            "Two measure a distance, three an angle, four or more an area.",
        )
    if action == "clear_measurements":
        return "clear-measurements", {}
    if action in {"save_view", "restore_view"}:
        if not kw.get("name"):
            raise ToolError(
                "INVALID_INPUT",
                f"{action} needs a viewpoint name.",
                "Pass name='south elevation'.",
            )
        return ("save-view" if action == "save_view" else "restore-view"), {"name": kw["name"]}
    if action == "list_views":
        return "list-views", {}
    raise ToolError(
        "INVALID_INPUT",
        f"unknown viewer action {action}.",
        f"Use one of: {', '.join(VIEWER_ACTIONS)}.",
    )


def register_launcher(mcp: OperationRegistry, core: AppCore) -> None:
    """The always-on tool that turns the viewer surface on for MCP clients."""
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        name=LAUNCHER_TOOL,
        annotations=LAUNCH_ANN,
        required_capabilities=(Capability.VIEWER_CONTROL,),
        description=(
            "[VIEW] Turn the 3D viewer on and open it in the user's browser, "
            "so the viewer tools (highlight_elements, control_viewer with "
            "focus, get_viewer_screenshot, get_viewer_selection) become "
            "available. Call it when visual work is needed and "
            "get_session_status says the viewer is off; it is a no-op when "
            "already on. The browser tab takes a moment to connect: check "
            "get_session_status.viewer before the first viewer call, and "
            "refresh your tool list if your client caches it."
        ),
    )
    @enveloped(core, LAUNCHER_TOOL)
    async def open_viewer(
        open_browser: Annotated[
            bool,
            Field(
                description="Also open the viewer in the local browser; the user can instead run /viewer."
            ),
        ] = True,
    ) -> Envelope:
        if core.transport == "stdio":
            raise ToolError(
                "VIEWER_UNAVAILABLE",
                "this session runs over stdio, which serves no web pages.",
                "Ask the user to run the interactive console or "
                "`ifc-console serve --http` and connect over HTTP; the viewer "
                "needs the HTTP surface.",
            )
        if not core.enable_viewer():
            raise ToolError(
                "EXTRA_NOT_INSTALLED",
                "the viewer asset bundle is not installed.",
                "Ask the user to install the viewer extra "
                "(`pip install 'ifc-console[viewer]'`) and restart.",
            )
        opened = False
        if open_browser and not core.viewer_hub.connected:
            import webbrowser

            # the tokenized URL goes to the local browser only, never into
            # the tool result and so never into a model context
            opened = bool(webbrowser.open(core.viewer_url))
        data = {
            "enabled": True,
            "connected": bool(core.viewer_hub.connected),
            "url": core.viewer_public_url,
            "opened_browser": opened,
        }
        if not core.viewer_hub.connected:
            data["note"] = (
                "waiting for a browser tab to connect; if none opened, the "
                "user can run /viewer in the ifc-console terminal"
            )
        return ok(data, core.session_meta(), char_limit=limit_)


def register(mcp: OperationRegistry, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @core.model_lifecycle_operation
    async def _resolve_selector(action: str, selector: str, guids: list[str] | None) -> list[str]:
        """Turn a selector into GlobalIds so "isolate level 2" is one call."""
        if action not in SELECTOR_ACTIONS:
            raise ToolError(
                "INVALID_INPUT",
                f"selector does not apply to {action}.",
                f"selector works with: {', '.join(SELECTOR_ACTIONS)}.",
            )
        if guids:
            raise ToolError(
                "INVALID_INPUT",
                "pass either selector or guids, not both.",
                "selector resolves to the ids itself; drop guids.",
            )
        session = core.session
        session.require_loaded()

        def job() -> list[str]:
            found = []
            for entity in sorted(selected(session.ifc, selector), key=lambda e: e.id()):
                guid = getattr(entity, "GlobalId", None)
                if isinstance(guid, str) and guid:
                    found.append(guid)
            return list(dict.fromkeys(found))

        matched = await session.run(job, timeout=60)
        if not matched:
            raise ToolError(
                "NO_MATCH",
                f"selector {selector!r} matched no elements with a GlobalId.",
                "Check it with query_elements first; the viewer can only "
                "address elements the model names.",
            )
        if len(matched) > MAX_SELECTOR_ELEMENTS:
            raise ToolError(
                "TOO_MANY_ELEMENTS",
                f"selector {selector!r} matched {len(matched)} elements, over the "
                f"{MAX_SELECTOR_ELEMENTS} the viewer accepts in one command.",
                "Narrow it, for example by adding a storey or a class.",
            )
        return matched

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
            # With several tabs open, say which one the user clicked in so a
            # disagreement between them is visible rather than silent.
            "tab": hub.selection_client_id,
        }
        # What the user is actually looking at: a section already cutting the
        # model, or elements already hidden, otherwise gets re-done blind.
        viewport = hub.viewport_summary()
        if viewport:
            data["viewport"] = viewport
        if not guids:
            data["note"] = "nothing is selected; ask the user to click elements in the viewer"
        extra_meta = {} if session is core.session else {"read_from": session.model_id}
        return ok(data, core.session_meta(), char_limit=limit_, **extra_meta)

    @mcp.tool(
        annotations=VIEW_ANN,
        description=(
            "[QUERY] Everything measured in the web viewer, by the user or by "
            "control_viewer: distances (M), angles (A), areas (R), element "
            "sizes and clearances. Each item carries its kind; lengths are "
            "metres and points are model axes (z up). The human's way of "
            "showing you a dimension - check it when the user says 'the "
            "distance I measured'. Empty until something is measured; "
            "requires the viewer."
        ),
    )
    @enveloped(core, "get_viewer_measurements")
    async def get_viewer_measurements() -> Envelope:
        hub = core.viewer_hub
        hub.require_connected()
        source = hub.measurement_source()
        items, measured_at = hub.latest_measurements()
        data: dict = {
            "connected": hub.connected,
            "measurements": items,
            "measured_at": measured_at,
            # With several tabs open, say which one these came from so a
            # disagreement between them is visible rather than silent.
            "tab": source.id if source is not None else None,
            # With two models resident a dimension must not be attributed to
            # the wrong file.
            "model_id": source.measurement_model_id if source is not None else None,
            "units": "metres, model axes (z up)",
        }
        if not items:
            data["note"] = (
                "no measurements; measure with control_viewer, or ask the user "
                "to press M in the viewer and click two points"
            )
        return ok(data, core.session_meta(), char_limit=limit_, returned=len(items))

    @mcp.tool(
        annotations=VIEW_ANN,
        description=(
            "[VIEW] Drive the 3D viewer and read back what it did. Cut a "
            "section, switch to orthographic, look from a named direction, "
            "place the camera anywhere (set_camera) or frame something with "
            "fit, select elements (frames them and opens their properties for "
            "the user), isolate or hide elements, measure, and save or restore "
            "a named viewpoint. select, isolate, hide, focus and fit take a "
            "selector instead of ids, so 'isolate the doors on level 2' is one "
            "call. Start from action='context' to read the camera, the viewport "
            "and what is currently hidden. "
            "focus opens the given elements alone in a named tab under the "
            "viewer's top bar, so one object can be analyzed without the rest "
            "of the model; unfocus closes one tab by name, or all of them. "
            "Measuring here uses the tessellated geometry on screen, so it "
            "answers questions the schema cannot (a rotated wall's real "
            "thickness, the clear distance between two elements, the area "
            "inside an outline) and every result is added to the viewer's "
            "measurement list where the user can see it. Sections and "
            "projections change nothing in the file. Requires the viewer "
            "(see get_session_status.viewer)."
        ),
    )
    @enveloped(core, "control_viewer")
    async def control_viewer(
        action: Annotated[
            Literal[
                "context",
                "set_view",
                "set_camera",
                "fit",
                "set_projection",
                "section",
                "select",
                "isolate",
                "hide",
                "show_all",
                "focus",
                "unfocus",
                "measure_elements",
                "measure_clearance",
                "measure_points",
                "clear_measurements",
                "save_view",
                "restore_view",
                "list_views",
            ],
            Field(description="What to do to the viewport."),
        ],
        guids: Annotated[
            list[str] | None,
            Field(
                default=None,
                max_length=500,
                description=(
                    "GlobalIds for select, isolate, hide, focus, fit and "
                    "measure_elements, and the element to shoot from for "
                    "measure_clearance. All but select default to the user's "
                    "viewer selection. Use selector instead of pasting a long list."
                ),
            ),
        ] = None,
        selector: Annotated[
            str | None,
            Field(
                default=None,
                max_length=2000,
                description=(
                    "query_elements selector standing in for guids on select, "
                    "isolate, hide, focus and fit, resolved against the active "
                    "model here: selector='IfcDoor, location=Level 2' isolates a "
                    "storey's doors in one call and is not capped at 500 ids."
                ),
            ),
        ] = None,
        view: Annotated[
            Literal["top", "bottom", "front", "back", "left", "right", "iso"] | None,
            Field(
                default=None,
                description="Camera direction for set_view, and optionally for fit.",
            ),
        ] = None,
        camera: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description=(
                    "For set_camera: {'position': [x, y, z], 'target': [x, y, z], "
                    "'up': [x, y, z], 'fov': 50, 'projection': 'perspective', "
                    "'transition': true}, every field optional. Coordinates are "
                    "metres in the model's own axes (z up), the same frame "
                    "measurements use. action='context' returns the current "
                    "camera in this shape, so read it, change one field, send it back."
                ),
            ),
        ] = None,
        padding: Annotated[
            float | None,
            Field(
                default=None,
                gt=0,
                le=100,
                description=(
                    "For fit: multiplier on the framed radius. 1.0 is snug, 1.5 "
                    "leaves the element in its context."
                ),
            ),
        ] = None,
        selection: Annotated[
            bool,
            Field(
                default=False,
                description=(
                    "For fit: frame the user's current viewer selection. Without "
                    "it, and without guids or selector, fit frames the whole model."
                ),
            ),
        ] = False,
        projection: Annotated[
            Literal["perspective", "orthographic"] | None,
            Field(
                default=None,
                description=(
                    "For set_projection. Orthographic is parallel projection: "
                    "use it whenever a length read off the screen has to mean "
                    "the same thing anywhere in the frame."
                ),
            ),
        ] = None,
        section: Annotated[
            dict[str, Any] | None,
            Field(
                default=None,
                description=(
                    "For section: {'z': {'at': 1.2, 'keep': 'below'}} cuts the "
                    "model's z axis at 1.2 m and keeps what is below it. Axes "
                    "are the model's own (z up), positions are metres, and "
                    "{'z': false} turns that axis off."
                ),
            ),
        ] = None,
        slice_depth: Annotated[
            float | None,
            Field(
                default=None,
                ge=0,
                le=1000,
                description=(
                    "For section: keep this many metres beyond each cut instead "
                    "of everything, which is what turns a cut into a floor plan. "
                    "0 keeps everything."
                ),
            ),
        ] = None,
        points: Annotated[
            list[list[float]] | None,
            Field(
                default=None,
                max_length=200,
                description=(
                    "For measure_points, in model axes (z up). Two points "
                    "measure a distance, three the angle at the middle one, "
                    "four or more the area of the outline they close."
                ),
            ),
        ] = None,
        name: Annotated[
            str | None,
            Field(
                default=None,
                max_length=80,
                description=(
                    "Viewpoint name for save_view and restore_view; tab name for "
                    "focus and unfocus (use the element's Name)."
                ),
            ),
        ] = None,
        clear: Annotated[
            bool,
            Field(
                default=False,
                description="For section: turn every existing cut off first.",
            ),
        ] = False,
    ) -> Envelope:
        hub = core.viewer_hub
        hub.require_connected()
        resolved = None
        if selector is not None:
            guids = await _resolve_selector(action, selector, guids)
            resolved = len(guids)
        command, params = _viewer_call(
            action,
            guids=guids,
            view=view,
            camera=camera,
            padding=padding,
            selection=selection,
            projection=projection,
            section=section,
            slice_depth=slice_depth,
            points=points,
            name=name,
            clear=clear,
        )
        result = await hub.run_command(command, params)
        data: dict[str, Any] = {"action": action, "result": result}
        if resolved is not None:
            data["selector"] = selector
            data["resolved"] = resolved
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
            frame_groups.append({"label": group.label, "color": color, "guids": resolved[index]})
            legend.append({"label": group.label, "color": color, "count": len(resolved[index])})
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
        return [OperationImage(data=data, format=format), note]
