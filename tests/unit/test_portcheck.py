"""Port occupant identification: free vs ifc-console vs foreign app."""

from __future__ import annotations

import http.server
import json
import socket
import threading
from urllib.parse import parse_qs, urlsplit

from ifc_console.portcheck import (
    FOREIGN,
    FREE,
    IFC_CONSOLE,
    IFC_CONSOLE_OTHER,
    classify_http,
    conflict_hint,
    find_free_port,
    port_status,
)

STATUS_BODY = json.dumps({"server": {"name": "ifc-console"}, "model": None})
UNAUTHORIZED_BODY = json.dumps(
    {"error": "unauthorized", "hint": "the token is shown in the ifc-console terminal"}
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --------------------------------------------------------------- free ports
def test_find_free_port_skips_an_occupied_one() -> None:
    """A second same-token session moves itself right past the busy port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        busy = sock.getsockname()[1]
        found = find_free_port(busy - 1, tries=5)
        assert found is not None
        assert found != busy
        assert busy - 1 < found <= busy - 1 + 5


def test_find_free_port_gives_up_inside_its_window() -> None:
    assert find_free_port(65535, tries=5) is None


# ------------------------------------------------------------- classification
def test_classify_running_session() -> None:
    kind, _ = classify_http(200, STATUS_BODY)
    assert kind == IFC_CONSOLE


def test_classify_ifc_code_with_other_token() -> None:
    kind, _ = classify_http(401, UNAUTHORIZED_BODY)
    assert kind == IFC_CONSOLE_OTHER


def test_classify_foreign_app() -> None:
    kind, detail = classify_http(404, "<html>totally different app</html>")
    assert kind == FOREIGN
    assert "404" in detail


# ------------------------------------------------------------------- probing
def test_free_port_reports_free() -> None:
    kind, _ = port_status(_free_port())
    assert kind == FREE


def test_silent_socket_is_foreign() -> None:
    """Something accepts connections but speaks no HTTP: not ifc-console."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        kind, detail = port_status(port)
    assert kind == FOREIGN
    assert "no HTTP answer" in detail


def test_foreign_http_server_identified() -> None:
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        kind, detail = port_status(port)
    finally:
        server.shutdown()
        thread.join(timeout=5)
    assert kind == FOREIGN
    assert "not ifc-console" in detail


def test_spoofed_identity_never_receives_the_bearer_token() -> None:
    seen_authorization: list[str | None] = []

    class Spoof(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen_authorization.append(self.headers.get("Authorization"))
            nonce = parse_qs(urlsplit(self.path).query).get("nonce", [""])[0]
            body = json.dumps(
                {
                    "name": "ifc-console",
                    "port": self.server.server_address[1],
                    "nonce": nonce,
                    "proof": "00" * 32,
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Spoof)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        kind, _detail = port_status(port, "super-secret-token")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    assert kind == IFC_CONSOLE_OTHER
    assert seen_authorization == [None]


# ---------------------------------------------------------------------- hints
def test_hints_are_actionable() -> None:
    assert "--port" in conflict_hint(IFC_CONSOLE, 8383)
    assert "token" in conflict_hint(IFC_CONSOLE_OTHER, 8383)
    foreign = conflict_hint(FOREIGN, 8383)
    assert "server.port" in foreign and "mcp-config" in foreign
