"""The chat backend: provider shaping, the tool loop, and what it refuses.

No network anywhere in here. The provider is a generator we control, which is
the whole point of keeping the transport behind one `stream()` function.
"""

from __future__ import annotations

import asyncio
import json
import threading
import urllib.request

import pytest

from ifc_console.agents.chat import providers
from ifc_console.agents.chat.providers import (
    PROVIDERS,
    ProviderError,
    key_source,
    resolve_key,
    to_anthropic_messages,
    to_openai_messages,
)


# ----------------------------------------------------------------- providers
def test_every_provider_is_one_of_the_two_shapes():
    assert set(PROVIDERS) == {"openai", "anthropic", "openrouter", "local"}
    for provider in PROVIDERS.values():
        assert provider.family in ("openai", "anthropic")
        assert provider.base_url.startswith("http")


async def test_local_provider_needs_no_key_and_stays_on_this_machine():
    local = PROVIDERS["local"]
    assert local.needs_key is False
    assert "localhost" in local.base_url


async def test_key_comes_from_the_environment_or_the_call(monkeypatch):
    provider = PROVIDERS["openai"]
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_key(provider) == ""
    assert key_source(provider) is None
    monkeypatch.setenv("OPENAI_API_KEY", "sk-from-env")
    assert resolve_key(provider) == "sk-from-env"
    assert key_source(provider) == "OPENAI_API_KEY"
    assert resolve_key(provider, "sk-explicit") == "sk-explicit"


def test_error_bodies_never_echo_a_key_back(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-supersecretvalue123")
    assert "sk-supersecretvalue123" not in providers.redact(
        "invalid key sk-supersecretvalue123 rejected"
    )


def test_explicit_request_key_is_redacted_without_an_environment_variable(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    key = "pasted-secret-value"
    assert key not in providers.redact(f"provider reflected {key}", (key,))


def test_openrouter_model_capabilities_are_preserved(monkeypatch):
    payload = {
        "data": [
            {
                "id": "vision-tools",
                "supported_parameters": ["tools", "temperature"],
                "architecture": {"input_modalities": ["text", "image"]},
            },
            {
                "id": "text-only",
                "supported_parameters": ["temperature"],
                "architecture": {"input_modalities": ["text"]},
            },
        ]
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(payload).encode()

    monkeypatch.setattr(providers, "_request", lambda *_args, **_kwargs: Response())
    models = providers.list_models(PROVIDERS["openrouter"], "test-key")

    assert list(models) == ["text-only", "vision-tools"]
    assert models.details["vision-tools"] == {"tools": True, "vision": True}
    assert models.details["text-only"] == {"tools": False, "vision": False}


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "http://worker.localhost:8000/v1",
    ],
)
def test_local_only_accepts_only_loopback_urls(url):
    assert providers.validate_base_url(url, local_only=True) == url


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com/v1",
        "http://127.0.0.1@evil.example/v1",
        "http://10.0.0.5:8000/v1",
        "file:///tmp/provider",
    ],
)
def test_local_only_rejects_remote_or_unsafe_urls(url):
    with pytest.raises(ProviderError):
        providers.validate_base_url(url, local_only=True)


def test_provider_base_url_rejects_credentials_queries_and_fragments():
    for url in (
        "https://user:pass@example.com/v1",
        "https://example.com/v1?key=value",
        "https://example.com/v1#fragment",
    ):
        with pytest.raises(ProviderError):
            providers.validate_base_url(url)


def test_provider_redirects_cannot_change_origin_or_downgrade_transport():
    handler = providers._SafeRedirectHandler(local_only=False)
    request = urllib.request.Request(
        "https://provider.example/v1/chat",
        headers={"Authorization": "Bearer test-secret"},
    )

    redirected = handler.redirect_request(
        request,
        None,
        302,
        "Found",
        {},
        "https://provider.example/v2/chat",
    )
    assert redirected is not None
    with pytest.raises(ProviderError, match="changed origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://collector.example/v1/chat",
        )
    with pytest.raises(ProviderError, match="changed origin"):
        handler.redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "http://provider.example/v1/chat",
        )


def test_openai_messages_carry_tool_calls_and_results():
    turns = [
        {"role": "user", "text": "how many walls?"},
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [
                {"id": "c1", "name": "query_elements", "arguments": '{"query":"IfcWall"}'}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "text": '{"ok":true}'},
    ]
    out = to_openai_messages("be brief", turns)
    assert out[0] == {"role": "system", "content": "be brief"}
    assert out[2]["tool_calls"][0]["function"]["name"] == "query_elements"
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": '{"ok":true}'}


