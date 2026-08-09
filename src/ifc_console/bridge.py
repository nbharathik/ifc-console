"""Stdio bridge to a running console: `ifc-console bridge`.

MCP clients start their servers when the client starts. A direct HTTP entry
fails for good when ifc-console is not running yet, and the only cure is
restarting the client. This bridge is a tiny stdio process the client owns: it
always starts, forwards every request to the console over loopback HTTP, and
answers with a clear hint while the console is not there yet. Start order stops
mattering, and the tool list refreshes by itself once the console appears.

Stdlib only, one request at a time, no dependency on the model side of the app.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ifc_console import __version__

log = logging.getLogger("ifc-console.bridge")

_TIMEOUT = 300.0  # a big model can make one tool call genuinely slow
# A listening loopback port accepts in microseconds, so this only has to be
# long enough to beat scheduling noise. It matters because some Windows
# firewall setups drop the SYN instead of refusing it, which would otherwise
# make every check while the console is down wait out the whole timeout.
_CONNECT_TIMEOUT = 0.4
_POLL_SECONDS = 3.0
_PROTOCOL_FALLBACK = "2025-06-18"

_NOT_RUNNING = (
    "ifc-console is not running. Ask the user to start it in a terminal "
    "(`ifc-console`), open a model with /file, then retry; nothing needs "
    "restarting on your side."
)

_AUTH_FAILED = (
    "ifc-console is running but rejected this bridge's token. Ask the user to "
    "check `server.persistent_token` is true (the bridge reads the token file "
    "this machine stores) and to restart ifc-console."
)

# Answers that keep a client happy while the console is down. A client that
# gets an error here marks the whole server broken, which is the bug.
_EMPTY_RESULTS: dict[str, dict[str, Any]] = {
    "tools/list": {"tools": []},
    "resources/list": {"resources": []},
    "resources/templates/list": {"resourceTemplates": []},
    "prompts/list": {"prompts": []},
    "ping": {},
}


class Bridge:
    def __init__(self, url: str, token: str, cache_file: Path | None = None) -> None:
        self.url = url
        self.token = token
        self.cache_file = cache_file
        # A plain urlopen() consults the system proxy settings on every call,
        # which costs seconds on Windows and would route a loopback request
        # through a corporate proxy. This opener never uses one.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.online = False
        self._out_lock = threading.Lock()
        self._tools: dict[str, Any] | None = self._load_cache()
        self._watcher: threading.Thread | None = None
        self.initialized = False
        self._notify_when_initialized = False

    # -- tool-list cache ------------------------------------------------------
    def _load_cache(self) -> dict[str, Any] | None:
        if self.cache_file is None or not self.cache_file.exists():
            return None
        try:
            data = json.loads(self.cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and "tools" in data else None

    def _save_cache(self, result: dict[str, Any]) -> None:
        self._tools = result
        if self.cache_file is None:
            return
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(json.dumps(result), encoding="utf-8")
        except OSError:
            pass  # a cache we cannot write is not worth failing over

    # -- transport ------------------------------------------------------------
    def post(self, payload: dict, timeout: float = _TIMEOUT) -> dict | None:
        """One JSON-RPC round trip. None means the request had no response."""
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self.token}",
            "X-Correlation-ID": f"corr-{secrets.token_hex(16)}",
        }
        if payload.get("id") is not None:
            headers["X-Request-ID"] = str(payload["id"])
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers=headers,
        )
        with self._opener.open(request, timeout=timeout) as response:
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
        if not raw:
            return None
        if "text/event-stream" in content_type:
            return _parse_sse(raw.decode("utf-8", errors="replace"))
        return json.loads(raw.decode("utf-8", errors="replace"))

    def reachable(self) -> bool:
        """Is anything listening on the console's port? One TCP connect, no
        HTTP and no token, so it is both the cheapest and the fastest check."""
        authority = self.url.split("//", 1)[-1].split("/", 1)[0]
        host, _, port = authority.rpartition(":")
        try:
            with socket.create_connection((host, int(port)), timeout=_CONNECT_TIMEOUT):
                return True
        except (OSError, ValueError):
            return False

    # -- stdout ---------------------------------------------------------------
    def write(self, message: dict) -> None:
        with self._out_lock:
            sys.stdout.write(json.dumps(message) + "\n")
            sys.stdout.flush()

    def _write_list_changed(self) -> None:
        for surface in ("tools", "resources", "prompts"):
            self.write({"jsonrpc": "2.0", "method": f"notifications/{surface}/list_changed"})

    def _set_online(self, online: bool, *, notify: bool = True) -> None:
        if online == self.online:
            return
        self.online = online
        if online:
            if notify:
                # The console just appeared: its real lists differ from the
                # placeholders the client is holding.
                if self.initialized:
                    self._notify_when_initialized = False
                    self._write_list_changed()
                else:
                    self._notify_when_initialized = True
        else:
            self._start_watcher()

    def _start_watcher(self) -> None:
        """While the console is down, watch for it so the client learns the
        moment it comes up instead of waiting for the next tool call."""
        if self._watcher is not None and self._watcher.is_alive():
            return

        def loop() -> None:
            while not self.online:
                time.sleep(_POLL_SECONDS)
                if self.reachable():
                    self._set_online(True)
                    return

        self._watcher = threading.Thread(target=loop, daemon=True, name="ifc-bridge-watch")
        self._watcher.start()

    # -- offline answers ------------------------------------------------------
    def offline_result(
        self, method: str, params: dict, *, reason: str = "CONSOLE_NOT_RUNNING"
    ) -> dict[str, Any]:
        hint = _AUTH_FAILED if reason == "CONSOLE_AUTH_FAILED" else _NOT_RUNNING
        if method == "initialize":
            return {
                "protocolVersion": params.get("protocolVersion") or _PROTOCOL_FALLBACK,
                "capabilities": {
                    "tools": {"listChanged": True},
                    "resources": {"subscribe": False, "listChanged": True},
                    "prompts": {"listChanged": True},
                },
                "serverInfo": {"name": "ifc-console", "version": __version__},
                "instructions": hint,
            }
        if method == "tools/list" and self._tools is not None:
            return self._tools
        if method == "tools/call":
            envelope = {
                "ok": False,
                "error": {
                    "code": reason,
                    "message": (
                        "the ifc-console session rejected this connection."
                        if reason == "CONSOLE_AUTH_FAILED"
                        else "the ifc-console session is not running."
                    ),
                    "hint": hint,
                },
                "meta": {"model": None, "mode": "unknown"},
            }
            result = {
                "content": [{"type": "text", "text": json.dumps(envelope, indent=2)}],
                "isError": True,
            }
            tool_name = params.get("name")
            tools = self._tools.get("tools", []) if self._tools else []
            if any(t.get("name") == tool_name and t.get("outputSchema") for t in tools):
                result["structuredContent"] = envelope
            return result
        return _EMPTY_RESULTS.get(method, {})

    # -- request handling -----------------------------------------------------
    def handle(self, message: dict) -> None:
        method = message.get("method") or ""
        request_id = message.get("id")
        reason = "CONSOLE_NOT_RUNNING"
        if method == "notifications/initialized":
            self.initialized = True
            if self.online and self._notify_when_initialized:
                self._notify_when_initialized = False
                self._write_list_changed()
        # Nothing listening: answer now instead of waiting out a connect that
        # a dropped SYN would stretch to the full timeout.
        if not self.online and not self.reachable():
            self._start_watcher()
            if request_id is not None:
                self.write(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "result": self.offline_result(method, message.get("params") or {}),
                    }
                )
            return
        try:
            response = self.post(message)
            self._set_online(True, notify=method != "initialize")
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError) as exc:
            log.debug("forward of %s failed: %s", method, exc)
            # A console that answers 401 is running, just not for this token:
            # saying "not running" would send the user hunting the wrong thing.
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
                reason = "CONSOLE_AUTH_FAILED"
                self._set_online(True)
            else:
                self._set_online(False)
            if request_id is None:
                return  # a notification the console missed; nothing to answer
            self.write(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": self.offline_result(
                        method, message.get("params") or {}, reason=reason
                    ),
                }
            )
            return
        if response is None:
            return  # 202 Accepted: notifications carry no response
        if method == "tools/list" and isinstance(response.get("result"), dict):
            self._save_cache(response["result"])
        self.write(response)

    def run(self, stdin: Any = None) -> int:
        # No startup probe: the first request checks reachability anyway, and
        # the client is waiting on it.
        stream = stdin if stdin is not None else sys.stdin
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self.handle(message)
        return 0


def _parse_sse(text: str) -> dict | None:
    """The streamable-HTTP server answers a request as one SSE data event."""
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                try:
                    return json.loads(payload)
                except json.JSONDecodeError:
                    return None
    return None
