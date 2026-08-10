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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ifc_console import __version__
from ifc_console.http_identity import (
    IDENTITY_NONCE_BYTES,
    IDENTITY_PATH,
    IDENTITY_RESPONSE_LIMIT,
    identity_matches,
)

log = logging.getLogger("ifc-console.bridge")

_TIMEOUT = 300.0  # a big model can make one tool call genuinely slow
# A listening loopback port accepts in microseconds, so this only has to be
# long enough to beat scheduling noise. It matters because some Windows
# firewall setups drop the SYN instead of refusing it, which would otherwise
# make every check while the console is down wait out the whole timeout.
_CONNECT_TIMEOUT = 0.4
_IDENTITY_TIMEOUT = 2.0
_POLL_SECONDS = 3.0
_PROTOCOL_FALLBACK = "2025-06-18"
_MAX_HTTP_RESPONSE_BYTES = 32 * 1024 * 1024

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

_IDENTITY_FAILED = (
    "A process is listening on the configured port, but it did not pass the "
    "ifc-console identity check. Another application may own the port, the "
    "bridge token may be stale, or the bridge and console versions may not "
    "match. Start the matching ifc-console version on the configured port and retry."
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


class BridgeProtocolError(OSError):
    """The loopback peer returned an invalid or oversized response."""


class BridgeIdentityError(BridgeProtocolError):
    """The loopback peer could not prove possession of the bearer token."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Redirects are never part of the bridge/server contract."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _read_bounded(response: Any, limit: int) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise BridgeProtocolError(f"loopback response exceeds {limit} bytes")
    return raw


class Bridge:
    def __init__(self, url: str, token: str, cache_file: Path | None = None) -> None:
        target = urllib.parse.urlsplit(url)
        try:
            port = target.port
        except ValueError as exc:
            raise ValueError("bridge URL has an invalid port") from exc
        if (
            target.scheme != "http"
            or target.hostname is None
            or target.hostname.casefold() not in {"127.0.0.1", "localhost", "::1"}
            or target.username is not None
            or target.password is not None
            or port is None
            or not 1 <= port <= 65535
            or target.path != "/mcp"
            or target.query
            or target.fragment
        ):
            raise ValueError("bridge URL must be an exact loopback http://host:port/mcp URL")
        if (
            not token
            or len(token) > 4096
            or not token.isascii()
            or any(ord(character) < 33 or ord(character) == 127 for character in token)
        ):
            raise ValueError("bridge token must contain 1-4096 visible ASCII characters")
        self.url = url
        self.token = token
        self._target = target
        self._port = port
        self.cache_file = cache_file
        # A plain urlopen() consults the system proxy settings on every call,
        # which costs seconds on Windows and would route a loopback request
        # through a corporate proxy. This opener never uses one.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())
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
    def _verify_console_identity(self, timeout: float = _IDENTITY_TIMEOUT) -> None:
        """Authenticate the listener before offering it the bearer token."""
        nonce = secrets.token_hex(IDENTITY_NONCE_BYTES)
        query = urllib.parse.urlencode({"nonce": nonce})
        identity_url = urllib.parse.urlunsplit(
            (
                self._target.scheme,
                self._target.netloc,
                IDENTITY_PATH,
                query,
                "",
            )
        )
        request = urllib.request.Request(
            identity_url,
            method="GET",
            headers={"Accept": "application/json"},
        )
        try:
            identity_timeout = min(max(float(timeout), 0.05), _IDENTITY_TIMEOUT)
            with self._opener.open(request, timeout=identity_timeout) as response:
                raw = _read_bounded(response, IDENTITY_RESPONSE_LIMIT)
                content_type = response.headers.get("Content-Type", "")
            if content_type.partition(";")[0].strip().casefold() != "application/json":
                raise BridgeIdentityError("identity response is not JSON")
            payload = json.loads(raw.decode("utf-8"))
            if not identity_matches(payload, self.token, nonce, self._port):
                raise BridgeIdentityError("loopback listener identity proof did not match")
        except BridgeIdentityError:
            raise
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise BridgeIdentityError("loopback listener identity check failed") from exc

    def _console_ready(self) -> bool:
        if not self.reachable():
            return False
        try:
            self._verify_console_identity()
            return True
        except BridgeIdentityError as exc:
            log.debug("identity probe failed: %s", exc)
            return False

    def post(self, payload: dict, timeout: float = _TIMEOUT) -> dict | None:
        """One JSON-RPC round trip. None means the request had no response."""
        self._verify_console_identity(timeout)
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
            raw = _read_bounded(response, _MAX_HTTP_RESPONSE_BYTES)
            content_type = response.headers.get("Content-Type", "")
        if not raw:
            return None
        if "text/event-stream" in content_type:
            parsed = _parse_sse(raw.decode("utf-8", errors="replace"))
            if parsed is None and payload.get("id") is not None:
                raise BridgeProtocolError("loopback response contains no valid SSE event")
            return parsed
        parsed = json.loads(raw.decode("utf-8", errors="replace"))
        if not isinstance(parsed, dict):
            raise BridgeProtocolError("loopback response must be a JSON object")
        return parsed

    def reachable(self) -> bool:
        """Is anything listening on the console's port? One TCP connect, no
        HTTP and no token, so it is both the cheapest and the fastest check."""
        try:
            with socket.create_connection(
                (self._target.hostname, self._port), timeout=_CONNECT_TIMEOUT
            ):
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
                if self._console_ready():
                    self._set_online(True)
                    return

        self._watcher = threading.Thread(target=loop, daemon=True, name="ifc-bridge-watch")
        self._watcher.start()

    # -- offline answers ------------------------------------------------------
    def offline_result(
        self, method: str, params: dict, *, reason: str = "CONSOLE_NOT_RUNNING"
    ) -> dict[str, Any]:
        if reason == "CONSOLE_AUTH_FAILED":
            hint = _AUTH_FAILED
        elif reason == "CONSOLE_IDENTITY_FAILED":
            hint = _IDENTITY_FAILED
        else:
            hint = _NOT_RUNNING
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
                        "the configured listener failed the authenticated identity check."
                        if reason == "CONSOLE_IDENTITY_FAILED"
                        else (
                            "the ifc-console session rejected this connection."
                            if reason == "CONSOLE_AUTH_FAILED"
                            else "the ifc-console session is not running."
                        )
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
            if isinstance(exc, BridgeIdentityError):
                reason = "CONSOLE_IDENTITY_FAILED"
                self._set_online(False)
            elif isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
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