def test_anthropic_messages_use_content_blocks():
    turns = [
        {"role": "user", "text": "how many walls?"},
        {
            "role": "assistant",
            "text": "checking",
            "tool_calls": [
                {"id": "c1", "name": "query_elements", "arguments": '{"query":"IfcWall"}'}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "text": '{"ok":true}'},
    ]
    out = to_anthropic_messages(turns)
    assert out[1]["content"][1]["type"] == "tool_use"
    assert out[1]["content"][1]["input"] == {"query": "IfcWall"}
    assert out[2]["content"][0]["type"] == "tool_result"
    assert out[2]["role"] == "user", "Anthropic carries tool results on a user turn"


def test_anthropic_tool_call_with_broken_json_still_shapes():
    turns = [
        {
            "role": "assistant",
            "text": "",
            "tool_calls": [{"id": "c1", "name": "x", "arguments": "{oops"}],
        }
    ]
    out = to_anthropic_messages(turns)
    assert out[0]["content"][0]["input"] == {}


def test_sse_lines_split_events_and_data():
    body = [
        b"event: content_block_delta\n",
        b'data: {"a":1}\n',
        b"\n",
        b"data: [DONE]\n",
    ]
    pairs = list(providers._sse_lines(iter(body)))
    assert pairs == [("content_block_delta", '{"a":1}'), ("", "[DONE]")]


def test_sse_lines_reject_oversized_provider_frames(monkeypatch):
    monkeypatch.setattr(providers, "_MAX_SSE_LINE", 8)
    with pytest.raises(ProviderError, match="oversized"):
        list(providers._sse_lines(iter([b"data: 123456789\n"])))


def test_local_only_rechecks_dns_resolution_before_a_request(monkeypatch):
    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (providers.socket.AF_INET, providers.socket.SOCK_STREAM, 6, "", ("10.0.0.5", 80))
        ],
    )
    with pytest.raises(ProviderError, match="outside loopback"):
        providers._require_loopback_resolution("http://localhost:8000/v1")


def test_local_only_accepts_only_all_loopback_dns_answers(monkeypatch):
    monkeypatch.setattr(
        providers.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                providers.socket.AF_INET,
                providers.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 8000),
            ),
            (
                providers.socket.AF_INET6,
                providers.socket.SOCK_STREAM,
                6,
                "",
                ("::1", 8000, 0, 0),
            ),
        ],
    )
    providers._require_loopback_resolution("http://localhost:8000/v1")


# ------------------------------------------------------------------- stopping
async def test_disabling_chat_drops_the_session_key(core):
    core.enable_chat()
    core.chat.keys["openai"] = "sk-test"
    core.disable_chat()
    assert core.chat.keys == {}
    assert core.chat.enabled is False


async def test_stopping_a_generation_releases_the_provider(monkeypatch):
    """Stop closes the async generator; the provider thread must let go."""
    closed = threading.Event()

    def endless(provider, **kwargs):
        cancel = kwargs.get("cancel")
        try:
            for index in range(100_000):
                if cancel is not None and cancel.is_set():
                    return
                yield {"type": "content", "text": f"tok{index} "}
        finally:
            closed.set()

    monkeypatch.setattr(providers, "stream", endless)
    events = providers.astream(
        PROVIDERS["openai"],
        base_url="https://api.openai.com/v1",
        key="sk-test",
        model="test-model",
        system="",
        turns=[{"role": "user", "text": "hi"}],
        tools=None,
        options={},
    )
    assert (await events.__anext__())["type"] == "content"

    await asyncio.wait_for(events.aclose(), timeout=5)
    assert closed.wait(timeout=5), "the provider stream was never closed"


async def test_astream_cancellation_closes_a_blocked_response(monkeypatch):
    class BlockingResponse:
        def __init__(self) -> None:
            self.closed = threading.Event()
            self.reading = threading.Event()
            self.reader_exited = threading.Event()
            self.reader: threading.Thread | None = None

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            self.close()

        def readline(self, _limit: int) -> bytes:
            self.reader = threading.current_thread()
            self.reading.set()
            if not self.closed.wait(5):
                raise RuntimeError("response was not closed")
            self.reader_exited.set()
            return b""

        def close(self) -> None:
            self.closed.set()

    response = BlockingResponse()
    monkeypatch.setattr(providers, "_open_stream", lambda *_args, **_kwargs: response)
    events = providers.astream(
        PROVIDERS["openai"],
        base_url="https://api.openai.com/v1",
        key="sk-test",
        model="test-model",
        system="",
        turns=[{"role": "user", "text": "hi"}],
        tools=None,
        options={},
    )
    pending = asyncio.create_task(events.__anext__())
    try:
        for _ in range(500):
            if response.reading.is_set():
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("provider worker never entered the blocking read")

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(pending, timeout=2)

        assert response.closed.is_set()
        assert response.reader_exited.is_set()
        assert response.reader is not None
        assert not response.reader.is_alive()
    finally:
        response.close()
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        await events.aclose()


