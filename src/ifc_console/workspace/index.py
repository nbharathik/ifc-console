"""WorkspaceIndex: a cheap catalogue of the BIM files under the allowed roots.

Indexing never loads a model. One directory walk plus a bounded header read
per file, so a folder of 400 models costs a walk, not 400 loads.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ifc_console.mcp.envelope import ToolError
from ifc_console.workspace.kinds import KINDS, describe_file, detect_kind, kind

# ISO 19650 style names put the role in a single-letter field; only trust a
# single letter when the stem really looks like that convention.
_DISCIPLINES: tuple[tuple[str, frozenset[str], frozenset[str]], ...] = (
    ("ARC", frozenset({"arc", "arch", "architecture", "architectural", "ar"}), frozenset({"a"})),
    ("STR", frozenset({"str", "struct", "structure", "structural", "st"}), frozenset({"s"})),
    ("MEP", frozenset({"mep", "mech", "mechanical", "hvac", "plumbing"}), frozenset({"m"})),
    ("ELE", frozenset({"ele", "elec", "electrical"}), frozenset({"e"})),
    ("CIV", frozenset({"civ", "civil", "infra"}), frozenset({"c"})),
    ("SIT", frozenset({"sit", "site", "topo", "landscape"}), frozenset({"l"})),
    ("FAC", frozenset({"fac", "facade", "cladding"}), frozenset()),
)
# Every word that means a discipline, so "architecture" finds an ISO 19650
# file whose only clue is the role letter A.
_DISCIPLINE_WORDS: dict[str, str] = {
    word: code for code, words, _ in _DISCIPLINES for word in words
}
_DISCIPLINE_WORDS.update({code.lower(): code for code, _, _ in _DISCIPLINES})

_TOKEN_SPLIT = re.compile(r"[^0-9a-zA-Z]+")
_REVISION_RE = re.compile(r"(?:^|[^a-z])(?:r|rev|v)[\s_-]?(\d{1,3})(?:$|[^0-9])", re.IGNORECASE)
_DATE_RE = re.compile(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})")


def _tokens(stem: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(stem.lower()) if t]


def guess_discipline(stem: str) -> str | None:
    """A hint from the file name. Never a fact: nothing behaves differently."""
    parts = _tokens(stem)
    iso_like = len(parts) >= 5
    for name, words, letters in _DISCIPLINES:
        for token in parts:
            if token in words:
                return name
            if iso_like and token in letters:
                return name
    return None


def guess_revision(stem: str) -> str | None:
    match = _REVISION_RE.search(stem)
    if match:
        return f"r{int(match.group(1))}"
    date = _DATE_RE.search(stem)
    if date:
        return "-".join(date.groups())
    return None


def slug(text: str) -> str:
    out = _TOKEN_SPLIT.sub("-", text.lower()).strip("-")
    return out or "file"


@dataclass(frozen=True)
class FileEntry:
    path: Path
    kind: str
    alias: str
    size_bytes: int
    mtime: str
    detail: dict[str, Any] = field(default_factory=dict)
    discipline: str | None = None
    revision: str | None = None

    @property
    def label(self) -> str:
        k = kind(self.kind)
        return k.label if k else "?"

    @property
    def opens_as(self) -> str:
        k = kind(self.kind)
        return k.opens_as if k else "unknown"


    def to_dict(self) -> dict[str, Any]:
        k = kind(self.kind)
        row: dict[str, Any] = {
            "alias": self.alias,
            "path": str(self.path),
            "name": self.path.name,
            "kind": self.kind,
            "opens_as": self.opens_as,
            "size_bytes": self.size_bytes,
            "mtime": self.mtime,
            "consumed_by": k.consumed_by if k else None,
        }
        if self.discipline:
            row["discipline"] = self.discipline
        if self.revision:
            row["revision"] = self.revision
        if self.detail:
            row.update(self.detail)
        return row


class WorkspaceIndex:
    """Scans the allowed roots for known BIM file kinds.

    `roots` is a callable so the index always sees the live allowed-directory
    list; adding a root stays a user action on AppCore.
    """

    def __init__(
        self,
        roots: Callable[[], Iterable[Path]],
        *,
        cap: int = 10_000,
        depth: int = 3,
        enabled: bool | Callable[[], bool] = True,
    ) -> None:
        self._roots = roots
        self.cap = cap
        self.depth = depth
        self._enabled = enabled
        self.entries: list[FileEntry] = []
        self.scanned_at: str | None = None
        self.truncated = False
        self.primary_root: Path | None = None

    # -- scanning ------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled() if callable(self._enabled) else self._enabled

    def _walk(self, root: Path, budget: int) -> list[Path]:
        try:
            root = root.resolve()
        except OSError:
            return []

        def contained(path: Path) -> Path | None:
            """The resolved path if it really sits under root, else None.

            Returns the resolution so the caller does not repeat the syscall.
            """
            try:
                resolved = path.resolve()
                resolved.relative_to(root)
                return resolved
            except (OSError, ValueError):
                return None

        found: list[Path] = []
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and len(found) < budget:
            current, level = stack.pop()
            try:
                children = sorted(current.iterdir())
            except OSError:
                continue
            for child in children:
                if len(found) >= budget:
                    break
                try:
                    # a symlinked directory could smuggle an outside tree into
                    # the index; the allowed-dir check stays the real gate
                    is_junction = getattr(child, "is_junction", lambda: False)
                    if child.is_symlink() or is_junction():
                        continue
                    resolved = contained(child)
                    if resolved is None:
                        continue
                    if child.is_dir():
                        if level + 1 < self.depth and not child.name.startswith((".", "__")):
                            stack.append((child, level + 1))
                        continue
                    if child.is_file():
                        found.append(resolved)
                except OSError:
                    continue
        return found

    def scan(self) -> list[FileEntry]:
        """Walk every root and rebuild the index. Blocking; call off the UI thread."""
        if not self.enabled:
            raise ToolError(
                "WORKSPACE_DISABLED",
                "workspace indexing is disabled.",
                "Ask the user to run /settings workspace.enabled true in the "
                "ifc-console terminal; it takes effect immediately.",
            )
        seen: set[Path] = set()
        candidates: list[Path] = []
        roots = [self.primary_root] if self.primary_root is not None else list(self._roots())
        for root in roots:
            if len(candidates) >= self.cap:
                break
            try:
                resolved_root = root.resolve()
            except OSError:
                continue
            for resolved in self._walk(root, self.cap - len(candidates)):
                # _walk already resolved and range-checked against its own
                # root; this guard covers the primary_root case where the two
                # roots differ.
                try:
                    resolved.relative_to(resolved_root)
                except ValueError:
                    continue
                if resolved not in seen:
                    seen.add(resolved)
                    candidates.append(resolved)
        self.truncated = len(candidates) >= self.cap

        entries: list[FileEntry] = []
        used: set[str] = set()
        for path in sorted(candidates):
            kind_name = detect_kind(path)
            if kind_name is None:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            alias = slug(path.stem)
            if alias in used:
                n = 2
                while f"{alias}-{n}" in used:
                    n += 1
                alias = f"{alias}-{n}"
            used.add(alias)
            entries.append(
                FileEntry(
                    path=path,
                    kind=kind_name,
                    alias=alias,
                    size_bytes=stat.st_size,
                    mtime=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    detail=describe_file(path, kind_name),
                    discipline=guess_discipline(path.stem),
                    revision=guess_revision(path.stem),
                )
            )
        self.entries = entries
        self.scanned_at = datetime.now(timezone.utc).isoformat()
        return entries

    # -- lookup --------------------------------------------------------------
    def by_kind(self, kinds: Iterable[str] | None = None) -> list[FileEntry]:
        if not kinds:
            return list(self.entries)
        wanted = {k.lower() for k in kinds}
        return [e for e in self.entries if e.kind in wanted]

    def get(self, key: str) -> FileEntry | None:
        """Resolve an alias, a file name, or a path to one entry."""
        needle = key.strip().lower()
        for entry in self.entries:
            if entry.alias == needle:
                return entry
        try:
            target = Path(key).expanduser().resolve()
        except OSError:
            target = None
        for entry in self.entries:
            if entry.path == target or entry.path.name.lower() == needle:
                return entry
        return None

    def _score(self, entry: FileEntry, needle: str) -> float:
        stem = entry.path.stem.lower()
        tokens = _tokens(entry.path.stem)
        if entry.alias == needle or stem == needle:
            return 100.0
        if needle in tokens:
            return 70.0
        if stem.startswith(needle) or entry.alias.startswith(needle):
            return 60.0
        if entry.discipline and entry.discipline == _DISCIPLINE_WORDS.get(needle):
            return 50.0
        if needle in stem:
            return 45.0
        if entry.kind == needle or (kind(entry.kind) or KINDS[0]).label.lower() == needle:
            return 30.0
        if needle in str(entry.path).lower():
            return 20.0
        return 0.0

    def find(
        self, query: str | None = None, kinds: Iterable[str] | None = None, limit: int = 30
    ) -> tuple[list[FileEntry], bool]:
        """Ranked candidates plus an ambiguity flag. Opens nothing."""
        pool = self.by_kind(kinds)
        if not query or not query.strip():
            ranked = sorted(pool, key=lambda e: e.mtime, reverse=True)
            return ranked[:limit], False
        parts = [p for p in _tokens(query) if p]
        scored: list[tuple[float, FileEntry]] = []
        for entry in pool:
            scores = [self._score(entry, part) for part in parts]
            if not scores or min(scores) <= 0:
                continue
            scored.append((sum(scores) / len(scores), entry))
        scored.sort(key=lambda pair: (-pair[0], pair[1].path.name))
        ambiguous = (
            len(scored) > 1 and scored[0][0] > 0 and (scored[0][0] - scored[1][0]) / scored[0][0] < 0.15
        )
        return [entry for _, entry in scored[:limit]], ambiguous

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return {
            "roots": [
                str(r)
                for r in (
                    [self.primary_root]
                    if self.primary_root is not None
                    else list(self._roots())
                )
            ],
            "files": len(self.entries),
            "by_kind": counts,
            "scanned_at": self.scanned_at,
            "truncated": self.truncated,
        }
