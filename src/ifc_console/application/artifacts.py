"""Local content-addressed artifact storage."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from ifc_console.application.locks import exclusive_file_lock
from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.results import ToolError
from ifc_console.core.revisions import RevisionRef


class ArtifactService:
    _CHUNK_SIZE = 1 << 20

    def __init__(self, root: Path) -> None:
        self.root = root
        self.content_dir = root / "content"
        self.metadata_dir = root / "metadata"
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def put_text(
        self,
        text: str,
        *,
        name: str,
        kind: str,
        media_type: str,
        producer: str,
        revision: RevisionRef | None = None,
        metadata: dict[str, Any] | None = None,
        references: Iterable[str] = (),
    ) -> ArtifactRef:
        return self.put_bytes(
            text.encode("utf-8"),
            name=name,
            kind=kind,
            media_type=media_type,
            producer=producer,
            revision=revision,
            metadata=metadata,
            references=references,
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        name: str,
        kind: str,
        media_type: str,
        producer: str,
        revision: RevisionRef | None = None,
        metadata: dict[str, Any] | None = None,
        references: Iterable[str] = (),
    ) -> ArtifactRef:
        digest = hashlib.sha256(data).hexdigest()
        ref = self._make_ref(
            digest=digest,
            size_bytes=len(data),
            name=name,
            kind=kind,
            media_type=media_type,
            producer=producer,
            revision=revision,
            metadata=metadata,
            references=references,
        )
        content_path = self._content_path(digest)
        metadata_path = self._metadata_path(digest)
        with self.locked():
            if not content_path.exists():
                self._atomic_write(content_path, data)
            if metadata_path.exists():
                return self._merge_references(self.verify(ref.artifact_id), ref.references)
            else:
                payload = json.dumps(
                    ref.model_dump(mode="json"), indent=2, ensure_ascii=False
                ).encode("utf-8")
                self._atomic_write(metadata_path, payload)
        return ref

    def put_file(
        self,
        source: Path,
        *,
        name: str,
        kind: str,
        media_type: str,
        producer: str,
        revision: RevisionRef | None = None,
        metadata: dict[str, Any] | None = None,
        references: Iterable[str] = (),
        expected_sha256: str | None = None,
    ) -> ArtifactRef:
        """Ingest a file without buffering it in the calling process."""
        source = source.expanduser().resolve()
        if not source.is_file():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"artifact source {source} is unavailable.",
                "Choose an existing regular file.",
            )
        incoming = self.root / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        staged = incoming / f".{secrets.token_hex(12)}.tmp"
        try:
            digest, size_bytes = self._copy_new(source, staged)
            if expected_sha256 is not None and digest != expected_sha256:
                raise ToolError(
                    "SOURCE_CHANGED",
                    f"{source.name} changed while it was being stored as an artifact.",
                    "Retry against a stable source file.",
                )
            ref = self._make_ref(
                digest=digest,
                size_bytes=size_bytes,
                name=name,
                kind=kind,
                media_type=media_type,
                producer=producer,
                revision=revision,
                metadata=metadata,
                references=references,
            )
            content_path = self._content_path(digest)
            metadata_path = self._metadata_path(digest)
            with self.locked():
                if not content_path.exists():
                    content_path.parent.mkdir(parents=True, exist_ok=True)
                    self._replace(staged, content_path)
                if metadata_path.exists():
                    return self._merge_references(self.verify(ref.artifact_id), ref.references)
                else:
                    payload = json.dumps(
                        ref.model_dump(mode="json"), indent=2, ensure_ascii=False
                    ).encode("utf-8")
                    self._atomic_write(metadata_path, payload)
            return ref
        finally:
            if staged.exists():
                staged.unlink()

    def get(self, artifact_id: str) -> ArtifactRef:
        digest = self._digest(artifact_id)
        try:
            payload = self._metadata_path(digest).read_text(encoding="utf-8")
            ref = ArtifactRef.model_validate_json(payload)
        except (OSError, ValueError) as exc:
            raise ToolError(
                "ARTIFACT_NOT_FOUND",
                f"artifact {artifact_id!r} is unavailable.",
                "List artifacts or use an artifact ID returned by a completed job.",
            ) from exc
        if ref.artifact_id != artifact_id or ref.sha256 != digest:
            raise ToolError(
                "ARTIFACT_CORRUPT",
                f"artifact metadata for {artifact_id!r} failed its identity check.",
                "Do not use this artifact; rerun the producing operation.",
            )
        if not self._content_path(digest).is_file():
            raise ToolError(
                "ARTIFACT_NOT_FOUND",
                f"artifact content for {artifact_id!r} is missing.",
                "The artifact store may be incomplete; rerun the producing job.",
            )
        return ref

    def list(self, *, limit: int = 100) -> list[ArtifactRef]:
        refs: list[ArtifactRef] = []
        for path in self.metadata_dir.glob("*.json"):
            try:
                refs.append(self.get(f"sha256:{path.stem}"))
            except ToolError:
                continue
        refs.sort(key=lambda ref: ref.created_at, reverse=True)
        return refs[: max(0, limit)]

    def read_bytes(self, artifact_id: str) -> bytes:
        ref = self.get(artifact_id)
        data = self._content_path(ref.sha256).read_bytes()
        if hashlib.sha256(data).hexdigest() != ref.sha256:
            raise ToolError(
                "ARTIFACT_CORRUPT",
                f"artifact {artifact_id!r} failed its checksum.",
                "Do not use this artifact; rerun the producing job.",
            )
        return data

    def read_text(self, artifact_id: str) -> str:
        return self.read_bytes(artifact_id).decode("utf-8")

    def export(self, artifact_id: str, target: Path, *, overwrite: bool = False) -> Path:
        ref = self.get(artifact_id)
        target = target.expanduser().resolve()
        if target.exists() and not overwrite:
            raise ToolError(
                "FILE_EXISTS",
                f"{target} already exists.",
                "Choose another path or pass overwrite=true.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_name(f".{target.name}.{secrets.token_hex(6)}.tmp")
        try:
            digest, size_bytes = self._copy_new(self._content_path(ref.sha256), temp)
            if digest != ref.sha256 or size_bytes != ref.size_bytes:
                raise ToolError(
                    "ARTIFACT_CORRUPT",
                    f"artifact {artifact_id!r} failed its checksum while exporting.",
                    "Do not use this artifact; rerun the producing operation.",
                )
            if overwrite:
                self._replace(temp, target)
            else:
                self._install_without_overwrite(temp, target)
        finally:
            if temp.exists():
                temp.unlink()
        return target

    def verify(self, artifact_id: str) -> ArtifactRef:
        ref = self.get(artifact_id)
        digest, size_bytes = self._hash_file(self._content_path(ref.sha256))
        if digest != ref.sha256 or size_bytes != ref.size_bytes:
            raise ToolError(
                "ARTIFACT_CORRUPT",
                f"artifact {artifact_id!r} failed its checksum.",
                "Do not use this artifact; rerun the producing operation.",
            )
        return ref

    def content_path(self, artifact_id: str) -> Path:
        """Return a verified local content path for internal application services."""
        return self._content_path(self.verify(artifact_id).sha256)

    def delete(self, artifact_id: str) -> ArtifactRef:
        """Delete one exact content-addressed object from the active store."""
        with self.locked():
            ref = self.verify(artifact_id)
            self._delete_unlocked(ref)
        return ref

    @contextmanager
    def locked(self) -> Iterator[None]:
        with self._lock, exclusive_file_lock(
            self.root / ".store.lock", error_code="ARTIFACT_STORE_BUSY"
        ):
            yield

    def _delete_unlocked(self, ref: ArtifactRef) -> None:
        self._metadata_path(ref.sha256).unlink()
        try:
            self._content_path(ref.sha256).unlink()
        except OSError as exc:
            raise ToolError(
                "ARTIFACT_GC_FAILED",
                f"metadata was removed but content deletion failed for {ref.artifact_id!r}: {exc}",
                "Retry garbage collection to remove the orphaned content.",
            ) from exc

    def _merge_references(
        self, existing: ArtifactRef, references: Iterable[str]
    ) -> ArtifactRef:
        merged = tuple(dict.fromkeys((*existing.references, *references)))
        if merged == existing.references:
            return existing
        updated = existing.model_copy(update={"references": merged})
        payload = json.dumps(
            updated.model_dump(mode="json"), indent=2, ensure_ascii=False
        ).encode("utf-8")
        self._atomic_write(self._metadata_path(existing.sha256), payload)
        return updated

    def _content_path(self, digest: str) -> Path:
        return self.content_dir / digest[:2] / digest

    def _metadata_path(self, digest: str) -> Path:
        return self.metadata_dir / f"{digest}.json"

    @staticmethod
    def _make_ref(
        *,
        digest: str,
        size_bytes: int,
        name: str,
        kind: str,
        media_type: str,
        producer: str,
        revision: RevisionRef | None,
        metadata: dict[str, Any] | None,
        references: Iterable[str],
    ) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=f"sha256:{digest}",
            name=Path(name).name or digest,
            kind=kind,
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=digest,
            created_at=datetime.now(timezone.utc),
            producer=producer,
            revision=revision,
            metadata=metadata or {},
            references=tuple(dict.fromkeys(references)),
        )

    @staticmethod
    def _digest(artifact_id: str) -> str:
        prefix, separator, digest = artifact_id.partition(":")
        valid = prefix == "sha256" and separator == ":" and len(digest) == 64
        if valid:
            try:
                valid = digest == digest.lower() and int(digest, 16) >= 0
            except ValueError:
                valid = False
        if not valid:
            raise ToolError(
                "ARTIFACT_NOT_FOUND",
                f"invalid artifact ID {artifact_id!r}.",
                "Artifact IDs have the form sha256:<64 lowercase hex characters>.",
            )
        return digest

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + 2
            while True:
                try:
                    ArtifactService._replace(temp, path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
        finally:
            if temp.exists():
                temp.unlink()

    @classmethod
    def _copy_new(cls, source: Path, target: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with source.open("rb") as reader, target.open("xb") as writer:
            for chunk in iter(lambda: reader.read(cls._CHUNK_SIZE), b""):
                writer.write(chunk)
                digest.update(chunk)
                size_bytes += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        return digest.hexdigest(), size_bytes

    @classmethod
    def _hash_file(cls, path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size_bytes = 0
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(cls._CHUNK_SIZE), b""):
                digest.update(chunk)
                size_bytes += len(chunk)
        return digest.hexdigest(), size_bytes

    @staticmethod
    def _replace(source: Path, target: Path) -> None:
        deadline = time.monotonic() + 2
        while True:
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)

    @staticmethod
    def _install_without_overwrite(source: Path, target: Path) -> None:
        try:
            os.link(source, target)
        except FileExistsError as exc:
            raise ToolError(
                "FILE_EXISTS",
                f"{target} already exists.",
                "Choose another path or pass overwrite=true.",
            ) from exc
        except OSError as exc:
            raise ToolError(
                "ARTIFACT_EXPORT_FAILED",
                f"could not install the exported artifact at {target}: {exc}",
                "Choose a destination on a local filesystem that supports atomic links.",
            ) from exc
