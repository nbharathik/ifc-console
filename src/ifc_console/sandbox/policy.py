"""The capability policy shared by the parent and the sandbox worker.

Deny by default. The worker may read the model directories, write only into
its own scratch directory, and do nothing else: no network, no process
creation, no arbitrary memory. Everything here is data, so the same object
describes the sandbox in settings, in the audit log, and on the wire.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _norm(path: str | os.PathLike[str]) -> str:
    """Absolute, symlink-resolved, comparable on this platform."""
    try:
        resolved = os.path.realpath(os.fspath(path))
    except (OSError, ValueError, TypeError):
        resolved = str(path)
    return os.path.normcase(resolved)


def _dedupe(paths: list[str]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for p in paths:
        if p:
            seen.setdefault(p)
    return tuple(seen)


def runtime_roots() -> tuple[str, ...]:
    """Directories the interpreter must read to import anything at all.

    Computed in the process it applies to; the worker inherits the parent's
    sys.path, so both sides agree without sending the list over the wire.
    """
    roots = [sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix]
    roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.extend(entry for entry in sys.path if entry)
    return _dedupe([_norm(r) for r in roots])


@dataclass(frozen=True)
class SandboxPolicy:
    """What the worker process is allowed to touch."""

    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()
    # Wins over read_roots: the console's own home holds the bearer token,
    # and a user working from their home directory would otherwise expose it.
    deny_roots: tuple[str, ...] = ()
    allow_network: bool = False
    allow_process: bool = False
    memory_mb: int = 2048

    @classmethod
    def build(
        cls,
        *,
        read_dirs: list[Path],
        scratch_dir: Path,
        deny_dirs: list[Path],
        memory_mb: int,
    ) -> SandboxPolicy:
        scratch = _norm(scratch_dir)
        return cls(
            read_roots=_dedupe([_norm(d) for d in read_dirs] + [scratch]),
            write_roots=(scratch,),
            deny_roots=_dedupe([_norm(d) for d in deny_dirs]),
            memory_mb=memory_mb,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_roots": list(self.read_roots),
            "write_roots": list(self.write_roots),
            "deny_roots": list(self.deny_roots),
            "allow_network": self.allow_network,
            "allow_process": self.allow_process,
            "memory_mb": self.memory_mb,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> SandboxPolicy:
        return cls(
            read_roots=tuple(raw.get("read_roots") or ()),
            write_roots=tuple(raw.get("write_roots") or ()),
            deny_roots=tuple(raw.get("deny_roots") or ()),
            allow_network=bool(raw.get("allow_network")),
            allow_process=bool(raw.get("allow_process")),
            memory_mb=int(raw.get("memory_mb") or 0),
        )

    def summary(self) -> dict[str, Any]:
        """Human-facing description, for /sandbox and the audit log."""
        return {
            "read_roots": len(self.read_roots),
            "write_roots": list(self.write_roots),
            "network": "allowed" if self.allow_network else "blocked",
            "subprocesses": "allowed" if self.allow_process else "blocked",
            "memory_mb": self.memory_mb,
        }