# ---------------------------------------------------- awkward provider servers
def test_a_server_that_rejects_stream_options_is_retried_without_it():
    payload = {"model": "m", "stream": True, "stream_options": {"include_usage": True}}
    relaxed = providers._relax(payload, "HTTP 400: unknown field stream_options")
    assert relaxed == {"model": "m", "stream": True}


def test_the_newer_openai_token_cap_is_renamed_not_dropped():
    payload = {"model": "m", "max_tokens": 100}
    relaxed = providers._relax(payload, "HTTP 400: use 'max_completion_tokens' instead")
    assert relaxed == {"model": "m", "max_completion_tokens": 100}


def test_a_failure_we_cannot_fix_is_not_retried():
    assert providers._relax({"model": "m"}, "HTTP 401: bad key") is None
    assert providers._relax({"model": "m"}, "HTTP 400: no such model") is None


def test_unsupported_tool_errors_explain_the_model_override():
    error = providers._capability_error(
        {"tools": [{"type": "function"}]},
        "HTTP 400: tool calling is not supported by this model",
    )
    assert error is not None
    assert "Tool calling" in str(error)
    assert "Not supported" in str(error)


class TestToolEvent:
    """What the panel is given to draw under one tool call."""

    def test_a_successful_call_reports_rows_and_a_preview(self) -> None:
        from ifc_console.agents.chat.events import tool_event

        event = tool_event(
            {"ok": True, "data": {"rows": [{"name": "Wall"}]}, "meta": {"returned": 1}}
        )
        assert event["ok"] is True
        assert event["summary"] == "1 row(s)"
        assert event["rows"] == 1
        assert "Wall" in event["preview"]
        assert event["output"]["data"]["rows"] == [{"name": "Wall"}]
        assert event["output"]["meta"]["returned"] == 1
        assert event["detail"] == ""

    def test_a_failure_carries_the_message_not_just_the_code(self) -> None:
        from ifc_console.agents.chat.events import tool_event

        event = tool_event(
            {"ok": False, "error": {"code": "bad_selector", "message": "NotAClass is unknown"}}
        )
        assert event["ok"] is False
        assert event["summary"] == "bad_selector"
        assert event["detail"] == "NotAClass is unknown"
        # prose, not a JSON record: a parser error is full of line breaks and
        # they have to survive into the panel as line breaks
        assert event["preview"].startswith("NotAClass is unknown")
        assert "{" not in event["preview"]

    def test_a_failure_keeps_the_hint_the_console_wrote(self) -> None:
        from ifc_console.agents.chat.events import tool_event

        event = tool_event(
            {
                "ok": False,
                "error": {
                    "code": "INVALID_QUERY",
                    "message": "parse failed:\n\t* DOT",
                    "hint": "Fix the query using the syntax_help examples.",
                },
            }
        )
        assert "parse failed:\n\t* DOT" in event["preview"]
        assert "Hint: Fix the query" in event["preview"]

    def test_image_bytes_never_reach_the_transcript(self) -> None:
        from ifc_console.agents.chat.events import TOOL_PREVIEW_LIMIT, tool_event

        event = tool_event(
            {"ok": True, "data": {"images": [{"bytes": "A" * 50_000}], "count": 1}, "meta": {}}
        )
        assert "AAAA" not in event["preview"]
        assert "AAAA" not in json.dumps(event["output"])
        assert event["output"]["data"]["images"] == "1 image(s)"
        assert "1 image(s)" in event["preview"]
        assert len(event["preview"]) <= TOOL_PREVIEW_LIMIT + 32

    def test_a_huge_payload_says_what_it_left_out(self) -> None:
        from ifc_console.agents.chat.events import TOOL_PREVIEW_LIMIT, tool_event

        event = tool_event({"ok": True, "data": {"rows": [{"n": i} for i in range(400)]}})
        assert len(event["preview"]) <= TOOL_PREVIEW_LIMIT + 32
        # a preview that quietly stopped at 50 rows would read as the answer
        assert "350 more not shown" in event["preview"] or event["preview"].endswith("truncated")
        assert len(event["output"]["data"]["rows"]) == 400

    def test_a_long_string_is_cut_rather_than_pasted_whole(self) -> None:
        from ifc_console.agents.chat.events import TOOL_PREVIEW_LIMIT, tool_event

        event = tool_event({"ok": True, "data": {"text": "x" * 40_000}})
        assert len(event["preview"]) <= TOOL_PREVIEW_LIMIT + 32
