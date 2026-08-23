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

from ifc_console.automation.files import sha256_file
from ifc_console.core.results import ToolError
from ifc_console.knowledge.ingest import SUPPORTED_SUFFIXES

MAX_REFERENCE_BYTES = 25 * 1024 * 1024
REFERENCES_DIR = Path(".ifc-console") / "agents" / "references"


class AgentReferenceStore:
    """A small managed folder for documents and images agents may cite."""

    def __init__(self, project_dir: str | Path) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.directory = self.project_dir / REFERENCES_DIR

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

    def _available_path(self, name: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        candidate = self.directory / name
        counter = 2
        while candidate.exists():
            source = Path(name)
            candidate = self.directory / f"{source.stem}-{counter}{source.suffix.lower()}"
            counter += 1
        return candidate

    def save_upload(self, name: str, data: bytes) -> Path:
        """Atomically store one browser upload without replacing another file."""
        clean = self._safe_name(name)
        if not data:
            raise ToolError("INVALID_INPUT", "the reference file is empty.", "Choose a file.")
        if len(data) > MAX_REFERENCE_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                f"{clean} is larger than the 25 MB reference-file limit.",
                "Use a smaller document or image.",
            )
        target = self._available_path(clean)
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
        for path in self.paths():
            digest = sha256_file(path)
            relative = path.relative_to(self.project_dir).as_posix()
            media = "image" if path.suffix.lower() in {".png", ".jpg", ".jpeg"} else "document"
            rows.append(
                {
                    "name": path.name,
                    "path": relative,
                    "media": media,
                    "size_bytes": path.stat().st_size,
                    "sha256": digest,
                    "indexed": indexed.get(relative) == digest,
                }
            )
        return rows

    def sync(self, knowledge: Any) -> dict[str, Any]:
        """Index changed files, including files copied into the folder by hand."""
        paths = self.paths()
        before = self.entries(knowledge.sources())
        if not paths or all(entry["indexed"] for entry in before):
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


__all__ = ["MAX_REFERENCE_BYTES", "REFERENCES_DIR", "AgentReferenceStore"]
