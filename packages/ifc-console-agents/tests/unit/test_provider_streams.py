"""The provider SSE normalizers, against recorded byte streams.

These two functions turn raw provider frames into the event vocabulary every
agent run consumes. A provider widening its block vocabulary would otherwise
break every run in the field with an empty tool call or a lost answer, and
nothing else in the suite reaches them.
"""

from __future__ import annotations

import pytest

from ifc_console_agents.chat import providers
from ifc_console_agents.chat.providers import PROVIDERS, ProviderError

KEY = "sk-secret-key-12345"


class Resp:
    """A response object that yields recorded frames and closes like a file."""

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)

    def __iter__(self) -> Resp:
        return self

    def __next__(self) -> bytes:
        return next(self._lines)

    def __enter__(self) -> Resp:
        return self

    def __exit__(self, *_exc) -> bool:
        return False


def _frames(*chunks: str) -> list[bytes]:
    lines: list[bytes] = []
    for chunk in chunks:
        for line in chunk.splitlines():
            lines.append(line.encode("utf-8") + b"\n")
        lines.append(b"\n")
    return lines


@pytest.fixture
def recorded(monkeypatch):
    def install(*chunks: str):
        response = Resp(_frames(*chunks))
        monkeypatch.setattr(providers, "_open_stream", lambda *a, **k: response)

    return install


def _openai(**overrides) -> list[dict]:
    kwargs = {
        "provider": PROVIDERS["openai"],
        "base": "https://api.openai.com/v1",
        "key": KEY,
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": None,
        "options": {},
    }
    kwargs.update(overrides)
    return list(providers._stream_openai(**kwargs))


def _anthropic(**overrides) -> list[dict]:
    kwargs = {
        "provider": PROVIDERS["anthropic"],
        "base": "https://api.anthropic.com/v1",
        "key": KEY,
        "model": "claude-test",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": None,
        "options": {},
        "system": "be brief",
    }
    kwargs.update(overrides)
    return list(providers._stream_anthropic(**kwargs))


# ------------------------------------------------------------------- openai


def test_openai_text_reasoning_and_finish_reach_the_loop(recorded):
    recorded(
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{"reasoning":"weighing it"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    )

    events = _openai()

    assert events == [
        {"type": "content", "text": "Hel"},
        {"type": "content", "text": "lo"},
        {"type": "reasoning", "text": "weighing it"},
        {"type": "finish", "reason": "stop"},
    ]


def test_openai_reasoning_content_is_the_same_event(recorded):
    recorded(
        'data: {"choices":[{"delta":{"reasoning_content":"step one"}}]}',
        "data: [DONE]",
    )

    assert _openai() == [{"type": "reasoning", "text": "step one"}]


def test_openai_assembles_one_tool_call_from_argument_fragments(recorded):
    recorded(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_a",'
        '"function":{"name":"query_elements","arguments":"{\\"que"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"ry\\": \\"Ifc"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"Wall\\"}"}}]}}]}',
        "data: [DONE]",
    )

    events = _openai()

    assert events == [
        {
            "type": "tool_calls",
            "calls": [
                {
                    "id": "call_a",
                    "name": "query_elements",
                    "arguments": '{"query": "IfcWall"}',
                }
            ],
        }
    ]


def test_openai_orders_parallel_tool_calls_by_index_not_arrival(recorded):
    recorded(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"b",'
        '"function":{"name":"second","arguments":"{}"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a",'
        '"function":{"name":"first","arguments":"{}"}}]}}]}',
        "data: [DONE]",
    )

    calls = _openai()[0]["calls"]

    assert [call["name"] for call in calls] == ["first", "second"]


def test_openai_usage_maps_to_the_keys_the_agent_loop_reads(recorded):
    recorded(
        'data: {"usage":{"prompt_tokens":31,"completion_tokens":7}}',
        "data: [DONE]",
    )

    assert _openai() == [{"type": "usage", "in": 31, "out": 7}]


def test_openai_skips_frames_it_cannot_parse_instead_of_failing(recorded):
    recorded(
        "data: not json at all",
        "data: [1, 2, 3]",
        'data: {"choices":[{"delta":{"content":"still here"}}]}',
        "data: [DONE]",
    )

    assert _openai() == [{"type": "content", "text": "still here"}]


def test_openai_drops_a_half_streamed_tool_call_when_the_user_stops(recorded):
    import threading

    cancel = threading.Event()
    cancel.set()
    recorded(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a",'
        '"function":{"name":"first","arguments":"{}"}}]}}]}',
        "data: [DONE]",
    )

    assert _openai(cancel=cancel) == []


# ---------------------------------------------------------------- anthropic


def test_anthropic_text_and_thinking_map_to_content_and_reasoning(recorded):
    recorded(
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text"}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"thinking_delta","thinking":"hmm"}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"Hello"}}',
    )

    assert _anthropic() == [
        {"type": "reasoning", "text": "hmm"},
        {"type": "content", "text": "Hello"},
    ]


def test_anthropic_assembles_one_tool_call_from_three_json_fragments(recorded):
    recorded(
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","id":"toolu_1","name":"query_elements"}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{\\"que"}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"ry\\": \\"Ifc"}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"Wall\\"}"}}',
    )

    assert _anthropic() == [
        {
            "type": "tool_calls",
            "calls": [
                {
                    "id": "toolu_1",
                    "name": "query_elements",
                    "arguments": '{"query": "IfcWall"}',
                }
            ],
        }
    ]


def test_anthropic_tool_use_with_no_input_at_all_still_parses(recorded):
    recorded(
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","id":"toolu_2","name":"orient"}}',
    )

    assert _anthropic()[0]["calls"][0]["arguments"] == "{}"


def test_anthropic_input_without_a_block_start_yields_a_nameless_call(recorded):
    """A known gap, pinned so a change to it is a deliberate one.

    An input_json_delta with no content_block_start still produces a call, but
    with no id and no name. The agent loop answers it with a TOOL_NOT_FOUND
    envelope rather than failing, so the run survives; dropping it in the
    normalizer would be better still.
    """
    recorded(
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"input_json_delta","partial_json":"{}"}}',
    )

    assert _anthropic() == [
        {"type": "tool_calls", "calls": [{"id": "", "name": "", "arguments": "{}"}]}
    ]


def test_anthropic_usage_maps_to_the_keys_the_agent_loop_reads(recorded):
    recorded(
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"input_tokens":31,"output_tokens":7}}',
    )

    assert _anthropic() == [
        {"type": "usage", "in": 31, "out": 7},
        {"type": "finish", "reason": "end_turn"},
    ]


def test_anthropic_error_frames_raise_without_echoing_the_key(recorded):
    recorded(
        "event: error\n"
        'data: {"type":"error","error":{"type":"authentication_error",'
        f'"message":"invalid x-api-key {KEY}"}}}}',
    )

    with pytest.raises(ProviderError) as raised:
        _anthropic()

    assert KEY not in str(raised.value)
    assert "authentication_error" in str(raised.value)


def test_anthropic_ignores_a_block_type_it_has_never_seen(recorded):
    recorded(
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"server_tool_use","id":"srv_1","name":"web_search"}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"citations_delta","citation":{"title":"x"}}}',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":1,'
        '"delta":{"type":"text_delta","text":"the answer"}}',
    )

    # an unknown block must not swallow the answer that follows it
    assert {"type": "content", "text": "the answer"} in _anthropic()
