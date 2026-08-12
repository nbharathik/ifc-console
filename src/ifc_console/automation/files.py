"""File identity helpers used across process boundaries."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ifc_console.core.jobs import SourceFileRef


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe_source(path: Path) -> SourceFileRef:
    resolved = path.expanduser().resolve()
    stat = resolved.stat()
    return SourceFileRef(
        path=str(resolved),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        sha256=sha256_file(resolved),
    )


def source_matches(source: SourceFileRef) -> bool:
    path = Path(source.path)
    try:
        stat = path.stat()
    except OSError:
        return False
    if stat.st_size != source.size_bytes or stat.st_mtime_ns != source.mtime_ns:
        return False
    return sha256_file(path) == source.sha256
