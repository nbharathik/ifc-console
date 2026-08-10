"""HTTP loopback, authentication, and request-size boundaries."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from ifc_console.mcp.server import TokenAuthMiddleware, _loopback_hostport


async def _invoke(
    headers: Iterable[tuple[bytes, bytes]],
    *,
    path: str = "/api/status",
    method: str = "GET",
    body_messages: list[dict] | None = None,
    body_limit: int | None = None,
) -> list[dict]:
    sent: list[dict] = []
    messages = iter(body_messages or [{"type": "http.request", "body": b"", "more_body": False}])

    async def receive() -> dict:
        return next(messages, {"type": "http.disconnect"})

    async def send(message: dict) -> None:
        sent.append(message)

    async def downstream(scope: dict, receive, send) -> None:
        if scope["method"] == "POST":
            while True:
                message = await receive()
                if not message.get("more_body", False):
                    break
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = TokenAuthMiddleware(downstream, "test-token")
    if body_limit is not None:
        middleware.MAX_CHAT_BODY = body_limit
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": list(headers),
    }
    await middleware(scope, receive, send)
    return sent


@pytest.mark.parametrize(
    "value",
    [
        "evil.example",
        "127.0.0.1.evil",
        "[::1].evil",
        "localhost@evil.example",
        "127.0.0.1/path",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        " 127.0.0.1",
    ],
)
def test_loopback_host_parser_rejects_ambiguous_values(value: str) -> None:
    assert _loopback_hostport(value) is False


@pytest.mark.parametrize("value", ["127.0.0.1", "127.0.0.1:8383", "localhost", "[::1]:8383"])
def test_loopback_host_parser_accepts_exact_loopback(value: str) -> None:
    assert _loopback_hostport(value) is True


@pytest.mark.asyncio
async def test_host_is_required_and_may_not_be_duplicated() -> None:
    missing = await _invoke([(b"authorization", b"Bearer test-token")])
    assert missing[0]["status"] == 403

    duplicate = await _invoke(
        [
            (b"host", b"127.0.0.1"),
            (b"host", b"localhost"),
            (b"authorization", b"Bearer test-token"),
        ]
    )
    assert duplicate[0]["status"] == 403


@pytest.mark.asyncio
async def test_origin_and_authorization_may_not_be_duplicated() -> None:
    duplicate_origin = await _invoke(
        [
            (b"host", b"127.0.0.1"),
            (b"origin", b"http://127.0.0.1:8383"),
            (b"origin", b"http://localhost:8383"),
            (b"authorization", b"Bearer test-token"),
        ]
    )
    assert duplicate_origin[0]["status"] == 403

    duplicate_auth = await _invoke(
        [
            (b"host", b"127.0.0.1"),
            (b"authorization", b"Bearer test-token"),
            (b"authorization", b"Bearer wrong"),
        ]
    )
    assert duplicate_auth[0]["status"] == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "origin",
    [
        "http://[::1].evil",
        "http://localhost/path",
        "http://user@localhost",
        "http://localhost:65536",
        "null",
    ],
)
async def test_origin_must_be_an_exact_loopback_origin(origin: str) -> None:
    response = await _invoke(
        [
            (b"host", b"127.0.0.1"),
            (b"origin", origin.encode()),
            (b"authorization", b"Bearer test-token"),
        ]
    )
    assert response[0]["status"] == 403


@pytest.mark.asyncio
async def test_successful_responses_receive_browser_security_headers() -> None:
    response = await _invoke(
        [
            (b"host", b"127.0.0.1"),
            (b"authorization", b"Bearer test-token"),
        ]
    )
    assert response[0]["status"] == 200
    headers = dict(response[0]["headers"])
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"referrer-policy"] == b"no-referrer"
    assert headers[b"x-frame-options"] == b"DENY"
    assert headers[b"cross-origin-resource-policy"] == b"same-origin"


@pytest.mark.asyncio
async def test_chat_body_limit_checks_length_and_streamed_bytes() -> None:
    base_headers = [
        (b"host", b"127.0.0.1"),
        (b"authorization", b"Bearer test-token"),
    ]
    declared = await _invoke(
        [*base_headers, (b"content-length", b"9")],
        path="/api/chat/stream",
        method="POST",
        body_limit=8,
    )
    assert declared[0]["status"] == 413

    streamed = await _invoke(
        base_headers,
        path="/api/chat/stream",
        method="POST",
        body_limit=8,
        body_messages=[
            {"type": "http.request", "body": b"12345", "more_body": True},
            {"type": "http.request", "body": b"6789", "more_body": False},
        ],
    )
    assert streamed[0]["status"] == 413
