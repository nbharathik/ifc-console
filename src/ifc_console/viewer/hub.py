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
from typing import TYPE_CHECKING, Any

from ifc_console.mcp.envelope import ToolError

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.viewer")

# Keepalive cadence and the ceiling for one screenshot payload. A 2048 px JPEG
# is far below this; the cap only trips when a client misbehaves.
_PING_INTERVAL = 30.0
_SCREENSHOT_TIMEOUT = 10.0
_MAX_SCREENSHOT_B64 = 12_000_000

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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

    def touch(self) -> None:
        self.last_active = next(self._activity)

    async def send(self, frame: dict) -> None:
        await self.ws.send_text(json.dumps(frame, default=str))


class ViewerHub:
    def __init__(self, core: AppCore) -> None:
        self.core = core
        self.clients: list[ViewerClient] = []
        self.selection: list[str] = []
        self.selected_at: str | None = None
        self.last_highlight: dict | None = None
        self._shots: dict[str, asyncio.Future] = {}
        self._shot_ids = itertools.count(1)
        self._ping_task: asyncio.Task | None = None
        # Model bytes are cached per ETag so several tabs (or a reconnect) do
        # not re-serialize the same revision.
        self._model_cache: tuple[str, bytes] | None = None
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

    def unregister(self, client: ViewerClient) -> None:
        # close_all() unregisters first and the socket's finally block
        # unregisters again; only the first call may emit.
        if client not in self.clients:
            return
        self.clients.remove(client)
        self.core.viewer.connected = self.connected
        self.core.events.emit("viewer_disconnected", client_id=client.id, tabs=self.connected)
        if not self.clients and self._ping_task is not None:
            self._ping_task.cancel()
            self._ping_task = None

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
            hint = (
                "Ask the user to open the viewer: type /viewer in the ifc-console "
                "terminal, or start with --viewer and open the printed URL."
                if self.core.viewer.enabled
                else "The viewer is not enabled for this session. Ask the user to "
                "type /viewer in the ifc-console terminal or restart with --viewer."
            )
            raise ToolError(
                "VIEWER_NOT_CONNECTED",
                "no web viewer tab is connected to this session.",
                hint + " Meanwhile query_elements/get_element give the same "
                "information as text.",
            )

    # -- state payloads ----------------------------------------------------------
    def model_etag(self) -> str | None:
        s = self.core.session
        if not s.loaded:
            return None
        return f"{s.fingerprint}-{s.revision}"

    def status_payload(self) -> dict:
        s = self.core.session
        return {
            "type": "status",
            "model": s.name,
            "schema": s.schema,
            "mode": self.core.policy.mode.value,
            "dirty": s.dirty,
            "fingerprint": s.fingerprint,
            "etag": self.model_etag(),
            "selection": list(self.selection),
            "highlight": self.last_highlight,
            "tabs": self.connected,
        }

    def cache_model_bytes(self, etag: str, data: bytes) -> None:
        self._model_cache = (etag, data)

    def cached_model_bytes(self, etag: str) -> bytes | None:
        if self._model_cache and self._model_cache[0] == etag:
            return self._model_cache[1]
        return None

    # -- incoming frames -----------------------------------------------------------
    async def handle_frame(self, client: ViewerClient, frame: dict) -> None:
        ftype = frame.get("type")
        client.touch()
        if ftype == "selection":
            guids = [g for g in frame.get("guids", []) if isinstance(g, str)][:500]
            self.selection = guids
            self.selected_at = _utcnow()
            self.core.viewer.selection = guids
            self.core.events.emit("viewer_selection", guids=guids, count=len(guids))
        elif ftype == "screenshot_response":
            fut = self._shots.get(str(frame.get("id")))
            if fut is not None and not fut.done():
                fut.set_result(frame)
        elif ftype == "pong":
            pass
        elif ftype == "hello":
            pass  # authentication already happened at the upgrade; see routes.py
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
    ) -> None:
        frame = {
            "type": "highlight",
            "guids": guids,
            "color": color,
            "isolate": isolate,
            "fit": fit,
            "clear": clear,
        }
        self.last_highlight = None if clear else frame
        await self.broadcast(frame)

    # -- screenshots -----------------------------------------------------------------
    def _target_client(self) -> ViewerClient:
        self.require_connected()
        return max(self.clients, key=lambda c: c.last_active)

    async def request_screenshot(
        self,
        *,
        view: str,
        fit: str | None,
        max_size: int,
        format: str,
        quality: int,
    ) -> tuple[bytes, int, int]:
        """Ask the most recently active tab for a canvas capture.

        Returns (image bytes, width, height). Raises VIEWER_TIMEOUT if no tab
        answers in time and VIEWER_ERROR / RESULT_TOO_LARGE on bad replies.
        """
        client = self._target_client()
        shot_id = str(next(self._shot_ids))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._shots[shot_id] = fut
        try:
            await client.send(
                {
                    "type": "screenshot_request",
                    "id": shot_id,
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
                    f"the viewer did not return a screenshot within "
                    f"{_SCREENSHOT_TIMEOUT:.0f}s.",
                    "The viewer tab may be hidden or busy. Ask the user to bring "
                    "it to the foreground, then retry once.",
                ) from None
        finally:
            self._shots.pop(shot_id, None)

        if frame.get("error"):
            raise ToolError(
                "VIEWER_ERROR",
                f"the viewer could not capture a screenshot: {frame['error']}",
                "Ask the user to check the viewer tab, then retry.",
            )
        data_b64 = frame.get("data_b64") or ""
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
        width = int(frame.get("width") or 0)
        height = int(frame.get("height") or 0)
        return data, width, height

    # -- event bridge -----------------------------------------------------------------
    def _on_event(self, event: dict) -> None:
        """EventBus subscriber: translate core events into protocol frames.

        Emission is synchronous on the caller's thread; sends are scheduled on
        the running loop. With no clients connected this is a no-op.
        """
        if not self.clients:
            return
        etype = event.get("type")
        reasons = {"model_loaded": "loaded", "model_saved": "saved", "model_mutated": "edited"}
        frame: dict | None = None
        if etype in reasons:
            if etype == "model_loaded":
                self.selection = []
                self.core.viewer.selection = []
                self.last_highlight = None
            frame = {
                "type": "model_updated",
                "etag": self.model_etag(),
                "reason": reasons[etype],
                "dirty": self.core.session.dirty,
            }
        elif etype == "mode_changed":
            frame = {"type": "mode_changed", "mode": event.get("mode")}
        if frame is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Emitted from a non-loop thread (should not happen by design);
            # dropping a refresh frame is harmless, the next status resyncs.
            return
        loop.create_task(self.broadcast(frame))

    # -- keepalive ---------------------------------------------------------------------
    async def _ping_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while self.clients:
                await asyncio.sleep(_PING_INTERVAL)
                await self.broadcast({"type": "ping"})
