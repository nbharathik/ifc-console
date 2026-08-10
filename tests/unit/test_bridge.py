"""The stdio bridge: a client that starts before the console must still work."""

from __future__ import annotations

import http.server
import json
import threading
import urllib.error
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

import ifc_console.bridge as bridge_module
from ifc_console.bridge import Bridge, BridgeIdentityError, BridgeProtocolError, _parse_sse
from ifc_console.http_identity import identity_proof


class _Response:
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self.body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]


class _Opener:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        return self.callback(request, timeout)


def _identity_response(request, token: str = "tok", **changes) -> _Response:
    parts = urlsplit(request.full_url)
    nonce = parse_qs(parts.query, strict_parsing=True)["nonce"][0]
    payload = {
        "name": "ifc-console",
        "version": "test",
        "port": parts.port,
        "nonce": nonce,
        "proof": identity_proof(token, nonce, parts.port),
    }
    payload.update(changes)
    return _Response(json.dumps(payload).encode())


class Offline(Bridge):
    """A bridge whose console is not running."""

    def __init__(self, tmp_path: Path, **kwargs) -> None:
        super().__init__("http://127.0.0.1:8383/mcp", "tok", **kwargs)
        self.written: list[dict] = []
        self.posts: list[dict] = []

    def post(self, payload, timeout=None):
        self.posts.append(payload)
        raise ConnectionRefusedError("console not running")

    def reachable(self) -> bool:
        return False

    def write(self, message: dict) -> None:
        self.written.append(message)

    def _start_watcher(self) -> None:  # no background thread in tests
        pass


class Online(Offline):
    def reachable(self) -> bool:
        return True

    def post(self, payload, timeout=None):
        self.posts.append(payload)
        return {
            "jsonrpc": "2.0",
            "id": payload.get("id"),
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "ifc-console", "version": "test"},
            },
        }


def _request(method: str, request_id: int = 1, **params) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def test_initialize_succeeds_while_the_console_is_down(tmp_path: Path) -> None:
    """The whole point: the client must not mark the server broken."""
    bridge = Offline(tmp_path)
    bridge.handle(_request("initialize", protocolVersion="2025-06-18"))
    result = bridge.written[0]["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "ifc-console"
    assert set(result["capabilities"]) == {"tools", "resources", "prompts"}
    assert "not running" in result["instructions"]


def test_tool_call_while_down_returns_a_readable_hint(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    bridge.handle(_request("tools/call", name="orient", arguments={}))
    result = bridge.written[0]["result"]
    assert result["isError"] is True
    envelope = json.loads(result["content"][0]["text"])
    assert envelope["error"]["code"] == "CONSOLE_NOT_RUNNING"
    assert "ifc-console" in envelope["error"]["hint"]


def test_offline_tool_error_keeps_structured_content(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    bridge._tools = {"tools": [{"name": "orient", "outputSchema": {"type": "object"}}]}
    bridge.handle(_request("tools/call", name="orient", arguments={}))
    result = bridge.written[0]["result"]
    assert result["structuredContent"]["error"]["code"] == "CONSOLE_NOT_RUNNING"


def test_listings_are_empty_not_failed(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    for index, method in enumerate(("tools/list", "resources/list", "prompts/list")):
        bridge.handle(_request(method, request_id=index))
    assert [w["result"] for w in bridge.written] == [
        {"tools": []},
        {"resources": []},
        {"prompts": []},
    ]


def test_notifications_get_no_response(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert bridge.written == []


def test_cached_tool_list_survives_a_cold_start(tmp_path: Path) -> None:
    """Second run with the console down still advertises the real tools."""
    cache = tmp_path / "tools_cache.json"
    warm = Offline(tmp_path, cache_file=cache)
    warm._save_cache({"tools": [{"name": "orient"}, {"name": "query_elements"}]})

    cold = Offline(tmp_path, cache_file=cache)
    cold.handle(_request("tools/list"))
    names = [t["name"] for t in cold.written[0]["result"]["tools"]]
    assert names == ["orient", "query_elements"]


def test_console_appearing_pushes_a_tool_refresh(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    bridge.online = False
    bridge.initialized = True
    bridge._set_online(True)
    assert [message["method"] for message in bridge.written] == [
        "notifications/tools/list_changed",
        "notifications/resources/list_changed",
        "notifications/prompts/list_changed",
    ]
    # flipping again must not spam the client
    bridge._set_online(True)
    assert len(bridge.written) == 3


def test_refresh_waits_until_the_client_is_initialized(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    bridge._set_online(True)
    assert bridge.written == []

    bridge.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert bridge.written[0]["method"] == "notifications/tools/list_changed"


def test_online_initialize_response_precedes_any_notifications(tmp_path: Path) -> None:
    bridge = Online(tmp_path)
    bridge.handle(_request("initialize", protocolVersion="2025-06-18"))
    assert bridge.written == [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "ifc-console", "version": "test"},
            },
        }
    ]


def test_run_reads_line_delimited_json(tmp_path: Path) -> None:
    bridge = Offline(tmp_path)
    stdin = iter([json.dumps(_request("initialize")) + "\n", "\n", "not json\n"])
    assert bridge.run(stdin) == 0
    assert len(bridge.written) == 1  # blank and unparseable lines are skipped


def test_post_authenticates_listener_before_sending_bearer_token(tmp_path: Path) -> None:
    def answer(request, _timeout):
        headers = dict(request.header_items())
        if request.get_method() == "GET":
            assert "Authorization" not in headers
            assert "tok" not in request.full_url
            return _identity_response(request)
        assert headers["Authorization"] == "Bearer tok"
        return _Response(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    bridge = Bridge("http://127.0.0.1:8383/mcp", "tok")
    opener = _Opener(answer)
    bridge._opener = opener

    response = bridge.post(_request("ping"))

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {}}
    assert [request.get_method() for request in opener.requests] == ["GET", "POST"]


def test_post_uses_a_fresh_high_entropy_identity_nonce(tmp_path: Path) -> None:
    nonces: list[str] = []

    def answer(request, _timeout):
        if request.get_method() == "GET":
            nonce = parse_qs(urlsplit(request.full_url).query)["nonce"][0]
            nonces.append(nonce)
            return _identity_response(request)
        return _Response(b'{"jsonrpc":"2.0","id":1,"result":{}}')

    bridge = Bridge("http://127.0.0.1:8383/mcp", "tok")
    bridge._opener = _Opener(answer)
    bridge.post(_request("ping"))
    bridge.post(_request("ping"))

    assert len(set(nonces)) == 2
    assert all(len(nonce) == 64 and int(nonce, 16) >= 0 for nonce in nonces)


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "other-service"},
        {"port": 8384},
        {"nonce": "00" * 32},
        {"proof": "00" * 32},
        {"proof": 123},
    ],
)
def test_post_never_sends_authorization_when_identity_is_invalid(
    tmp_path: Path, changes: dict
) -> None:
    def answer(request, _timeout):
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") is None
        return _identity_response(request, **changes)

    bridge = Bridge("http://127.0.0.1:8383/mcp", "tok")
    opener = _Opener(answer)
    bridge._opener = opener

    with pytest.raises(BridgeIdentityError):
        bridge.post(_request("ping"))

    assert len(opener.requests) == 1


def test_post_rejects_oversized_identity_before_authorization(tmp_path: Path) -> None:
    def answer(request, _timeout):
        assert request.get_header("Authorization") is None
        return _Response(b"x" * 4097)

    bridge = Bridge("http://127.0.0.1:8383/mcp", "tok")
    opener = _Opener(answer)
    bridge._opener = opener

    with pytest.raises(BridgeIdentityError):
        bridge.post(_request("ping"))

    assert len(opener.requests) == 1


def test_post_rejects_oversized_mcp_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "_MAX_HTTP_RESPONSE_BYTES", 16)

    def answer(request, _timeout):
        if request.get_method() == "GET":
            return _identity_response(request)
        return _Response(b"x" * 17)

    bridge = Bridge("http://127.0.0.1:8383/mcp", "tok")
    bridge._opener = _Opener(answer)
    with pytest.raises(BridgeProtocolError, match="exceeds 16 bytes"):
        bridge.post(_request("ping"))


