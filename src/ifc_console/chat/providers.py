"""LLM providers for the chat panel, over the standard library only.

Two API shapes cover everything we support: the OpenAI chat-completions shape
(OpenAI, OpenRouter, vLLM, LM Studio, Ollama, and anything else that speaks it)
and the Anthropic messages shape. Requests go out with `urllib`, which keeps
the install free of an HTTP client dependency; the blocking work runs on a
worker thread.

Keys are never logged, never written to disk by us, and never put in a URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

USER_AGENT = "ifc-console-chat"
# urllib applies one timeout to the connect and to every read, so this has to
# cover the wait for the first token: a local model prefilling a long prompt,
# or a reasoning model thinking, can be quiet for minutes.
STREAM_TIMEOUT = 300.0
# Listing models is a plain GET; nothing justifies a long wait there.
LIST_TIMEOUT = 30.0
_MAX_ERROR_BODY = 64 * 1024
_MAX_MODELS_BODY = 4 * 1024 * 1024
_MAX_SSE_LINE = 1024 * 1024
_MAX_SSE_TOTAL = 64 * 1024 * 1024
_MAX_MODELS = 10_000
_MAX_MODEL_NAME = 500
_STREAM_WORKER_JOIN_TIMEOUT = 1.0
_QUEUE_DONE = object()
log = logging.getLogger("ifc-console.chat.providers")


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


def _close_stream_response(response: Any) -> None:
    pending = [response]
    seen: set[int] = set()
    while pending and len(seen) < 12:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, socket.socket):
            with contextlib.suppress(OSError):
                current.shutdown(socket.SHUT_RDWR)
            break
        for attribute in ("fp", "raw", "_sock", "sock"):
            with contextlib.suppress(Exception):
                nested = getattr(current, attribute, None)
                if nested is not None:
                    pending.append(nested)
    with contextlib.suppress(Exception):
        response.close()


class _StreamCancellation(threading.Event):
    def __init__(self) -> None:
        super().__init__()
        self._response: Any = None
        self._response_lock = threading.Lock()

    def attach_response(self, response: Any) -> None:
        with self._response_lock:
            close_now = self.is_set()
            if not close_now:
                self._response = response
        if close_now:
            _close_stream_response(response)

    def detach_response(self, response: Any) -> None:
        with self._response_lock:
            if self._response is response:
                self._response = None

    def set(self) -> None:
        super().set()
        with self._response_lock:
            response = self._response
            self._response = None
        if response is not None:
            _close_stream_response(response)


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Providers sometimes echo the key back in an error body."""
    out = text
    candidates = [value for value in os.environ.values() if len(value) > 12]
    candidates.extend(value for value in secrets if len(value) >= 4)
    for value in sorted(set(candidates), key=len, reverse=True):
        if value in out:
            out = out.replace(value, "***")
    return out[:600]


def _validated_url(url: str, *, local_only: bool, base: bool) -> str:
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError as exc:
        raise ProviderError(f"invalid provider URL: {exc}") from None
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ProviderError("provider URL must use http or https")
    if parsed.hostname is None or not parsed.netloc:
        raise ProviderError("provider URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ProviderError("provider URL must not contain credentials")
    if parsed.fragment or (base and parsed.query):
        raise ProviderError("provider base URL must not contain a query or fragment")
    if port is not None and not 1 <= port <= 65535:
        raise ProviderError("provider URL port is outside 1 to 65535")

    host = parsed.hostname.rstrip(".").lower()
    if local_only:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost" or host.endswith(".localhost")
        if not is_loopback:
            raise ProviderError(
                f"chat.local_only is on and {host} is not loopback; "
                "use the local provider or turn the setting off"
            )
    return urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, parsed.path.rstrip("/"), parsed.query, "")
    )


def validate_base_url(url: str, *, local_only: bool = False) -> str:
    """Validate and normalize a configured provider base URL."""
    return _validated_url(url, local_only=local_only, base=True)


