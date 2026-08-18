"""Conversation storage implementations for the agent runtime."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ifc_console.agents.models import AgentMessage

_RECORD_VERSION = "1"


class InMemoryThreadStore:
    """Process-local threads for examples, tests, and short-lived services."""

    def __init__(self) -> None:
        self._threads: dict[str, tuple[AgentMessage, ...]] = {}
        self._lock = asyncio.Lock()

    async def load(self, thread_id: str) -> Sequence[AgentMessage]:
        async with self._lock:
            return tuple(
                message.model_copy(deep=True) for message in self._threads.get(thread_id, ())
            )

    async def save(self, thread_id: str, messages: Sequence[AgentMessage]) -> None:
        async with self._lock:
            self._threads[thread_id] = tuple(message.model_copy(deep=True) for message in messages)

    async def delete(self, thread_id: str) -> bool:
        async with self._lock:
            return self._threads.pop(thread_id, None) is not None

    async def list_threads(self) -> tuple[str, ...]:
        async with self._lock:
            return tuple(sorted(self._threads))


class ThreadStoreError(RuntimeError):
    """A durable thread record is missing, corrupt, or outside configured limits."""


class JsonThreadStore:
    """Small durable JSON thread store for local services and reference apps.

    Company deployments can implement the same ``ThreadStore`` protocol over a
    database. This implementation keeps each thread in one atomically replaced
    file and never uses the caller's thread id as a path.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        max_messages: int = 500,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if max_messages < 1 or max_bytes < 1024:
            raise ValueError("thread-store limits are too small")
        self.directory = Path(directory).expanduser().resolve()
        self.max_messages = max_messages
        self.max_bytes = max_bytes
        self._lock = asyncio.Lock()

    def _path(self, thread_id: str) -> Path:
        if not thread_id or len(thread_id) > 1000:
            raise ThreadStoreError("thread id must contain 1 to 1000 characters")
        digest = hashlib.sha256(thread_id.encode("utf-8")).hexdigest()
        return self.directory / f"{digest}.json"

    def _decode(self, raw: bytes, thread_id: str) -> tuple[AgentMessage, ...]:
        try:
            payload = json.loads(raw.decode("utf-8"))
            if (
                payload.get("version") != _RECORD_VERSION
                or payload.get("thread_id") != thread_id
            ):
                raise ValueError("record identity does not match")
            messages = payload.get("messages")
            if not isinstance(messages, list):
                raise ValueError("messages is not a list")
            if len(messages) > self.max_messages:
                raise ThreadStoreError("thread record exceeds the configured message limit")
            return tuple(AgentMessage.model_validate(message) for message in messages)
        except ThreadStoreError:
            raise
        except Exception as exc:
            raise ThreadStoreError(f"thread record is corrupt ({type(exc).__name__})") from None

    async def load(self, thread_id: str) -> Sequence[AgentMessage]:
        path = self._path(thread_id)
        async with self._lock:
            if not path.is_file():
                return ()

            def read() -> bytes:
                size = path.stat().st_size
                if size > self.max_bytes:
                    raise ThreadStoreError("thread record exceeds the configured size limit")
                return path.read_bytes()

            raw = await asyncio.to_thread(read)
            return self._decode(raw, thread_id)

    async def save(self, thread_id: str, messages: Sequence[AgentMessage]) -> None:
        if len(messages) > self.max_messages:
            raise ThreadStoreError("thread exceeds the configured message limit")
        path = self._path(thread_id)
        payload: dict[str, Any] = {
            "version": _RECORD_VERSION,
            "thread_id": thread_id,
            "messages": [message.model_dump(mode="json") for message in messages],
        }
        raw = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if len(raw) > self.max_bytes:
            raise ThreadStoreError("thread exceeds the configured size limit")

        async with self._lock:

            def write() -> None:
                self.directory.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
                try:
                    with temporary.open("xb") as handle:
                        handle.write(raw)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)

            await asyncio.to_thread(write)

    async def delete(self, thread_id: str) -> bool:
        path = self._path(thread_id)
        async with self._lock:

            def remove() -> bool:
                if not path.is_file():
                    return False
                path.unlink()
                return True

            return await asyncio.to_thread(remove)

    async def list_threads(self) -> tuple[str, ...]:
        async with self._lock:
            if not self.directory.is_dir():
                return ()

            def scan() -> tuple[str, ...]:
                thread_ids: list[str] = []
                for path in self.directory.glob("*.json"):
                    try:
                        if path.stat().st_size > self.max_bytes:
                            continue
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        if payload.get("version") != _RECORD_VERSION:
                            continue
                        thread_id = payload.get("thread_id")
                        if not isinstance(thread_id, str):
                            continue
                        if path.name != self._path(thread_id).name:
                            continue
                        messages = payload.get("messages")
                        if not isinstance(messages, list) or len(messages) > self.max_messages:
                            continue
                        thread_ids.append(thread_id)
                    except Exception:
                        continue
                return tuple(sorted(set(thread_ids)))

            return await asyncio.to_thread(scan)


__all__ = ["InMemoryThreadStore", "JsonThreadStore", "ThreadStoreError"]