def test_authenticated_post_does_not_follow_redirect_with_bearer_token() -> None:
    captured: list[str | None] = []

    class Capture(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            captured.append(self.headers.get("Authorization"))
            self.send_response(204)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, _format, *_args) -> None:
            return

    capture_server = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    capture_port = capture_server.server_address[1]

    class RedirectingConsole(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            nonce = parse_qs(urlsplit(self.path).query)["nonce"][0]
            port = self.server.server_address[1]
            body = json.dumps(
                {
                    "name": "ifc-console",
                    "port": port,
                    "nonce": nonce,
                    "proof": identity_proof("tok", nonce, port),
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self.send_response(307)
            self.send_header("Location", f"http://127.0.0.1:{capture_port}/collect")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, _format, *_args) -> None:
            return

    console_server = http.server.HTTPServer(("127.0.0.1", 0), RedirectingConsole)
    console_port = console_server.server_address[1]
    threads = [
        threading.Thread(target=capture_server.serve_forever, daemon=True),
        threading.Thread(target=console_server.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        bridge = Bridge(f"http://127.0.0.1:{console_port}/mcp", "tok")
        with pytest.raises(urllib.error.HTTPError) as caught:
            bridge.post(_request("ping"))
        assert caught.value.code == 307
    finally:
        console_server.shutdown()
        capture_server.shutdown()
        for thread in threads:
            thread.join(timeout=5)

    assert captured == []


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:8383/mcp",
        "https://127.0.0.1:8383/mcp",
        "http://127.0.0.1:0/mcp",
        "http://127.0.0.1:8383/other",
        "http://user@127.0.0.1:8383/mcp",
        "http://127.0.0.1:8383/mcp?next=evil",
    ],
)
def test_bridge_rejects_noncanonical_targets(url: str) -> None:
    with pytest.raises(ValueError, match="exact loopback"):
        Bridge(url, "tok")


def test_identity_failure_is_reported_without_forwarding(tmp_path: Path) -> None:
    class Unverified(Offline):
        def reachable(self) -> bool:
            return True

        def post(self, payload, timeout=None):
            self.posts.append(payload)
            raise BridgeIdentityError("invalid proof")

    bridge = Unverified(tmp_path)
    bridge.handle(_request("tools/call", name="orient", arguments={}))
    envelope = json.loads(bridge.written[0]["result"]["content"][0]["text"])
    assert envelope["error"]["code"] == "CONSOLE_IDENTITY_FAILED"
    assert "identity check" in envelope["error"]["hint"]


@pytest.mark.parametrize(
    "body,expected",
    [
        ('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n', {"id": 1}),
        ("data: not-json\n", None),
        ("event: ping\n\n", None),
    ],
)
def test_sse_parsing(body: str, expected: dict | None) -> None:
    parsed = _parse_sse(body)
    if expected is None:
        assert parsed is None
    else:
        assert parsed["id"] == expected["id"]