def _require_loopback_resolution(url: str) -> None:
    parsed = urlsplit(url)
    host = parsed.hostname
    if host is None:
        raise ProviderError("local provider URL has no host")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProviderError(f"cannot resolve local provider host: {exc}") from None
    addresses = {row[4][0] for row in rows if row[4]}
    if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ProviderError("chat.local_only provider resolved outside loopback")


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, local_only: bool) -> None:
        self.local_only = local_only

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        current = urlsplit(_validated_url(req.full_url, local_only=self.local_only, base=False))
        target = urlsplit(_validated_url(newurl, local_only=self.local_only, base=False))

        def origin(parsed):
            default_port = 443 if parsed.scheme == "https" else 80
            return parsed.scheme, parsed.hostname.lower().rstrip("."), parsed.port or default_port

        # urllib forwards request headers to a redirect target. Refuse any
        # origin change so a provider cannot redirect an Authorization header
        # to another host, port, or weaker transport.
        if origin(current) != origin(target):
            raise ProviderError("provider redirect changed origin and was blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _request(
    url: str,
    headers: dict[str, str],
    payload: dict | None,
    method: str = "POST",
    timeout: float = LIST_TIMEOUT,
    *,
    local_only: bool = False,
    secrets: tuple[str, ...] = (),
):
    url = _validated_url(url, local_only=local_only, base=False)
    if local_only:
        _require_loopback_resolution(url)
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    handlers: list[Any] = [_SafeRedirectHandler(local_only=local_only)]
    if local_only:
        handlers.insert(0, urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        return opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        body = ""
        with contextlib.suppress(Exception):
            raw_body = exc.read(_MAX_ERROR_BODY + 1)
            body = raw_body[:_MAX_ERROR_BODY].decode("utf-8", "replace")
        detail = ""
        try:
            parsed = json.loads(body)
            detail = (parsed.get("error") or {}).get("message") or parsed.get("message") or ""
        except Exception:
            detail = body
        safe_detail = redact(detail, secrets)
        safe_reason = redact(str(exc.reason), secrets)
        raise ProviderError(f"HTTP {exc.code}: {safe_detail or safe_reason}") from None
    except urllib.error.URLError as exc:
        raise ProviderError(
            f"cannot reach the provider: {redact(str(exc.reason), secrets)}"
        ) from None
    except ProviderError:
        raise
    except Exception as exc:
        raise ProviderError(f"{type(exc).__name__}: {redact(str(exc), secrets)}") from None


def list_models(
    provider: Provider,
    key: str,
    base_url: str | None = None,
    *,
    local_only: bool = False,
) -> list[str]:
    base = validate_base_url(base_url or provider.base_url, local_only=local_only)
    headers = {k: v for k, v in _headers(provider, key).items() if k != "accept"}
    with _request(
        f"{base}/models",
        headers,
        None,
        method="GET",
        local_only=local_only,
        secrets=(key,),
    ) as response:
        raw = response.read(_MAX_MODELS_BODY + 1)
        if len(raw) > _MAX_MODELS_BODY:
            raise ProviderError("provider model list exceeded the response-size limit")
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, RecursionError) as exc:
            raise ProviderError(f"provider returned invalid model-list JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise ProviderError("provider model list is not a JSON object")
    rows = payload.get("data") or payload.get("models") or []
    if not isinstance(rows, list):
        raise ProviderError("provider model list has an invalid data field")
    if len(rows) > _MAX_MODELS:
        raise ProviderError("provider returned too many models")
    names = [row.get("id") or row.get("name") for row in rows if isinstance(row, dict)]
    return sorted(
        name for name in names if isinstance(name, str) and 0 < len(name) <= _MAX_MODEL_NAME
    )


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


def _open_stream(
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: float,
    *,
    local_only: bool,
    key: str,
):
    try:
        return _request(
            url,
            headers,
            payload,
            timeout=timeout,
            local_only=local_only,
            secrets=(key,),
        )
    except ProviderError as first:
        relaxed = _relax(payload, str(first))
        if relaxed is None:
            raise
        try:
            return _request(
                url,
                headers,
                relaxed,
                timeout=timeout,
                local_only=local_only,
                secrets=(key,),
            )
        except ProviderError:
            raise first from None  # the first message is the honest one


def _sse_lines(response, cancel: threading.Event | None = None) -> Iterator[tuple[str, str]]:
    """Yield (event, data) pairs from a text/event-stream response.

    Checked against `cancel` between lines: when the user stops a generation we
    have to leave this loop, or the socket stays open and the provider keeps
    billing for tokens nobody will read.
    """
    tracked = isinstance(cancel, _StreamCancellation)
    if tracked:
        cancel.attach_response(response)
    try:
        event = ""
        total = 0
        while True:
            if cancel is not None and cancel.is_set():
                return
            if hasattr(response, "readline"):
                raw = response.readline(_MAX_SSE_LINE + 1)
            else:
                try:
                    raw = next(response)
                except StopIteration:
                    return
            if not raw:
                return
            if len(raw) > _MAX_SSE_LINE:
                raise ProviderError("provider sent an oversized streaming line")
            total += len(raw)
            if total > _MAX_SSE_TOTAL:
                raise ProviderError("provider stream exceeded the response-size limit")
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
    finally:
        if tracked:
            cancel.detach_response(response)


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
    local_only: bool = False,
) -> Iterator[dict]:
    payload = _openai_payload(model, messages, tools, options)
    calls: dict[int, dict] = {}
    url = f"{base}/chat/completions"
    with _open_stream(
        url,
        _headers(provider, key),
        payload,
        timeout,
        local_only=local_only,
        key=key,
    ) as response:
        for _event, data in _sse_lines(response, cancel):
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
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


