"""Bounded file-discovery helpers."""

from __future__ import annotations

import io

from ifc_console.mcp.tools_files import _peek_schema


class _BoundedReader(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        assert size == 4096
        return super().read(size)


class _FakePath:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def open(self, mode: str):
        assert mode == "rb"
        return _BoundedReader(self.data)


def test_schema_peek_reads_only_the_file_prefix() -> None:
    path = _FakePath(b"FILE_SCHEMA(('IFC4X3'));" + b"x" * 10_000)

    assert _peek_schema(path) == "IFC4X3"
