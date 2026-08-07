"""LLM providers for the chat panel, over the standard library only.

Two API shapes cover everything we support: the OpenAI chat-completions shape
(OpenAI, OpenRouter, vLLM, LM Studio, Ollama, and anything else that speaks it)
and the Anthropic messages shape. Requests go out with `urllib`, which keeps
the install free of an HTTP client dependency; the blocking work runs on a
worker thread.

Keys are never logged, never written to disk by us, and never put in a URL.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

USER_AGENT = "ifc-console-chat"
# urllib applies one timeout to the connect and to every read, so this has to
# cover the wait for the first token: a local model prefilling a long prompt,
# or a reasoning model thinking, can be quiet for minutes.
STREAM_TIMEOUT = 300.0
# Listing models is a plain GET; nothing justifies a long wait there.
LIST_TIMEOUT = 30.0


@dataclass(frozen=True)
class Provider:
    id: str
    label: str
    family: str  # openai | anthropic
    base_url: str
    key_env: tuple[str, ...] = ()
    suggested_model: str = ""
    needs_key: bool = True
    note: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)


PROVIDERS: dict[str, Provider] = {
    "openai": Provider(
        id="openai",
        label="OpenAI",
        family="openai",
        base_url="https://api.openai.com/v1",
        key_env=("OPENAI_API_KEY",),
        note="Models are listed from your account.",
    ),
    "anthropic": Provider(
        id="anthropic",
        label="Claude (Anthropic)",
        family="anthropic",
        base_url="https://api.anthropic.com/v1",
        key_env=("ANTHROPIC_API_KEY",),
        suggested_model="claude-sonnet-5",
        note="Tool use is native; the model can drive the ifc-console tools.",
    ),
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter",
        family="openai",
        base_url="https://openrouter.ai/api/v1",
        key_env=("OPENROUTER_API_KEY",),
        note="One key, most models. Model ids look like anthropic/claude-sonnet-5.",
        extra_headers={"X-Title": "ifc-console"},
    ),
    "local": Provider(
        id="local",
        label="Local (vLLM, LM Studio, Ollama)",
        family="openai",
        base_url="http://localhost:8000/v1",
        key_env=("LOCAL_LLM_API_KEY",),
        needs_key=False,
        note="Any OpenAI-compatible server. Nothing leaves your machine.",
    ),
}


def resolve_key(provider: Provider, supplied: str | None = None) -> str:
    if supplied:
        return supplied.strip()
    for name in provider.key_env:
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def key_source(provider: Provider) -> str | None:
    """Which environment variable is already set, if any."""
    return next((name for name in provider.key_env if os.environ.get(name)), None)


def _headers(provider: Provider, key: str) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "user-agent": USER_AGENT,
        **provider.extra_headers,
    }
    if provider.family == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["authorization"] = f"Bearer {key}"
    return headers


class ProviderError(RuntimeError):
    """A provider refused the call. The message is safe to show the user."""


def _redact(text: str) -> str:
    """Providers sometimes echo the key back in an error body."""
    out = text
    for value in os.environ.values():
        if len(value) > 12 and value in out:
            out = out.replace(value, "***")
    return out[:600]


def _request(
    url: str,
    headers: dict[str, str],
    payload: dict | None,
    method: str = "POST",
    timeout: float = LIST_TIMEOUT,
):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            body = exc.read().decode("utf-8", "replace")
        detail = ""
        try:
            parsed = json.loads(body)
            detail = (parsed.get("error") or {}).get("message") or parsed.get("message") or ""
        except Exception:
            detail = body
        raise ProviderError(f"HTTP {exc.code}: {_redact(detail) or exc.reason}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(f"cannot reach the provider: {exc.reason}") from None
    except Exception as exc:
        raise ProviderError(f"{type(exc).__name__}: {exc}") from None


def list_models(provider: Provider, key: str, base_url: str | None = None) -> list[str]:
    base = (base_url or provider.base_url).rstrip("/")
    headers = {k: v for k, v in _headers(provider, key).items() if k != "accept"}
    with _request(f"{base}/models", headers, None, method="GET") as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))
    rows = payload.get("data") or payload.get("models") or []
    names = [row.get("id") or row.get("name") for row in rows if isinstance(row, dict)]
    return sorted(name for name in names if name)


def _relax(payload: dict, message: str) -> dict | None:
    """Drop or rename fields a strict server rejected, or None if we can't help.

    The OpenAI shape is a family, not a spec: some servers reject
    `stream_options`, and the newer OpenAI models renamed `max_tokens`. Nothing
    has streamed yet when the request itself fails, so one retry is safe.
    """
    low = message.lower()
    if "http 400" not in low and "http 422" not in low:
        return None
    relaxed = dict(payload)
    if "max_completion_tokens" in low and "max_tokens" in relaxed:
        relaxed["max_completion_tokens"] = relaxed.pop("max_tokens")
    elif "stream_options" in relaxed:
        relaxed.pop("stream_options")
    else:
        return None
    return relaxed


def _open_stream(url: str, headers: dict[str, str], payload: dict, timeout: float):
    try:
        return _request(url, headers, payload, timeout=timeout)
    except ProviderError as first:
        relaxed = _relax(payload, str(first))
        if relaxed is None:
            raise
        try:
            return _request(url, headers, relaxed, timeout=timeout)
        except ProviderError:
            raise first from None  # the first message is the honest one


def _sse_lines(response, cancel: threading.Event | None = None) -> Iterator[tuple[str, str]]:
    """Yield (event, data) pairs from a text/event-stream response.

    Checked against `cancel` between lines: when the user stops a generation we
    have to leave this loop, or the socket stays open and the provider keeps
    billing for tokens nobody will read.
    """
    event = ""
    for raw in response:
        if cancel is not None and cancel.is_set():
            return
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            event = ""
            continue
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            yield event, line[5:].strip()


# --------------------------------------------------------------- openai shape


def _openai_payload(
    model: str, messages: list[dict], tools: list[dict] | None, options: dict
) -> dict:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["input_schema"],
                },
            }
            for tool in tools
        ]
    for key in ("temperature", "top_p", "max_tokens"):
        if options.get(key) is not None:
            payload[key] = options[key]
    return payload


def _stream_openai(
    provider: Provider,
    base: str,
    key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    options: dict,
    cancel: threading.Event | None = None,
    timeout: float = STREAM_TIMEOUT,
) -> Iterator[dict]:
    payload = _openai_payload(model, messages, tools, options)
    calls: dict[int, dict] = {}
    url = f"{base}/chat/completions"
    with _open_stream(url, _headers(provider, key), payload, timeout) as response:
        for _event, data in _sse_lines(response, cancel):
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
                yield {
                    "type": "usage",
                    "in": usage.get("prompt_tokens"),
                    "out": usage.get("completion_tokens"),
                }
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                reasoning = delta.get("reasoning") or delta.get("reasoning_content")
                if reasoning:
                    yield {"type": "reasoning", "text": reasoning}
                if delta.get("content"):
                    yield {"type": "content", "text": delta["content"]}
                for call in delta.get("tool_calls") or []:
                    slot = calls.setdefault(
                        call.get("index", 0), {"id": "", "name": "", "arguments": ""}
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    function = call.get("function") or {}
                    if function.get("name"):
                        slot["name"] = function["name"]
                    if function.get("arguments"):
                        slot["arguments"] += function["arguments"]
                if choice.get("finish_reason"):
                    yield {"type": "finish", "reason": choice["finish_reason"]}
    if calls and not (cancel is not None and cancel.is_set()):
        yield {"type": "tool_calls", "calls": [calls[i] for i in sorted(calls)]}


def to_openai_messages(system: str, turns: list[dict]) -> list[dict]:
    """Normalized transcript to the OpenAI chat shape."""
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    for turn in turns:
        role = turn["role"]
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": turn["tool_call_id"],
                    "content": turn["text"],
                }
            )
        elif role == "assistant" and turn.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": turn.get("text") or None,
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                        for call in turn["tool_calls"]
                    ],
                }
            )
        else:
            out.append({"role": role, "content": turn.get("text") or ""})
    return out


# ------------------------------------------------------------ anthropic shape


def _stream_anthropic(
    provider: Provider,
    base: str,
    key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None,
    options: dict,
    system: str,
    cancel: threading.Event | None = None,
    timeout: float = STREAM_TIMEOUT,
) -> Iterator[dict]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": True,
        "max_tokens": int(options.get("max_tokens") or 8192),
    }
    if system:
        payload["system"] = system
    if tools:
        payload["tools"] = [
            {
                "name": tool["name"],
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
            for tool in tools
        ]
    if options.get("temperature") is not None:
        payload["temperature"] = options["temperature"]

    blocks: dict[int, dict] = {}
    with _open_stream(f"{base}/messages", _headers(provider, key), payload, timeout) as response:
        for event, data in _sse_lines(response, cancel):
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            kind = event or chunk.get("type", "")
            if kind == "content_block_start":
                block = chunk.get("content_block") or {}
                blocks[chunk.get("index", 0)] = {
                    "type": block.get("type"),
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": "",
                }
            elif kind == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "text_delta":
                    yield {"type": "content", "text": delta.get("text", "")}
                elif delta.get("type") == "thinking_delta":
                    yield {"type": "reasoning", "text": delta.get("thinking", "")}
                elif delta.get("type") == "input_json_delta":
                    slot = blocks.setdefault(
                        chunk.get("index", 0), {"type": "tool_use", "arguments": ""}
                    )
                    slot["arguments"] = slot.get("arguments", "") + delta.get("partial_json", "")
            elif kind == "message_delta":
                usage = chunk.get("usage") or {}
                if usage.get("output_tokens"):
                    yield {"type": "usage", "in": usage.get("input_tokens"), "out": usage["output_tokens"]}
                reason = (chunk.get("delta") or {}).get("stop_reason")
                if reason:
                    yield {"type": "finish", "reason": reason}
            elif kind == "error":
                raise ProviderError(_redact(json.dumps(chunk.get("error") or chunk)))
    calls = [
        {"id": b.get("id", ""), "name": b.get("name", ""), "arguments": b.get("arguments") or "{}"}
        for b in blocks.values()
        if b.get("type") == "tool_use"
    ]
    if calls and not (cancel is not None and cancel.is_set()):
        yield {"type": "tool_calls", "calls": calls}


def to_anthropic_messages(turns: list[dict]) -> list[dict]:
    """Normalized transcript to the Anthropic messages shape."""
    out: list[dict] = []
    for turn in turns:
        role = turn["role"]
        if role == "tool":
            content = [
                {
                    "type": "tool_result",
                    "tool_use_id": turn["tool_call_id"],
                    "content": turn["text"],
                }
            ]
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].extend(content)
            else:
                out.append({"role": "user", "content": content})
        elif role == "assistant" and turn.get("tool_calls"):
            content: list[dict] = []
            if turn.get("text"):
                content.append({"type": "text", "text": turn["text"]})
            for call in turn["tool_calls"]:
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                content.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": arguments,
                    }
                )
            out.append({"role": "assistant", "content": content})
        else:
            out.append({"role": role, "content": turn.get("text") or ""})
    return out


# ------------------------------------------------------------------ dispatch


def stream(
    provider: Provider,
    *,
    base_url: str,
    key: str,
    model: str,
    system: str,
    turns: list[dict],
    tools: list[dict] | None,
    options: dict,
    cancel: threading.Event | None = None,
    timeout: float = STREAM_TIMEOUT,
) -> Iterator[dict]:
    """One provider round trip as a stream of normalized events."""
    base = (base_url or provider.base_url).rstrip("/")
    if provider.family == "anthropic":
        yield from _stream_anthropic(
            provider,
            base,
            key,
            model,
            to_anthropic_messages(turns),
            tools,
            options,
            system,
            cancel,
            timeout,
        )
    else:
        yield from _stream_openai(
            provider,
            base,
            key,
            model,
            to_openai_messages(system, turns),
            tools,
            options,
            cancel,
            timeout,
        )