def _turn_images(turn: dict) -> list[dict]:
    """Valid {media_type, data} image parts a normalized turn carries."""
    images = turn.get("images")
    if not isinstance(images, list):
        return []
    return [
        item
        for item in images
        if isinstance(item, dict)
        and str(item.get("media_type", "")).startswith("image/")
        and isinstance(item.get("data"), str)
    ]


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
            images = _turn_images(turn) if role == "user" else []
            if images:
                content: list[dict] = [{"type": "text", "text": turn.get("text") or ""}]
                content.extend(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"},
                    }
                    for img in images
                )
                out.append({"role": role, "content": content})
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
    local_only: bool = False,
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
    with _open_stream(
        f"{base}/messages",
        _headers(provider, key),
        payload,
        timeout,
        local_only=local_only,
        key=key,
    ) as response:
        for event, data in _sse_lines(response, cancel):
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
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
                    yield {
                        "type": "usage",
                        "in": usage.get("input_tokens"),
                        "out": usage["output_tokens"],
                    }
                reason = (chunk.get("delta") or {}).get("stop_reason")
                if reason:
                    yield {"type": "finish", "reason": reason}
            elif kind == "error":
                raise ProviderError(redact(json.dumps(chunk.get("error") or chunk), (key,)))
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
            images = _turn_images(turn) if role == "user" else []
            if images:
                blocks: list[dict] = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["media_type"],
                            "data": img["data"],
                        },
                    }
                    for img in images
                ]
                if turn.get("text"):
                    blocks.append({"type": "text", "text": turn["text"]})
                # user roles must alternate; fold into an open user turn
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].extend(blocks)
                else:
                    out.append({"role": "user", "content": blocks})
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
    local_only: bool = False,
) -> Iterator[dict]:
    """One provider round trip as a stream of normalized events."""
    base = validate_base_url(base_url or provider.base_url, local_only=local_only)
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
            local_only,
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
            local_only,
        )


async def astream(provider: Provider, *, stream_factory: Any = None, **kwargs: Any):
    """Async bridge over the blocking provider stream.

    Both the embedded chat panel and the public agent SDK use this one bridge,
    so cancellation closes the provider socket and no provider-specific work
    blocks the application event loop.
    """

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Any] = asyncio.Queue()
    cancel = _StreamCancellation()
    slots = threading.Semaphore(64)

    def emit(event: Any) -> bool:
        while not cancel.is_set():
            if slots.acquire(timeout=0.05):
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, event)
                except RuntimeError:
                    slots.release()
                    return False
                return True
        return False

    def pump() -> None:
        try:
            factory = stream_factory or stream
            for event in factory(provider, cancel=cancel, **kwargs):
                if cancel.is_set() or not emit(event):
                    return
        except ProviderError as exc:
            if not cancel.is_set():
                emit({"type": "error", "text": str(exc)})
        except Exception as exc:
            if not cancel.is_set():
                log.error("provider stream failed with %s", type(exc).__name__)
                emit({"type": "error", "text": "internal provider error"})
        finally:
            emit(_QUEUE_DONE)

    worker = threading.Thread(target=pump, name="ifc-console-provider", daemon=True)
    worker.start()
    try:
        while True:
            event = await queue.get()
            slots.release()
            if event is _QUEUE_DONE:
                return
            yield event
    finally:
        cancel.set()
        if worker.is_alive():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.to_thread(worker.join, _STREAM_WORKER_JOIN_TIMEOUT),
                    timeout=_STREAM_WORKER_JOIN_TIMEOUT + 0.1,
                )
        if worker.is_alive():
            log.warning("provider worker did not stop after stream cancellation")
