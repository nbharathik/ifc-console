"""Length-prefixed JSON framing for the parent/worker pipe.

Framing rather than line JSON because a C extension in the worker can write
to fd 1 behind Python's back; the worker moves the protocol to a private fd
and a magic-prefixed header makes any stray byte a hard, visible error
instead of a silently mangled message.
"""

from __future__ import annotations

import json
import struct
from typing import Any, BinaryIO

MAGIC = b"IFCS"
_HEADER = struct.Struct(">4sI")
MAX_FRAME = 16 * 1024 * 1024
# Two independently clipped text fields can share one reply. At one million
# characters each, even JSON's six-byte control-character escapes stay below
# MAX_FRAME with room for metadata and error text.
MAX_EXEC_OUTPUT_CHARS = 1_000_000


class ProtocolError(RuntimeError):
    """The pipe carried something that is not a frame."""


def encode(payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload, ensure_ascii=False, default=str).encode(
        "utf-8", errors="backslashreplace"
    )
    if len(body) > MAX_FRAME:
        raise ProtocolError(f"frame of {len(body)} bytes exceeds the {MAX_FRAME} limit")
    return _HEADER.pack(MAGIC, len(body)) + body


def write_frame(stream: BinaryIO, payload: dict[str, Any]) -> None:
    stream.write(encode(payload))
    stream.flush()


def _read_exactly(stream: BinaryIO, count: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    """Next frame, or None at clean end of stream."""
    header = _read_exactly(stream, _HEADER.size)
    if header is None:
        return None
    magic, length = _HEADER.unpack(header)
    if magic != MAGIC:
        raise ProtocolError(f"bad frame header {magic!r}")
    if length > MAX_FRAME:
        raise ProtocolError(f"frame of {length} bytes exceeds the {MAX_FRAME} limit")
    body = _read_exactly(stream, length)
    if body is None:
        raise ProtocolError("truncated frame")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"undecodable frame: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("frame payload is not an object")
    return payload
