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

from ifc_console.chat import providers
from ifc_console.chat.agent import converse, run_tool
from ifc_console.chat.providers import (
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


# ------------------------------------------------------------------ tool loop
def fake_stream(*scripts):
    """A provider that replays scripted event lists, one per round."""
    rounds = iter(scripts)
    seen: list[dict] = []

    def _stream(provider, **kwargs):
        seen.append(kwargs)
        yield from next(rounds)

    _stream.seen = seen
    return _stream


@pytest.fixture
async def chat_core(core, work_model):
    from ifc_console.mcp.server import build_mcp

    core.start_audit()
    await core.open_model(work_model)
    build_mcp(core)
    core.enable_chat()
    core.chat.model = "test-model"
    core.chat.keys["openai"] = "sk-test"
    return core


async def collect(core, monkeypatch, script, **kwargs):
    monkeypatch.setattr("ifc_console.chat.agent.stream", script)
    return [
        event async for event in converse(core, turns=[{"role": "user", "text": "hi"}], **kwargs)
    ]


async def test_plain_answer_streams_through(chat_core, monkeypatch):
    events = await collect(
        chat_core,
        monkeypatch,
        fake_stream(
            [{"type": "content", "text": "three walls"}, {"type": "finish", "reason": "stop"}]
        ),
    )
    assert [e["type"] for e in events] == ["content", "finish"]


async def test_a_tool_call_runs_and_feeds_the_result_back(chat_core, monkeypatch):
    script = fake_stream(
        [
            {
                "type": "tool_calls",
                "calls": [
                    {"id": "c1", "name": "query_elements", "arguments": '{"query": "IfcWall"}'}
                ],
            }
        ],
        [{"type": "content", "text": "there are three walls"}],
    )
    events = await collect(chat_core, monkeypatch, script)
    kinds = [e["type"] for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["ok"] is True
    # the second round must see the tool output in its transcript
    second = script.seen[1]["turns"]
    assert second[-1]["role"] == "tool"
    assert "IfcWall" in second[-1]["text"]


async def test_the_loop_stops_at_the_round_limit(chat_core, monkeypatch):
    chat_core.store.settings.chat.max_tool_rounds = 2
    call = [
        {
            "type": "tool_calls",
            "calls": [{"id": "c", "name": "get_session_status", "arguments": "{}"}],
        }
    ]
    events = await collect(chat_core, monkeypatch, fake_stream(call, call))
    assert any(e["type"] == "error" and "tool rounds" in e["text"] for e in events)


async def test_tools_can_be_turned_off(chat_core, monkeypatch):
    script = fake_stream([{"type": "content", "text": "no tools for me"}])
    await collect(chat_core, monkeypatch, script, use_tools=False)
    assert script.seen[0]["tools"] == []


async def test_the_tool_schemas_are_the_real_ones(chat_core, monkeypatch):
    script = fake_stream([{"type": "content", "text": "ok"}])
    await collect(chat_core, monkeypatch, script)
    names = {tool["name"] for tool in script.seen[0]["tools"]}
    assert {"query_elements", "get_element", "search_ifc_knowledge"} <= names


async def test_a_broken_tool_call_is_reported_not_raised(chat_core):
    text, info = await run_tool(chat_core, "query_elements", "{not json")
    assert info["ok"] is False and "invalid tool arguments" in text


async def test_an_unknown_tool_is_reported(chat_core):
    text, info = await run_tool(chat_core, "make_coffee", "{}")
    assert info["ok"] is False and "no tool named" in text


async def test_wrong_argument_names_come_back_as_a_message(chat_core):
    text, info = await run_tool(chat_core, "query_elements", '{"selector": "IfcWall"}')
    assert info["ok"] is False and "bad arguments" in text


async def test_tool_results_are_clipped(chat_core, monkeypatch):
    monkeypatch.setattr("ifc_console.chat.agent.TOOL_RESULT_LIMIT", 200)
    text, _info = await run_tool(chat_core, "get_spatial_structure", "{}")
    assert len(text) < 400


async def test_the_ask_mode_gate_still_applies(chat_core):
    text, info = await run_tool(
        chat_core,
        "execute_ifc_code",
        json.dumps(
            {"code": "ifc_api.root.create_entity(ifc, ifc_class='IfcWall')", "description": "x"}
        ),
    )
    assert info["ok"] is False
    assert "ASK_MODE_BLOCKED" in text


async def test_a_missing_key_is_a_clear_error(chat_core, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    chat_core.chat.keys.clear()
    with pytest.raises(ProviderError, match="no API key"):
        await collect(chat_core, monkeypatch, fake_stream([]))


async def test_a_missing_model_is_a_clear_error(chat_core, monkeypatch):
    chat_core.chat.model = ""
    with pytest.raises(ProviderError, match="pick a model"):
        await collect(chat_core, monkeypatch, fake_stream([]))


async def test_local_only_refuses_a_remote_provider(chat_core, monkeypatch):
    chat_core.store.settings.chat.local_only = True
    with pytest.raises(ProviderError, match="local_only"):
        await collect(chat_core, monkeypatch, fake_stream([]))


async def test_local_only_allows_a_local_server(chat_core, monkeypatch):
    chat_core.store.settings.chat.local_only = True
    events = await collect(
        chat_core,
        monkeypatch,
        fake_stream([{"type": "content", "text": "hello"}]),
        provider_id="local",
    )
    assert events[0]["text"] == "hello"


async def test_an_unknown_provider_is_refused(chat_core, monkeypatch):
    with pytest.raises(ProviderError, match="unknown provider"):
        await collect(chat_core, monkeypatch, fake_stream([]), provider_id="nope")


async def test_the_request_is_audited_without_the_key(chat_core, monkeypatch):
    await collect(chat_core, monkeypatch, fake_stream([{"type": "content", "text": "hi"}]))
    records = chat_core.audit.tail(20)
    entry = next(r for r in records if r.get("ev") == "chat_request")
    assert entry["provider"] == "openai" and entry["model"] == "test-model"
    assert "sk-test" not in json.dumps(records)


async def test_disabling_chat_drops_the_session_key(chat_core):
    assert chat_core.chat.keys
    chat_core.disable_chat()
    assert chat_core.chat.keys == {}
    assert chat_core.chat.enabled is False


async def test_the_agent_calls_the_provider_with_its_real_signature(chat_core, monkeypatch):
    """A fake provider swallows **kwargs; the real one does not. Bind them."""
    import inspect

    script = fake_stream([{"type": "content", "text": "ok"}])
    await collect(chat_core, monkeypatch, script)
    signature = inspect.signature(providers.stream)
    signature.bind(PROVIDERS["openai"], **script.seen[0])


# ------------------------------------------------------------------- stopping
async def test_stopping_a_generation_releases_the_provider(chat_core, monkeypatch):
    """The Stop button closes this generator. It used to wedge the worker
    thread on a full queue, leaving the provider streaming into nothing."""
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

    monkeypatch.setattr("ifc_console.chat.agent.stream", endless)
    events = converse(chat_core, turns=[{"role": "user", "text": "hi"}])
    assert (await events.__anext__())["type"] == "content"

    await asyncio.wait_for(events.aclose(), timeout=5)
    assert closed.wait(timeout=5), "the provider stream was never closed"


async def test_the_provider_is_told_when_the_listener_leaves(chat_core, monkeypatch):
    """`cancel` is what lets a blocking urllib read give up mid-stream."""
    script = fake_stream([{"type": "content", "text": "ok"}])
    await collect(chat_core, monkeypatch, script)
    assert isinstance(script.seen[0]["cancel"], threading.Event)


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


async def test_two_calls_to_one_tool_get_separate_ids(chat_core, monkeypatch):
    """Chips are paired to calls by id; by name, a repeated tool lost one."""
    calls = [
        {"id": "", "name": "get_session_status", "arguments": "{}"},
        {"id": "", "name": "get_session_status", "arguments": "{}"},
    ]
    events = await collect(
        chat_core,
        monkeypatch,
        fake_stream([{"type": "tool_calls", "calls": calls}], []),
    )
    ids = [event["id"] for event in events if event["type"] == "tool_call"]
    assert len(ids) == 2 and len(set(ids)) == 2
    results = [event["id"] for event in events if event["type"] == "tool_result"]
    assert results == ids


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
