"""The stdio bridge: a client that starts before the console must still work."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ifc_console.bridge import Bridge, _parse_sse


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
