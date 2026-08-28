"""Project-local reference files shared by the built-in agents.

The managed directory is intentionally boring and inspectable: users may add
files through the browser/CLI or copy them there themselves. The project
knowledge manifest remains the source of truth for indexing, so there is no
second metadata database to drift out of sync.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ifc_console.agents.models import AgentImage
from ifc_console.automation.files import sha256_file
from ifc_console.core.results import ToolError
from ifc_console.knowledge.ingest import IMAGE_SUFFIXES, SUPPORTED_SUFFIXES

MAX_REFERENCE_BYTES = 25 * 1024 * 1024
MAX_PROMPT_IMAGE_BYTES = 4 * 1024 * 1024
REFERENCES_DIR = Path(".ifc-console") / "agents" / "references"
TURN_REFERENCES_DIR = REFERENCES_DIR / ".turns"


def is_turn_reference_path(value: str) -> bool:
    normalized = str(value or "").replace("\\", "/").strip("/")
    prefix = TURN_REFERENCES_DIR.as_posix().strip("/")
    return normalized.startswith(prefix + "/")


class AgentReferenceStore:
    """A small managed folder for documents and images agents may cite."""

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.directory = self.project_dir / REFERENCES_DIR
        # path -> (size, mtime_ns, digest). Listing the library hashes every
        # managed file, and one panel request lists it more than once, so a
        # 20 MB manual was read repeatedly to answer "what files are there".
        self._digests: dict[str, tuple[int, int, str]] = {}

    def _digest(self, path: Path) -> tuple[str, int]:
        """The file's sha256 and size, recomputed only when it changed."""
        stat = path.stat()
        key = str(path)
        cached = self._digests.get(key)
        if cached is not None and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            return cached[2], stat.st_size
        digest = sha256_file(path)
        self._digests[key] = (stat.st_size, stat.st_mtime_ns, digest)
        return digest, stat.st_size

    @staticmethod
    def _safe_name(name: str) -> str:
        clean = Path(name).name.strip()
        suffix = Path(clean).suffix.lower()
        if not clean or clean in {".", ".."} or suffix not in SUPPORTED_SUFFIXES:
            raise ToolError(
                "INVALID_INPUT",
                f"{name!r} is not a supported reference file name.",
                f"Use one of: {', '.join(SUPPORTED_SUFFIXES)}.",
            )
        return clean

    def _available_path(self, name: str, *, directory: Path | None = None) -> Path:
        parent = directory or self.directory
        parent.mkdir(parents=True, exist_ok=True)
        candidate = parent / name
        counter = 2
        while candidate.exists():
            source = Path(name)
            candidate = parent / f"{source.stem}-{counter}{source.suffix.lower()}"
            counter += 1
        return candidate

    def _save_upload(self, name: str, data: bytes, *, directory: Path) -> Path:
        clean = self._safe_name(name)
        if not data:
            raise ToolError("INVALID_INPUT", "the reference file is empty.", "Choose a file.")
        if len(data) > MAX_REFERENCE_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                f"{clean} is larger than the 25 MB reference-file limit.",
                "Use a smaller document or image.",
            )
        target = self._available_path(clean, directory=directory)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def save_upload(self, name: str, data: bytes) -> Path:
        """Atomically store one standing workspace upload."""
        return self._save_upload(name, data, directory=self.directory)

    def save_turn_upload(self, name: str, data: bytes) -> Path:
        """Atomically store one attachment outside the standing library."""
        return self._save_upload(
            name,
            data,
            directory=self.project_dir / TURN_REFERENCES_DIR,
        )

    def add(self, source: str | Path) -> Path:
        """Copy one local reference into the managed folder."""
        path = Path(source).expanduser().resolve()
        if not path.is_file():
            raise ToolError(
                "FILE_NOT_FOUND", f"{path} is not a file.", "Pass a document or image path."
            )
        clean = self._safe_name(path.name)
        size = path.stat().st_size
        if size <= 0 or size > MAX_REFERENCE_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                f"{path.name} must contain data and be no larger than 25 MB.",
                "Use a smaller document or image.",
            )
        if path.parent == self.directory.resolve():
            return path
        target = self._available_path(clean)
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with path.open("rb") as source_handle, temporary.open("xb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def add_paths(self, paths: list[str | Path]) -> list[Path]:
        """Copy supported files from explicit files or directories."""
        added: list[Path] = []
        for raw in paths:
            source = Path(raw).expanduser().resolve()
            if source.is_dir():
                found = [
                    path
                    for path in sorted(source.rglob("*"))
                    if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
                ]
                if not found:
                    raise ToolError(
                        "INVALID_INPUT",
                        f"{source} contains no supported reference files.",
                        f"Use files ending in {', '.join(SUPPORTED_SUFFIXES)}.",
                    )
                added.extend(self.add(path) for path in found)
            else:
                added.append(self.add(source))
        return added

    def paths(self) -> list[Path]:
        if not self.directory.is_dir():
            return []
        return [
            path
            for path in sorted(self.directory.iterdir(), key=lambda item: item.name.casefold())
            if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES
        ]

    def entries(self, indexed_sources: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
        """Describe every managed file and whether the knowledge index matches it."""
        indexed = {
            str(entry.get("path", "")).replace("\\", "/"): str(entry.get("sha256", ""))
            for entry in indexed_sources
        }
        rows: list[dict[str, Any]] = []
        live: set[str] = set()
        for path in self.paths():
            digest, size = self._digest(path)
            live.add(str(path))
            relative = path.relative_to(self.project_dir).as_posix()
            media = "image" if path.suffix.lower() in {".png", ".jpg", ".jpeg"} else "document"
            rows.append(
                {
                    "name": path.name,
                    "path": relative,
                    "media": media,
                    "size_bytes": size,
                    "sha256": digest,
                    "indexed": indexed.get(relative) == digest,
                }
            )
        for stale in self._digests.keys() - live:
            self._digests.pop(stale, None)
        return rows

    def library_entries(self, indexed_sources: list[dict[str, Any]] = ()) -> list[dict[str, Any]]:
        """Describe managed uploads and other project-local indexed content."""
        rows = self.entries(indexed_sources)
        known = {str(row["path"]).replace("\\", "/") for row in rows}
        for source in indexed_sources:
            raw_path = str(source.get("path") or "").replace("\\", "/")
            if not raw_path or raw_path in known:
                continue
            target = Path(raw_path)
            if target.is_absolute():
                try:
                    relative = target.resolve().relative_to(self.project_dir).as_posix()
                except (OSError, ValueError):
                    continue
            else:
                relative = raw_path
                target = self.project_dir / raw_path
            if is_turn_reference_path(relative):
                continue
            try:
                target = target.expanduser().resolve()
                target.relative_to(self.project_dir)
            except (OSError, ValueError):
                continue
            media = str(source.get("media") or "document")
            rows.append(
                {
                    "name": Path(relative).name,
                    "path": relative,
                    "media": media,
                    "size_bytes": target.stat().st_size if target.is_file() else 0,
                    "sha256": str(source.get("sha256") or ""),
                    "indexed": True,
                    "managed": False,
                }
            )
            known.add(relative)
        for row in rows:
            row.setdefault("managed", str(row["path"]).startswith(REFERENCES_DIR.as_posix()))
        return sorted(rows, key=lambda row: str(row["name"]).casefold())

    def sync(self, knowledge: Any) -> dict[str, Any]:
        """Index changed files, including files copied into the folder by hand."""
        paths = self.paths()
        sources = knowledge.sources()
        before = self.entries(sources)
        current = {path.relative_to(self.project_dir).as_posix() for path in paths}
        managed_prefix = REFERENCES_DIR.as_posix().rstrip("/") + "/"
        indexed_managed = {
            str(source.get("path") or "").replace("\\", "/")
            for source in sources
            if str(source.get("path") or "").replace("\\", "/").startswith(managed_prefix)
            and not is_turn_reference_path(str(source.get("path") or ""))
        }
        stale = indexed_managed - current
        needs_repair = bool(sources) and not bool(getattr(knowledge, "ready", True))
        if not stale and not needs_repair and all(entry["indexed"] for entry in before):
            return {
                "changed": False,
                "directory": str(self.directory),
                "files": before,
            }
        report = knowledge.ingest(paths)
        return {
            "changed": True,
            "directory": str(self.directory),
            "files": self.entries(knowledge.sources()),
            "documents": report["documents"],
            "records": report["records"],
            **{
                key: report[key]
                for key in ("instruction_like_chunks", "without_text", "note")
                if key in report
            },
        }

    def prompt_images(
        self,
        paths: list[str],
        indexed_sources: list[dict[str, Any]],
    ) -> tuple[AgentImage, ...]:
        """Load indexed managed images for one user message.

        The browser sends stable project-relative paths, never bytes or an
        arbitrary filesystem path. Hash matching prevents a changed file from
        silently becoming different evidence after ingestion.
        """
        indexed = {
            str(entry.get("path", "")).replace("\\", "/"): entry for entry in indexed_sources
        }
        images: list[AgentImage] = []
        seen: set[str] = set()
        total_bytes = 0
        managed = self.directory.resolve()
        for raw in paths[:8]:
            normalized = str(raw).replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)
            entry = indexed.get(normalized)
            if entry is None or entry.get("media") != "image":
                continue
            target = Path(normalized)
            if not target.is_absolute():
                target = self.project_dir / target
            target = target.expanduser().resolve()
            try:
                target.relative_to(managed)
            except ValueError:
                continue
            if not target.is_file() or target.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            size = target.stat().st_size
            if (
                size > MAX_PROMPT_IMAGE_BYTES
                or total_bytes + size > MAX_PROMPT_IMAGE_BYTES
                or sha256_file(target) != entry.get("sha256")
            ):
                continue
            images.append(AgentImage.from_file(target))
            total_bytes += size
        return tuple(images)


__all__ = [
    "MAX_REFERENCE_BYTES",
    "MAX_PROMPT_IMAGE_BYTES",
    "REFERENCES_DIR",
    "TURN_REFERENCES_DIR",
    "AgentReferenceStore",
    "is_turn_reference_path",
]
