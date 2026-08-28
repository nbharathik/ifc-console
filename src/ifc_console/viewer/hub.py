"""ViewerHub: the server side of the viewer WebSocket protocol.

One hub per AppCore. It tracks connected browser tabs, holds the user's
click-selection, fans out server-to-client frames (highlight, camera,
model_updated, mode_changed), and correlates screenshot requests with their
responses. Everything runs on the server event loop; the hub never touches
the IFC model itself.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import itertools
import json
import logging
from datetime import datetime, timezone
from hashlib import sha256
from math import isfinite
from typing import TYPE_CHECKING, Any

from ifc_console.core.results import ToolError
from ifc_console.ifc.units import unit_info

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.viewer")

# Keepalive cadence and the ceiling for one screenshot payload. A 2048 px JPEG
# is far below this; the cap only trips when a client misbehaves.
_PING_INTERVAL = 30.0
_SCREENSHOT_TIMEOUT = 10.0
# A viewer command is arithmetic on data already in the tab, so it either
# answers immediately or the tab is not answering at all.
_COMMAND_TIMEOUT = 8.0
# A tab rebuilding its scene has no geometry to address, so a short wait beats
# both a wrong answer and an eight second timeout.
_REBUILD_WAIT = 3.0
_REBUILD_POLL = 0.05
_MAX_COMMAND_RESULT = 400_000
_MAX_SCREENSHOT_B64 = 12_000_000
_MAX_SCREENSHOT_DIMENSION = 8192
_MAX_GUID_LENGTH = 128
_MAX_MODEL_ID_LENGTH = 200

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


_MAX_MEASUREMENTS = 100


def _triple(value: Any) -> list[float] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    try:
        numbers = [float(v) for v in value]
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(isfinite(number) for number in numbers):
        return None
    return [round(number, 6) for number in numbers]


def _guid_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        guid for guid in value if isinstance(guid, str) and 0 < len(guid) <= _MAX_GUID_LENGTH
    ][:500]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return round(number, 6) if isfinite(number) else None


def _clean_distance(item: dict) -> dict | None:
    start = _triple(item.get("from"))
    end = _triple(item.get("to"))
    distance = _number(item.get("distance"))
    if start is None or end is None or distance is None:
        return None
    entry = {"from": start, "to": end, "distance": distance}
    for key in ("horizontal", "vertical", "slope_percent", "slope_angle"):
        value = _number(item.get(key))
        if value is not None:
            entry[key] = value
    delta = _triple(item.get("delta"))
    if delta is not None:
        entry["delta"] = delta
    ends = item.get("ends")
    if isinstance(ends, list) and len(ends) == 2:
        entry["ends"] = [str(end)[:20] for end in ends]
    return entry


def _clean_dimensions(item: dict) -> dict | None:
    entry: dict = {}
    for key in ("length", "width", "thickness", "diagonal", "area", "volume", "box_volume"):
        value = _number(item.get(key))
        if value is not None:
            entry[key] = value
    if not entry:
        return None
    for key in ("guid", "method"):
        if isinstance(item.get(key), str):
            entry[key] = item[key][:200]
    # Dropping this flag would hand the caller an area and a volume the viewer
    # itself considers unreliable, with nothing left to say so.
    if item.get("approximate") is True:
        entry["approximate"] = True
    if isinstance(item.get("centre"), dict):
        centre = {k: _number(v) for k, v in item["centre"].items() if k in "xyz"}
        if len(centre) == 3 and None not in centre.values():
            entry["centre"] = centre
    return entry


def _clean_laser(item: dict) -> dict | None:
    axes = item.get("axes")
    if not isinstance(axes, dict):
        return None
    out: dict = {}
    for name in ("x", "y", "z"):
        value = axes.get(name)
        if not isinstance(value, dict):
            continue
        span = _number(value.get("span"))
        hits = {}
        for side in ("negative", "positive"):
            hit = value.get(side)
            if isinstance(hit, dict) and _number(hit.get("distance")) is not None:
                hits[side] = {
                    "distance": _number(hit.get("distance")),
                    "guid": str(hit.get("guid"))[:200] if hit.get("guid") else None,
                }
        out[name] = {"span": span, **hits}
    if not out:
        return None
    entry = {"axes": out}
    if isinstance(item.get("method"), str):
        entry["method"] = item["method"][:200]
    origin = _triple(item.get("origin"))
    if origin is not None:
        entry["origin"] = origin
    return entry


def _clean_angle(item: dict) -> dict | None:
    degrees = _number(item.get("degrees"))
    at = _triple(item.get("at"))
    if degrees is None or at is None:
        return None
    entry = {"degrees": degrees, "at": at}
    for key in ("from", "to"):
        point = _triple(item.get(key))
        if point is not None:
            entry[key] = point
    legs = item.get("legs")
    if isinstance(legs, list) and len(legs) == 2:
        cleaned = [_number(leg) for leg in legs]
        if None not in cleaned:
            entry["legs"] = cleaned
    return entry


def _clean_area(item: dict) -> dict | None:
    area = _number(item.get("area"))
    points = item.get("points")
    if area is None or not isinstance(points, list) or not 3 <= len(points) <= 200:
        return None
    cleaned = [_triple(point) for point in points]
    # Removing one bad vertex would join its neighbours while retaining the
    # client's original area and perimeter, making the geometry disagree with
    # the reported totals.
    if any(point is None for point in cleaned):
        return None
    entry = {"area": area, "points": cleaned}
    for key in ("perimeter", "flatness"):
        value = _number(item.get(key))
        if value is not None:
            entry[key] = value
    # The normal says which plane the area was measured in and the centre says
    # where; without them an area is a number with no place in the model.
    normal = _triple(item.get("normal"))
    if normal is not None:
        entry["normal"] = normal
    centre = item.get("centre")
    if isinstance(centre, dict):
        cleaned_centre = {axis: _number(centre.get(axis)) for axis in ("x", "y", "z")}
        if None not in cleaned_centre.values():
            entry["centre"] = cleaned_centre
    else:
        cleaned_centre_list = _triple(centre)
        if cleaned_centre_list is not None:
            # Older clients used triples; preserve that wire shape while the
            # current browser's named-axis object remains equally valid.
            entry["centre"] = cleaned_centre_list
    return entry


def _clean_path(item: dict) -> dict | None:
    distance = _number(item.get("distance"))
    points = item.get("points")
    if distance is None or not isinstance(points, list) or not 2 <= len(points) <= 200:
        return None

    # Unlike an area outline, a path's point order defines each segment. If a
    # point is invalid, dropping just that point would silently join its two
    # neighbours and change the measurement.
    cleaned_points = [_triple(point) for point in points]
    if any(point is None for point in cleaned_points):
        return None
    entry = {"distance": distance, "points": cleaned_points}

    segments = item.get("segments")
    if isinstance(segments, list) and len(segments) == len(cleaned_points) - 1:
        cleaned_segments = [_number(segment) for segment in segments]
        if None not in cleaned_segments:
            entry["segments"] = cleaned_segments
    return entry


_MEASUREMENT_KINDS = {
    "distance": _clean_distance,
    "dimensions": _clean_dimensions,
    "laser": _clean_laser,
    "angle": _clean_angle,
    "area": _clean_area,
    "path": _clean_path,
}


def _clean_measurements(items: Any) -> list[dict]:
    """Validated user measurements from a client frame; anything odd drops.

    A frame carries whatever the viewer's measure card is showing, which is
    several different shapes. An untagged item is a distance, because that is
    what the only shape used to be.
    """
    cleaned: list[dict] = []
    if not isinstance(items, list):
        return cleaned
    for item in items[:_MAX_MEASUREMENTS]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        kind = kind if isinstance(kind, str) and kind in _MEASUREMENT_KINDS else "distance"
        entry = _MEASUREMENT_KINDS[kind](item)
        if entry is None:
            continue
        if kind != "distance":
            entry = {"kind": kind, **entry}
        # A stable per-row id is what lets a caller name one measurement; tabs
        # that do not send one simply have no id.
        item_id = item.get("id")
        if isinstance(item_id, str) and 0 < len(item_id) <= 64:
            entry = {"id": item_id, **entry}
        cleaned.append(entry)
    return cleaned


_KEEP_SIDES = ("above", "below")
_PROJECTIONS = ("perspective", "orthographic")


def _clean_camera(item: Any) -> dict | None:
    """The camera the tab is actually looking through, in model axes, metres."""
    if not isinstance(item, dict):
        return None
    entry: dict = {}
    for key in ("position", "target", "up"):
        vector = _triple(item.get(key))
        if vector is not None:
            entry[key] = vector
    for key in ("fov", "ortho_height", "distance", "world_per_pixel"):
        value = _number(item.get(key))
        if value is not None:
            entry[key] = value
    if item.get("projection") in _PROJECTIONS:
        entry["projection"] = item["projection"]
    return entry or None


def _counts(item: Any, keys: tuple[str, ...]) -> dict | None:
    if not isinstance(item, dict):
        return None
    entry = {}
    for key in keys:
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10_000_000:
            continue
        entry[key] = value
    return entry or None


def _clean_section(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    entry: dict = {}
    axes: dict = {}
    for name in ("x", "y", "z"):
        axis = item.get(name)
        if not isinstance(axis, dict):
            continue
        at = _number(axis.get("at"))
        if at is None:
            continue
        keep = axis.get("keep") if axis.get("keep") in _KEEP_SIDES else "below"
        axes[name] = {"at": at, "keep": keep}
    if axes:
        entry["axes"] = axes
    depth = _number(item.get("slice"))
    if depth is not None:
        entry["slice"] = depth
    return entry or None


def _clean_viewport_state(state: Any) -> dict | None:
    """One tab's viewport, whitelisted the way measurement frames are.

    Everything here is context the agent otherwise cannot see: what the camera
    is looking at, how much of the model is cut away, and how much is hidden.
    """
    if not isinstance(state, dict):
        return None
    entry: dict = {}
    camera = _clean_camera(state.get("camera"))
    if camera is not None:
        entry["camera"] = camera
    viewport = _counts(state.get("viewport"), ("width", "height"))
    if viewport is not None:
        entry["viewport"] = viewport
    visibility = _counts(state.get("visibility"), ("hidden", "isolated", "total", "visible"))
    if visibility is not None:
        entry["visibility"] = visibility
    section = _clean_section(state.get("section"))
    if section is not None:
        entry["section"] = section
    if isinstance(state.get("view"), str):
        entry["view"] = state["view"][:20]
    if isinstance(state.get("model_id"), str):
        entry["model_id"] = state["model_id"][:_MAX_MODEL_ID_LENGTH]
    elif "model_id" in state and state["model_id"] is None:
        # Explicit null means this connected page currently has no IFC surface
        # open. Missing keeps the legacy "active model" fallback.
        entry["model_id"] = None
    return entry or None


def _summarize_viewport(state: dict) -> str | None:
    """One line an agent can read before it re-isolates what is already isolated."""
    parts: list[str] = []
    camera = state.get("camera") or {}
    view = state.get("view")
    projection = camera.get("projection")
    if view and projection:
        parts.append(f"{projection} {view} view")
    elif view:
        parts.append(f"{view} view")
    elif projection:
        parts.append(f"{projection} view")
    section = state.get("section") or {}
    for name, axis in (section.get("axes") or {}).items():
        parts.append(f"section cut on {name} at {axis['at']:g} m keeping {axis['keep']}")
    visibility = state.get("visibility") or {}
    total = visibility.get("total")
    isolated = visibility.get("isolated")
    hidden = visibility.get("hidden")
    if isolated:
        parts.append(f"isolated to {isolated} elements")
    elif hidden:
        parts.append(f"{hidden} of {total} elements hidden" if total else f"{hidden} elements hidden")
    return "; ".join(parts) or None


class ViewerClient:
    """One connected browser tab."""

    _ids = itertools.count(1)
    # Monotonic activity sequence, not wall time: screenshot targeting picks
    # the tab that talked to us last, and coarse clocks must not tie-break.
    _activity = itertools.count(1)

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.id = next(self._ids)
        self.connected_at = _utcnow()
        self.last_active = next(self._activity)
        self.selection: list[str] = []
        self.selection_model_id: str | None = None
        self.selections: dict[str, list[str]] = {}
        self.selection_versions: dict[str, int] = {}
        self.view_model_id: str | None = None
        self.selected_at: str | None = None
        self.selection_order = 0
        self.measurements: list[dict] = []
        self.measured_at: str | None = None
        self.measurement_model_id: str | None = None
        self.measurement_order = 0
        # A tab that predates the scene_state frame never sends one, so
        # "ready" is the only default that keeps it working.
        self.scene_state = "ready"
        # Last viewport this tab published: camera, section, visibility counts.
        self.viewport: dict | None = None
        self.viewport_at: str | None = None
        # Ids of the calls this tab still owes an answer to, so closing it
        # fails them now instead of at the timeout.
        self.commands: set[str] = set()
        self.shots: set[str] = set()

    def touch(self) -> None:
        self.last_active = next(self._activity)

    async def send(self, frame: dict) -> None:
        await self.ws.send_text(json.dumps(frame, default=str))


class ViewerHub:
    def __init__(self, core: AppCore) -> None:
        self.core = core
        self.clients: list[ViewerClient] = []
        self.selection: list[str] = []
        self.selection_model_id: str | None = None
        self.selections: dict[str, list[str]] = {}
        self.selected_at: str | None = None
        self._selection_client: ViewerClient | None = None
        self._selection_updates = itertools.count(1)
        self.last_highlight: dict | None = None
        self.last_color_theme: dict | None = None
        self._shots: dict[str, tuple[ViewerClient, str | None, asyncio.Future]] = {}
        self._shot_ids = itertools.count(1)
        # Viewer commands share the id counter: one sequence, no collisions.
        self._commands: dict[str, tuple[ViewerClient, asyncio.Future]] = {}
        self._ping_task: asyncio.Task | None = None
        # Model bytes cached per ETag so several tabs (or a reconnect) do not
        # re-serialize the same revision; a few entries cover model switching.
        self._model_cache: dict[str, bytes] = {}
        # One in-flight serialization per ETag, so N tabs opening together
        # queue one job on the model worker, not N.
        self._model_jobs: dict[str, asyncio.Future] = {}
        # Length unit per model, stamped with the fingerprint it was read at.
        # status_payload is synchronous and the model belongs to its worker, so
        # the units are read there and served from here.
        self._units: dict[str, tuple[str | None, dict]] = {}
        core.events.subscribe(self._on_event)

    # -- connection registry ---------------------------------------------------
    @property
    def connected(self) -> int:
        return len(self.clients)

    def register(self, ws: Any) -> ViewerClient:
        client = ViewerClient(ws)
        self.clients.append(client)
        self.core.viewer.connected = self.connected
        self.core.events.emit("viewer_connected", client_id=client.id, tabs=self.connected)
        if self._ping_task is None or self._ping_task.done():
            self._ping_task = asyncio.ensure_future(self._ping_loop())
        return client

    def _clear_selection(self) -> None:
        self.selection = []
        self.selection_model_id = None
        self.selections = {}
        self.selected_at = None
        self._selection_client = None
        self.core.viewer.selection = []

    def _set_selection(self, client: ViewerClient) -> None:
        self.selection = list(client.selection)
        self.selection_model_id = client.selection_model_id
        self.selected_at = client.selected_at
        self._selection_client = client
        self.core.viewer.selection = list(client.selection)

    def _adopt_selection(self, client: ViewerClient) -> None:
        """Take this tab's selection unless doing so would erase another's.

        Every tab publishes an empty selection while it rebuilds its scene, so
        last-writer-wins let a freshly opened tab tell the LLM that nothing is
        selected while the first tab still shows the user's picks. Only the tab
        that owns the selection may empty it.
        """
        if (
            not client.selection
            and self.selection
            and self._selection_client is not None
            and self._selection_client is not client
            and self._selection_client in self.clients
        ):
            return
        self._set_selection(client)

    def _restore_latest_selection(self) -> None:
        candidates = [client for client in self.clients if client.selection_order]
        with_selection = [client for client in candidates if client.selection]
        pool = with_selection or candidates
        if pool:
            self._set_selection(max(pool, key=lambda client: client.selection_order))
        else:
            self._clear_selection()

    def _restore_model_selections(self) -> None:
        """Keep the newest connected tab's selection for each resident IFC."""
        selections: dict[str, list[str]] = {}
        model_ids = {
            model_id
            for client in self.clients
            for model_id in client.selection_versions
            if self.core.models.get(model_id) is not None
        }
        for model_id in model_ids:
            candidates = [
                client
                for client in self.clients
                if model_id in client.selection_versions
            ]
            if candidates:
                latest = max(
                    candidates,
                    key=lambda client: client.selection_versions[model_id],
                )
                if guids := latest.selections.get(model_id):
                    selections[model_id] = list(guids)
        self.selections = selections

    def selection_rows(self) -> list[dict]:
        """Model-scoped selections suitable for status and prompt context."""
        models = self.model_rows()
        return [
            {
                "model_id": row["id"],
                "model": row["name"],
                "count": len(guids),
                "guids": list(guids),
            }
            for row in models
            if (guids := self.selections.get(row["id"]))
        ]

    def _prune_selection_models(self) -> None:
        changed = False
        for client in self.clients:
            model_id = client.selection_model_id
            if model_id is None or self.core.models.get(model_id) is None:
                client.selection = []
                client.selection_model_id = None
                client.selected_at = None
                client.selection_order = 0
                changed = True
            client.selections = {
                selected_model: guids
                for selected_model, guids in client.selections.items()
                if self.core.models.get(selected_model) is not None
            }
            client.selection_versions = {
                selected_model: version
                for selected_model, version in client.selection_versions.items()
                if self.core.models.get(selected_model) is not None
            }
        if changed:
            self._restore_latest_selection()
        self._restore_model_selections()

    def _tab_gone(self) -> ToolError:
        return ToolError(
            "VIEWER_NOT_CONNECTED",
            "the viewer tab closed before it answered.",
            "Ask the user to reopen the viewer (type /viewer in the ifc-console "
            "terminal), then retry.",
        )

    def _fail_pending(self, client: ViewerClient) -> None:
        """Fail a closed tab's calls now rather than at their timeout."""
        for command_id in list(client.commands):
            pending = self._commands.get(command_id)
            if pending is not None and not pending[1].done():
                pending[1].set_exception(self._tab_gone())
        for shot_id in list(client.shots):
            entry = self._shots.get(shot_id)
            if entry is not None and not entry[2].done():
                entry[2].set_exception(self._tab_gone())

    def unregister(self, client: ViewerClient) -> None:
        # close_all() unregisters first and the socket's finally block
        # unregisters again; only the first call may emit.
        if client not in self.clients:
            return
        self.clients.remove(client)
        self._fail_pending(client)
        self.core.viewer.connected = self.connected
        self.core.events.emit("viewer_disconnected", client_id=client.id, tabs=self.connected)
        if not self.clients:
            # No tab means nothing is selected. A closed or reloaded tab must
            # not leave a selection the LLM would read as still on screen;
            # every tab resends its own selection on connect.
            self._clear_selection()
            if self._ping_task is not None:
                self._ping_task.cancel()
                self._ping_task = None
        elif self._selection_client is client:
            self._restore_latest_selection()
        self._restore_model_selections()

    async def close_all(self) -> int:
        """Disconnect every tab (used by /viewer off). Returns tabs closed."""
        clients = list(self.clients)
        for client in clients:
            with contextlib.suppress(Exception):
                await client.ws.close(code=4000)
            self.unregister(client)
        return len(clients)

    def require_connected(self) -> None:
        if not self.clients:
            if self.core.transport != "http":
                raise ToolError(
                    "VIEWER_UNAVAILABLE",
                    f"this {self.core.transport} session has no web viewer surface.",
                    "Connect through the shared console with `ifc-console bridge`, "
                    "or run `ifc-console serve --http`, then call open_viewer.",
                )
            hint = (
                "Call open_viewer(wait_for_connection_s=10) to open and connect "
                "the local viewer tab. If the browser cannot be opened on this "
                "machine, ask the user to run /viewer in the ifc-console terminal."
            )
            raise ToolError(
                "VIEWER_NOT_CONNECTED",
                "no web viewer tab is connected to this session.",
                hint + " Meanwhile query_elements/get_element give the same information as text.",
            )

    @property
    def selection_client_id(self) -> int | None:
        """Which tab the shared selection came from, so two tabs disagreeing
        is visible to the caller instead of silent."""
        client = self._selection_client
        return client.id if client is not None and client in self.clients else None

    def measurement_source(self, model_id: str | None = None) -> ViewerClient | None:
        """The tab whose measurements the tools should read.

        Content beats recency: a tab rebuilding its scene publishes an empty
        list, which must not erase the dimensions another tab is showing.
        """
        target = model_id or self.core.models.active_id
        candidates = [
            client
            for client in self.clients
            if client.measured_at is not None and client.measurement_model_id == target
        ]
        if not candidates:
            return None
        with_items = [client for client in candidates if client.measurements]
        return max(with_items or candidates, key=lambda client: client.measurement_order)

    def latest_measurements(self, model_id: str | None = None) -> tuple[list[dict], str | None]:
        """The measurements a tool should report, with their timestamp."""
        client = self.measurement_source(model_id)
        if client is None:
            return [], None
        return list(client.measurements), client.measured_at

    def viewport_source(self, model_id: str | None = None) -> ViewerClient | None:
        """The tab whose viewport the tools should describe."""
        target = model_id or self.core.models.active_id
        candidates = [
            client
            for client in self.clients
            if client.viewport is not None and client.view_model_id == target
        ]
        if not candidates:
            return None
        ready = [client for client in candidates if client.scene_state == "ready"]
        return max(ready or candidates, key=lambda client: client.last_active)

    def viewport_state(self, model_id: str | None = None) -> dict | None:
        """The full viewport of the tab showing the active model, or None."""
        client = self.viewport_source(model_id)
        return dict(client.viewport) if client is not None and client.viewport else None

    def viewport_summary(self, model_id: str | None = None) -> str | None:
        """One line describing what is on screen, for a status or a panel note."""
        state = self.viewport_state(model_id)
        return _summarize_viewport(state) if state else None

    # -- state payloads ----------------------------------------------------------
    def model_etag(self, session: Any = None) -> str | None:
        s = session or self.core.session
        if not s.loaded:
            return None
        model_id = s.model_id or "model"
        return f"{model_id}-{s.fingerprint}-{s.revision}"

    def units(self, session: Any = None) -> dict | None:
        """The cached length unit for a model, or None until it has been read."""
        s = session or self.core.session
        cached = self._units.get(s.model_id or "model")
        if cached is None or cached[0] != s.fingerprint:
            return None
        return cached[1]

    async def refresh_units(self) -> None:
        """Read every resident model's length unit on its own worker.

        The viewer needs the file's unit to label anything it measures, and
        status_payload cannot read the model itself: the file object belongs to
        the model worker. Cached per fingerprint, so this is a no-op per load.
        """
        for session in list(self.core.models.sessions.values()):
            if not session.loaded or self.units(session) is not None:
                continue
            fingerprint = session.fingerprint
            try:
                info = await session.run(lambda s=session: unit_info(s.ifc), timeout=30)
            except Exception:
                # A busy or detached model simply has no units yet; the next
                # status frame asks again.
                continue
            self._units[session.model_id or "model"] = (fingerprint, info)

    async def _publish_units(self) -> None:
        """Resend status once the units are known.

        The first status after a load cannot carry them (the model belongs to
        its worker and status_payload is synchronous), so the tab gets them in
        the next frame rather than waiting on the model worker for the first.
        """
        before = dict(self._units)
        await self.refresh_units()
        if self._units != before and self.clients:
            await self.broadcast(self.status_payload())

    def model_rows(self) -> list[dict]:
        """Every resident model the viewer may show; the active one leads."""
        rows = []
        for model_id, session in self.core.models.sessions.items():
            rows.append(
                {
                    "id": model_id,
                    "name": session.name,
                    "schema": session.schema,
                    "active": model_id == self.core.models.active_id,
                    "etag": self.model_etag(session),
                    "units": self.units(session),
                }
            )
        rows.sort(key=lambda r: (not r["active"], r["id"]))
        return rows

    def project_scope(self) -> str:
        """Stable opaque browser-storage scope for this project directory."""
        return sha256(
            str(self.core.store.project_dir.resolve()).encode("utf-8")
        ).hexdigest()[:16]

    def status_payload(self) -> dict:
        from ifc_console.themes import resolve_theme

        s = self.core.session
        return {
            "type": "status",
            "model": s.name,
            "models": self.model_rows(),
            "schema": s.schema,
            "mode": self.core.policy.mode.value,
            "theme": resolve_theme(self.core.ui_theme),
            "dirty": s.dirty,
            "fingerprint": s.fingerprint,
            # The file's own length unit: the viewer measures in SI metres and
            # cannot label a number without it.
            "units": self.units(s),
            # A stable opaque scope keeps browser conversation archives from
            # crossing projects served later on the same local origin without
            # exposing the project path to the page.
            "project_scope": self.project_scope(),
            "etag": self.model_etag(),
            "selection": list(self.selection),
            "selections": self.selection_rows(),
            "highlight": self.last_highlight,
            "color_theme": self.last_color_theme,
            "tabs": self.connected,
            # the viewer only offers the chat dock while the console has it on
            "chat": {"enabled": self.core.chat.enabled},
            # Installed extensions contribute browser UI declaratively.  The
            # viewer shell stays useful on its own and lazy-loads a panel only
            # when both its manifest and enabled state are present.
            "browser_panels": self.core.extensions.browser_panels(),
        }

    def cache_model_bytes(self, etag: str, data: bytes) -> None:
        # Keyed by etag and bounded: with several models resident a single
        # slot would re-serialize on every switch. The byte budget keeps a few
        # small models cached without ever holding several large ones.
        self._model_cache.pop(etag, None)
        self._model_cache[etag] = data
        budget = self.core.settings.viewer.max_model_mb * 1_048_576
        total = sum(len(v) for v in self._model_cache.values())
        while len(self._model_cache) > 1 and (len(self._model_cache) > 3 or total > budget):
            total -= len(self._model_cache.pop(next(iter(self._model_cache))))

    def cached_model_bytes(self, etag: str) -> bytes | None:
        return self._model_cache.get(etag)

    async def _serialize_model(self, etag: str, session: Any) -> bytes:
        # Serialization runs on the model worker: the file object is not
        # thread-safe and edits must not interleave with it.
        data = await session.run(lambda: session.ifc.to_string().encode("utf-8"))
        budget = self.core.settings.viewer.max_model_mb * 1_048_576
        # Bytes from a revision that has already moved on would be served to
        # the next tab under an ETag they no longer describe.
        if len(data) <= budget and self.model_etag(session) == etag:
            self.cache_model_bytes(etag, data)
        return data

    def model_bytes_job(self, etag: str, session: Any) -> asyncio.Future:
        """One serialization per ETag, shared by every tab waiting on it.

        N tabs opening together must queue one job on the single model worker,
        and a caller that gives up must not abandon the work the others need.
        """
        job = self._model_jobs.get(etag)
        if job is not None and not job.done():
            return job
        job = asyncio.ensure_future(self._serialize_model(etag, session))

        def forget(done: asyncio.Future) -> None:
            if self._model_jobs.get(etag) is done:
                del self._model_jobs[etag]

        self._model_jobs[etag] = job
        job.add_done_callback(forget)
        return job

    # -- incoming frames -----------------------------------------------------------
    async def handle_frame(self, client: ViewerClient, frame: dict) -> None:
        ftype = frame.get("type")
        client.touch()
        if ftype == "selection":
            supplied_guids = frame.get("guids")
            guids = (
                [
                    guid
                    for guid in supplied_guids
                    if isinstance(guid, str) and 0 < len(guid) <= _MAX_GUID_LENGTH
                ][:500]
                if isinstance(supplied_guids, list)
                else []
            )
            requested_model = frame.get("model_id")
            if "model_id" not in frame:
                model_id = self.core.models.active_id
            elif requested_model is None:
                model_id = None
            elif (
                isinstance(requested_model, str)
                and len(requested_model) <= _MAX_MODEL_ID_LENGTH
                and self.core.models.get(requested_model) is not None
            ):
                model_id = requested_model
            else:
                guids = []
                model_id = None
            client.selection = guids
            client.selection_model_id = model_id
            client.view_model_id = model_id
            client.selected_at = _utcnow()
            client.selection_order = next(self._selection_updates)
            previous_selection_models = set(client.selections)
            supplied_selections = frame.get("selections")
            selections: dict[str, list[str]] = {}
            if isinstance(supplied_selections, list):
                for item in supplied_selections[:32]:
                    if not isinstance(item, dict):
                        continue
                    selected_model = item.get("model_id")
                    supplied = item.get("guids")
                    if (
                        not isinstance(selected_model, str)
                        or len(selected_model) > _MAX_MODEL_ID_LENGTH
                        or self.core.models.get(selected_model) is None
                        or not isinstance(supplied, list)
                    ):
                        continue
                    cleaned = [
                        guid
                        for guid in supplied
                        if isinstance(guid, str) and 0 < len(guid) <= _MAX_GUID_LENGTH
                    ][:500]
                    if cleaned:
                        selections[selected_model] = list(dict.fromkeys(cleaned))
            elif model_id is not None and guids:
                # Older viewers publish only the current model.
                selections[model_id] = list(dict.fromkeys(guids))
            if model_id is not None and guids and model_id not in selections:
                selections[model_id] = list(dict.fromkeys(guids))
            for selected_model in previous_selection_models | set(selections):
                client.selection_versions[selected_model] = client.selection_order
            client.selections = selections
            self._adopt_selection(client)
            self._restore_model_selections()
            self.core.events.emit(
                "viewer_selection",
                guids=guids,
                count=len(guids),
                model_id=model_id,
                selections=self.selection_rows(),
            )
        elif ftype == "measurements":
            requested_model = frame.get("model_id")
            active_id = self.core.models.active_id
            if requested_model is not None and requested_model != active_id:
                # A tab showing another model must not publish over the
                # measurements taken on the active one.
                return
            client.measurements = _clean_measurements(frame.get("items"))
            client.measured_at = _utcnow()
            client.measurement_model_id = active_id
            client.measurement_order = next(self._selection_updates)
            self.core.events.emit(
                "viewer_measurements", count=len(client.measurements), model_id=active_id
            )
        elif ftype == "scene_state":
            state = frame.get("state")
            if state in ("rebuilding", "ready"):
                client.scene_state = state
        elif ftype == "viewer_state":
            viewport = _clean_viewport_state(frame.get("state") or frame)
            if viewport is not None:
                shown = viewport.get("model_id")
                if "model_id" in viewport and shown is None:
                    client.view_model_id = None
                elif shown is not None and self.core.models.get(shown) is not None:
                    client.view_model_id = shown
                client.viewport = viewport
                client.viewport_at = _utcnow()
        elif ftype == "command_result":
            pending = self._commands.get(str(frame.get("id")))
            if pending is not None:
                target, fut = pending
                if client is target and not fut.done():
                    fut.set_result(frame)
        elif ftype == "screenshot_response":
            pending = self._shots.get(str(frame.get("id")))
            if pending is not None:
                target, model_id, fut = pending
                if (
                    client is target
                    and frame.get("model_id") == model_id
                    and not fut.done()
                ):
                    fut.set_result(frame)
        elif ftype == "pong":
            pass
        elif ftype == "hello":
            pass  # the socket handshake in routes.py consumed the real hello
        else:
            log.debug("viewer client %s sent unknown frame type %r", client.id, ftype)

    # -- outgoing frames -----------------------------------------------------------
    async def broadcast(self, frame: dict) -> None:
        for client in list(self.clients):
            try:
                await client.send(frame)
            except Exception:
                # A send failure means the socket is gone; the receive loop in
                # routes.py will unregister it, we just skip it here.
                log.debug("broadcast to viewer client %s failed", client.id)

    async def send_highlight(
        self,
        guids: list[str],
        *,
        color: str,
        isolate: bool,
        fit: bool,
        clear: bool,
        model_id: str | None = None,
    ) -> None:
        frame = {
            "type": "highlight",
            "guids": guids,
            "color": color,
            "isolate": isolate,
            "fit": fit,
            "clear": clear,
        }
        if model_id is not None:
            frame["model_id"] = model_id
        self.last_highlight = None if clear else frame
        if model_id is None:
            await self.broadcast(frame)
        else:
            await (await self._ready_client(model_id, "a highlight")).send(frame)

    async def send_color_theme(
        self,
        groups: list[dict],
        *,
        title: str,
        clear: bool,
        model_id: str | None = None,
    ) -> None:
        frame = {"type": "color_theme", "title": title, "groups": groups, "clear": clear}
        if model_id is not None:
            frame["model_id"] = model_id
        self.last_color_theme = None if clear else frame
        if model_id is None:
            await self.broadcast(frame)
        else:
            await (await self._ready_client(model_id, "a color theme")).send(frame)

    # -- screenshots -----------------------------------------------------------------
    def _target_client(self, model_id: str | None) -> ViewerClient:
        self.require_connected()
        candidates = [client for client in self.clients if client.view_model_id == model_id]
        if not candidates:
            raise ToolError(
                "VIEWER_NOT_CONNECTED",
                f"no connected viewer tab is showing model {model_id!r}.",
                "Ask the user to open or switch a viewer tab to that model, then retry.",
            )
        # A tab mid-rebuild has disposed its geometry, so it would answer any
        # id-addressed command with "not in this model".
        ready = [client for client in candidates if client.scene_state == "ready"]
        return max(ready or candidates, key=lambda c: c.last_active)

    def _busy_error(self, action: str) -> ToolError:
        return ToolError(
            "VIEWER_BUSY",
            f"the viewer is rebuilding its scene, so it cannot run {action} yet.",
            "The tab reloads the model after a change and has no geometry to "
            "address meanwhile; the GlobalIds are fine. Retry in a moment.",
        )

    async def _ready_client(self, model_id: str | None, action: str) -> ViewerClient:
        """The target tab, once it has a scene to act on."""
        client = self._target_client(model_id)
        if client.scene_state == "ready":
            return client
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _REBUILD_WAIT
        while loop.time() < deadline:
            await asyncio.sleep(_REBUILD_POLL)
            client = self._target_client(model_id)  # raises once the tab is gone
            if client.scene_state == "ready":
                return client
        raise self._busy_error(action)

    async def run_command(
        self,
        action: str,
        params: dict | None = None,
        *,
        model_id: str | None = None,
    ) -> Any:
        """Run one viewer command in the tab showing the requested model.

        Returns whatever the command produced. Raises VIEWER_NOT_CONNECTED if
        no tab is showing the model, VIEWER_BUSY while it rebuilds its scene,
        VIEWER_TIMEOUT if none answers, and VIEWER_ERROR carrying the viewer's
        own message if it refused.
        """
        target_model = model_id or self.core.models.active_id
        client = await self._ready_client(target_model, action)
        command_id = str(next(self._shot_ids))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._commands[command_id] = (client, fut)
        client.commands.add(command_id)
        try:
            await client.send(
                {
                    "type": "command",
                    "id": command_id,
                    "model_id": target_model,
                    "action": action,
                    **(params or {}),
                }
            )
            try:
                frame = await asyncio.wait_for(fut, timeout=_COMMAND_TIMEOUT)
            except asyncio.TimeoutError:
                raise ToolError(
                    "VIEWER_TIMEOUT",
                    f"the viewer did not answer {action} within {_COMMAND_TIMEOUT:.0f}s.",
                    "The viewer tab may be hidden or busy. Ask the user to bring "
                    "it to the foreground, then retry once.",
                ) from None
        finally:
            self._commands.pop(command_id, None)
            client.commands.discard(command_id)

        if not frame.get("ok"):
            error = str(frame.get("error"))[:400]
            # The tab refuses id-addressed work while it rebuilds; that is not
            # a bad argument and must not read like one.
            if "VIEWER_BUSY" in error:
                raise self._busy_error(action)
            raise ToolError(
                "VIEWER_ERROR",
                f"the viewer refused {action}: {error}",
                "Check the arguments against describe_capabilities, then retry.",
            )
        result = frame.get("result")
        # The tab is a client like any other; a reply it cannot have meant
        # should not become an unbounded tool result.
        if len(json.dumps(result, default=str)) > _MAX_COMMAND_RESULT:
            raise ToolError(
                "RESULT_TOO_LARGE",
                f"the viewer returned an oversized result for {action}.",
                "Narrow the request, then retry.",
            )
        return result

    async def request_screenshot(
        self,
        *,
        view: str,
        fit: str | None,
        max_size: int,
        format: str,
        quality: int,
        model_id: str | None = None,
    ) -> tuple[bytes, int, int]:
        """Ask the most recently active tab for a canvas capture.

        Returns (image bytes, width, height). Raises VIEWER_BUSY while the tab
        rebuilds its scene (an empty viewport is worse evidence than none),
        VIEWER_TIMEOUT if no tab answers in time, and VIEWER_ERROR /
        RESULT_TOO_LARGE on bad replies.
        """
        model_id = model_id or self.core.models.active_id
        client = await self._ready_client(model_id, "a screenshot")
        shot_id = str(next(self._shot_ids))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._shots[shot_id] = (client, model_id, fut)
        client.shots.add(shot_id)
        try:
            await client.send(
                {
                    "type": "screenshot_request",
                    "id": shot_id,
                    "model_id": model_id,
                    "view": view,
                    "fit": fit,
                    "max_size": max_size,
                    "format": format,
                    "quality": quality,
                }
            )
            try:
                frame = await asyncio.wait_for(fut, timeout=_SCREENSHOT_TIMEOUT)
            except asyncio.TimeoutError:
                raise ToolError(
                    "VIEWER_TIMEOUT",
                    f"the viewer did not return a screenshot within {_SCREENSHOT_TIMEOUT:.0f}s.",
                    "The viewer tab may be hidden or busy. Ask the user to bring "
                    "it to the foreground, then retry once.",
                ) from None
        finally:
            self._shots.pop(shot_id, None)
            client.shots.discard(shot_id)

        error = frame.get("error")
        if error:
            raise ToolError(
                "VIEWER_ERROR",
                f"the viewer could not capture a screenshot: {str(error)[:500]}",
                "Ask the user to check the viewer tab, then retry.",
            )
        data_b64 = frame.get("data_b64") or ""
        if not isinstance(data_b64, str):
            raise ToolError(
                "VIEWER_ERROR",
                "the viewer returned image data in an invalid format.",
                "Retry once; report a bug if this persists.",
            )
        if len(data_b64) > _MAX_SCREENSHOT_B64:
            raise ToolError(
                "RESULT_TOO_LARGE",
                "the viewer returned an oversized screenshot.",
                "Retry with a smaller max_size or jpeg format.",
            )
        try:
            data = base64.b64decode(data_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError(
                "VIEWER_ERROR",
                "the viewer returned unreadable image data.",
                "Retry once; report a bug if this persists.",
            ) from exc
        if not (data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)):
            raise ToolError(
                "VIEWER_ERROR",
                "the viewer returned data that is not a PNG or JPEG image.",
                "Retry once; report a bug if this persists.",
            )
        try:
            width = int(frame.get("width") or 0)
            height = int(frame.get("height") or 0)
        except (TypeError, ValueError) as exc:
            raise ToolError(
                "VIEWER_ERROR",
                "the viewer returned invalid image dimensions.",
                "Retry once; report a bug if this persists.",
            ) from exc
        if not (
            1 <= width <= _MAX_SCREENSHOT_DIMENSION and 1 <= height <= _MAX_SCREENSHOT_DIMENSION
        ):
            raise ToolError(
                "VIEWER_ERROR",
                "the viewer returned image dimensions outside the accepted range.",
                "Retry with a smaller max_size.",
            )
        return data, width, height

    # -- event bridge -----------------------------------------------------------------
    @staticmethod
    def _geometry_changed(etype: str, event: dict) -> bool:
        """Whether the tab has to re-parse the model, or only re-read text.

        A rebuild costs seconds to minutes on a large file, so skipping it after
        an edit that touched no shape is the whole point. Claiming "no geometry"
        wrongly leaves stale geometry on screen, which is worse than a slow
        reload, so True is the default and only two cases opt out: a save, which
        writes the in-memory model out and changes nothing in it, and a mutation
        whose emitter states it touched no representation.
        """
        if etype == "model_saved":
            return False
        return event.get("geometry") is not False

    def _on_event(self, event: dict) -> None:
        """EventBus subscriber: translate core events into protocol frames.

        Emission is synchronous on the caller's thread; sends are scheduled on
        the running loop. Housekeeping runs even with no clients connected, so
        a tab opened later does not inherit the previous model's state.
        """
        etype = event.get("type")
        reasons = {"model_loaded": "loaded", "model_saved": "saved", "model_mutated": "edited"}
        frame: dict | None = None
        if etype in reasons:
            if etype == "model_loaded":
                self._prune_selection_models()
                self.last_highlight = None
                self.last_color_theme = None
                self._units.clear()
            frame = {
                "type": "model_updated",
                "etag": self.model_etag(),
                "reason": reasons[etype],
                "dirty": self.core.session.dirty,
                "geometry": self._geometry_changed(etype, event),
            }
            touched = _guid_list(event.get("guids"))
            if touched:
                frame["elements"] = touched
        elif etype in (
            "model_attached",
            "model_detached",
            "model_evicted",
            "active_model_changed",
        ):
            # The set of models a tab may show changed; status carries the list.
            if etype in ("model_detached", "model_evicted"):
                self._prune_selection_models()
            frame = self.status_payload()
        elif etype == "mode_changed":
            frame = {"type": "mode_changed", "mode": event.get("mode")}
        elif etype == "theme_changed":
            frame = {"type": "theme", "theme": event.get("theme")}
        if frame is None or not self.clients:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Emitted from a non-loop thread (should not happen by design);
            # dropping a refresh frame is harmless, the next status resyncs.
            return
        loop.create_task(self.broadcast(frame))
        if etype == "model_loaded":
            loop.create_task(self.broadcast(self.status_payload()))
        if etype in ("model_loaded", "model_saved", "model_attached", "active_model_changed"):
            loop.create_task(self._publish_units())

    # -- keepalive ---------------------------------------------------------------------
    async def _ping_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while self.clients:
                await asyncio.sleep(_PING_INTERVAL)
                await self.broadcast({"type": "ping"})
