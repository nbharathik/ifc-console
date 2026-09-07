"""Skill packs: a folder any LLM can produce from a document, installed per user.

A pack zip holds pack.json, SKILL.md (more under skills/), knowledge/*.md,
data/*.jsonl and optionally the source PDF. Install unpacks it under
`~/.ifc-console/agents/packs/<name>/`, registers its skills in the user skill
store and indexes its documents in the user's pack knowledge, so one upload
serves every project. installed.json records what was written, and uninstall
removes exactly that.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from ifc_console.core.results import ToolError
from ifc_console.knowledge.ingest import (
    IMAGE_SUFFIXES,
    MARKDOWN_SUFFIXES,
    PDF_SUFFIXES,
    TEXT_SUFFIXES,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ifc_console_agents.skills import MAX_SKILL_BYTES, AgentSkillStore, _split_front_matter

PACKS_DIRNAME = Path("agents") / "packs"
MAX_PACK_BYTES = 100 * 1024 * 1024
MAX_UNPACKED_BYTES = 200 * 1024 * 1024
MAX_PACK_FILES = 500
TABLE_SUFFIXES = (".jsonl", ".csv")
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_STAGING_TTL_S = 24 * 3600


class SkillPackManifest(BaseModel):
    """pack.json: enough to name, version and attribute a pack."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(default="1", max_length=40)
    title: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)
    task: str = Field(default="", max_length=2000)
    applies_to: str = Field(default="", max_length=300)
    source: dict[str, Any] = Field(default_factory=dict)
    attribution: str = Field(default="", max_length=1000)


def _slug(value: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]
    return base if _NAME.match(base) else ""


def _valid_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ToolError(
            "INVALID_INPUT",
            f"{name!r} is not a valid pack name",
            "Use lowercase letters, digits and dashes.",
        )
    return name


def _members(archive: zipfile.ZipFile) -> list[tuple[zipfile.ZipInfo, PurePosixPath]]:
    """Regular files in the zip with safe relative paths, one top folder stripped."""
    members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        raw = info.filename.replace("\\", "/")
        parts = PurePosixPath(raw).parts
        if not parts or raw.startswith("/") or ".." in parts or ":" in parts[0]:
            raise ToolError(
                "INVALID_INPUT",
                f"unsafe path in zip: {raw!r}",
                "Zip the pack folder itself; no absolute or parent paths.",
            )
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ToolError(
                "INVALID_INPUT", f"symbolic link in zip: {raw!r}", "Zip plain files only."
            )
        if any(part.startswith(".") or part == "__MACOSX" for part in parts):
            continue
        members.append((info, PurePosixPath(*parts)))
    if not members:
        raise ToolError(
            "INVALID_INPUT",
            "the zip holds no files",
            "Zip the pack folder with pack.json and SKILL.md inside.",
        )
    if len(members) > MAX_PACK_FILES:
        raise ToolError(
            "INVALID_INPUT",
            f"the zip holds more than {MAX_PACK_FILES} files",
            "A pack is one document's worth of files.",
        )
    if sum(info.file_size for info, _ in members) > MAX_UNPACKED_BYTES:
        raise ToolError(
            "INVALID_INPUT",
            "the zip unpacks to more than 200 MB",
            "Leave large source documents out of the pack.",
        )
    roots = {path.parts[0] for _, path in members}
    if len(roots) == 1 and all(len(path.parts) > 1 for _, path in members):
        members = [(info, PurePosixPath(*path.parts[1:])) for info, path in members]
    return members


def _count_rows(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = sum(1 for line in handle if line.strip())
    except OSError:
        return 0
    return max(lines - 1, 0) if path.suffix.lower() == ".csv" else lines


def _skill_entry(path: Path, rel: str, size: int) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    header, _body = _split_front_matter(text)
    stem = Path(rel).stem
    name = _slug(header.get("name") or ("" if stem.upper() == "SKILL" else stem))
    return {
        "file": rel,
        "name": name,
        "description": header.get("description") or "",
        "applies_to": header.get("applies_to") or "",
        "kind": header.get("kind") or "prose",
        "size_bytes": size,
        "text": text,
    }


def _manifest_from(folder: Path, fallback_name: str, warnings: list[str]) -> tuple[Any, bool]:
    manifest_path = folder / "pack.json"
    synthesized = not manifest_path.is_file()
    raw: dict[str, Any] = {}
    if synthesized:
        warnings.append("pack.json is missing; a minimal manifest was synthesized")
    else:
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError(
                "INVALID_INPUT",
                f"pack.json is not valid JSON: {exc}",
                "Fix pack.json, or remove it to have one synthesized.",
            ) from None
        if not isinstance(loaded, dict):
            raise ToolError(
                "INVALID_INPUT", "pack.json must be a JSON object", "See the pack generator."
            )
        raw = dict(loaded)
    if not isinstance(raw.get("name"), str) or not raw.get("name"):
        raw["name"] = _slug(str(raw.get("name") or fallback_name))
        if not synthesized:
            warnings.append("pack.json has no usable name; derived from the file name")
    for key in ("version", "title", "description", "task", "applies_to", "attribution"):
        if key in raw and raw[key] is None:
            raw.pop(key)
        elif key in raw and not isinstance(raw[key], str):
            raw[key] = str(raw[key])
    if "source" in raw and not isinstance(raw["source"], dict):
        raw["source"] = {"title": str(raw["source"])}
    try:
        manifest = SkillPackManifest.model_validate(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first.get("loc", ())) or "pack.json"
        raise ToolError(
            "INVALID_INPUT",
            f"pack.json {where}: {first.get('msg')}",
            "Match the pack.json shape from the skill pack generator.",
        ) from None
    return manifest, synthesized


def inspect_pack(folder: Path, *, fallback_name: str = "") -> dict[str, Any]:
    """Validate an unpacked folder and describe what installing it would do."""
    warnings: list[str] = []
    manifest, synthesized = _manifest_from(folder, fallback_name, warnings)
    skills: list[dict[str, Any]] = []
    knowledge: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    total = 0
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = path.relative_to(folder).as_posix()
        if rel in {"pack.json", "installed.json"}:
            continue
        suffix = path.suffix.lower()
        size = path.stat().st_size
        total += size
        top = rel.split("/", 1)[0]
        if suffix in MARKDOWN_SUFFIXES and (Path(rel).stem.upper() == "SKILL" or top == "skills"):
            if size > MAX_SKILL_BYTES:
                raise ToolError(
                    "INVALID_INPUT",
                    f"{rel} is larger than {MAX_SKILL_BYTES // 1024} KB",
                    "Split it: one skill per file.",
                )
            skills.append(_skill_entry(path, rel, size))
        elif suffix in MARKDOWN_SUFFIXES + TEXT_SUFFIXES:
            knowledge.append({"path": rel, "size_bytes": size})
        elif suffix in PDF_SUFFIXES + IMAGE_SUFFIXES:
            media = "image" if suffix in IMAGE_SUFFIXES else "pdf"
            documents.append({"path": rel, "size_bytes": size, "media": media})
        elif suffix in TABLE_SUFFIXES:
            tables.append({"path": rel, "size_bytes": size, "rows": _count_rows(path)})
        else:
            warnings.append(f"{rel} is not a pack file and is ignored")
    for skill in skills:
        if not skill["name"]:
            skill["name"] = manifest.name
    names = [skill["name"] for skill in skills]
    if len(names) != len(set(names)):
        raise ToolError(
            "INVALID_INPUT",
            "two skills in the pack share a name",
            "Give each skill file its own front-matter name.",
        )
    if not (skills or knowledge or documents or tables):
        raise ToolError(
            "INVALID_INPUT",
            "the pack holds nothing to install",
            "Add SKILL.md, knowledge/*.md, data/*.jsonl or a source PDF.",
        )
    if not skills:
        warnings.append("no SKILL.md: the pack adds knowledge only, nothing appears under #")
    if tables:
        warnings.append(
            f"{len(tables)} table(s) will be indexed row by row for search and lookup_table_rows"
        )
    return {
        "name": manifest.name,
        "version": manifest.version,
        "title": manifest.title or manifest.name,
        "description": manifest.description,
        "task": manifest.task,
        "applies_to": manifest.applies_to,
        "source": manifest.source,
        "attribution": manifest.attribution,
        "manifest": manifest.model_dump(mode="json"),
        "synthesized_manifest": synthesized,
        "skills": skills,
        "knowledge": knowledge,
        "documents": documents,
        "tables": tables,
        "total_bytes": total,
        "warnings": warnings,
    }


class SkillPackStore:
    """Installed packs under the user's IFC Console home."""

    def __init__(self, home: str | Path) -> None:
        self.home = Path(home).expanduser()
        self.directory = self.home / PACKS_DIRNAME
        self.staging = self.directory / ".staging"

    # -- stage -------------------------------------------------------------
    def _purge_staging(self) -> None:
        if not self.staging.is_dir():
            return
        cutoff = time.time() - _STAGING_TTL_S
        for child in self.staging.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue

    def stage(self, data: bytes, filename: str = "pack.zip") -> dict[str, Any]:
        """Unpack a zip into a staging folder and return its preview."""
        if not data:
            raise ToolError("INVALID_INPUT", "the pack file is empty", "Choose a .zip file.")
        if len(data) > MAX_PACK_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                f"{filename} is larger than {MAX_PACK_BYTES // (1024 * 1024)} MB",
                "Leave the source document out of the pack.",
            )
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise ToolError(
                "INVALID_INPUT",
                f"{filename} is not a zip file",
                "Zip the pack folder and upload the .zip.",
            ) from None
        self._purge_staging()
        staging_id = uuid.uuid4().hex
        target = self.staging / staging_id
        skipped: list[str] = []
        try:
            with archive:
                members = _members(archive)
                target.mkdir(parents=True, exist_ok=True)
                for info, rel in members:
                    if len(rel.parts) > 4:
                        skipped.append(str(rel))
                        continue
                    dest = target.joinpath(*rel.parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as source, dest.open("wb") as out:
                        shutil.copyfileobj(source, out)
            preview = inspect_pack(target, fallback_name=Path(filename).stem)
            # A synthesized manifest is written now, so commit sees the same
            # name the preview showed rather than re-deriving one.
            if preview["synthesized_manifest"]:
                (target / "pack.json").write_text(
                    json.dumps(preview["manifest"], indent=2) + "\n", encoding="utf-8"
                )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        preview["staging_id"] = staging_id
        if skipped:
            preview["warnings"].append(
                f"skipped {len(skipped)} deeply nested file(s): {', '.join(skipped[:5])}"
            )
        installed = self._installed(self.directory / preview["name"])
        if installed is not None:
            preview["replaces"] = {
                "version": installed.get("version"),
                "installed_at": installed.get("installed_at"),
            }
        return preview

    def _staged(self, staging_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", staging_id or ""):
            raise ToolError("INVALID_INPUT", "invalid staging id", "Upload the pack again.")
        target = self.staging / staging_id
        if not target.is_dir():
            raise ToolError(
                "NOT_FOUND", "the staged pack is gone", "Upload the pack again and install it."
            )
        return target

    def discard(self, staging_id: str) -> bool:
        try:
            target = self._staged(staging_id)
        except ToolError:
            return False
        shutil.rmtree(target, ignore_errors=True)
        return True

    # -- install -----------------------------------------------------------
    @staticmethod
    def _installed(folder: Path) -> dict[str, Any] | None:
        record = folder / "installed.json"
        if not record.is_file():
            return None
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def installed(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.directory.is_dir():
            return rows
        for child in sorted(self.directory.iterdir(), key=lambda item: item.name):
            if child.name.startswith(".") or not child.is_dir():
                continue
            record = self._installed(child)
            if record is not None:
                rows.append(record)
        return rows

    def get(self, name: str) -> dict[str, Any] | None:
        return self._installed(self.directory / _valid_name(name))

    def _index_paths(self, name: str, entries: list[dict[str, Any]]) -> list[str]:
        # paths are relative to the console home, the base of the library index
        return [f"agents/packs/{name}/{entry['path']}" for entry in entries]

    def commit(
        self,
        staging_id: str,
        *,
        skills: AgentSkillStore,
        knowledge: Any,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Move a staged pack into place, register its skills, index its documents."""
        staged = self._staged(staging_id)
        preview = inspect_pack(staged, fallback_name=staging_id)
        name = preview["name"]
        target = self.directory / name
        previous = self._installed(target)
        if target.exists():
            if previous is not None:
                self._forget_skills(previous, skills)
            shutil.rmtree(target)
        shutil.move(str(staged), str(target))
        installed_skills: list[str] = []
        for skill in preview["skills"]:
            row = skills.import_file(
                skill["file"],
                (target / skill["file"]).read_bytes(),
                scope="user",
                pack=name,
            )
            installed_skills.append(row["name"])
        documents = preview["knowledge"] + preview["documents"] + preview["tables"]
        records = 0
        if documents:
            report = knowledge.ingest([target / entry["path"] for entry in documents])
            records = int(report.get("records") or 0)
        record = {
            "name": name,
            "version": preview["version"],
            "title": preview["title"],
            "description": preview["description"],
            "task": preview["task"],
            "applies_to": preview["applies_to"],
            "source": preview["source"],
            "attribution": preview["attribution"],
            "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent": agent,
            "skills": installed_skills,
            "documents": self._index_paths(name, documents),
            "tables": [{"path": t["path"], "rows": t["rows"]} for t in preview["tables"]],
            "records": records,
            "directory": str(target),
            "replaced": previous.get("version") if previous else None,
        }
        (target / "installed.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return record

    @staticmethod
    def _forget_skills(record: dict[str, Any], skills: AgentSkillStore) -> list[str]:
        removed = []
        for skill_name in record.get("skills") or []:
            if isinstance(skill_name, str) and skills.delete(
                skill_name, scope="user", pack=str(record.get("name") or "")
            ):
                removed.append(skill_name)
        return removed

    def uninstall(self, name: str, *, skills: AgentSkillStore, knowledge: Any) -> dict[str, Any]:
        """Remove the pack folder, its skills, and its documents from the index."""
        target = self.directory / _valid_name(name)
        record = self._installed(target)
        if record is None and not target.is_dir():
            raise ToolError(
                "NOT_FOUND", f"no installed pack named {name!r}", "The panel lists installed packs."
            )
        removed_skills = self._forget_skills(record or {"name": name}, skills)
        shutil.rmtree(target, ignore_errors=True)
        prefix = f"agents/packs/{name}/"
        if knowledge.sources():
            base = self.home
            remaining = [
                base / str(source.get("path") or "")
                for source in knowledge.sources()
                if not str(source.get("path") or "").replace("\\", "/").startswith(prefix)
            ]
            knowledge.ingest([path for path in remaining if path.is_file()], replace=True)
        return {
            "name": name,
            "skills": removed_skills,
            "documents": list((record or {}).get("documents") or []),
        }


__all__ = [
    "MAX_PACK_BYTES",
    "PACKS_DIRNAME",
    "SkillPackManifest",
    "SkillPackStore",
    "inspect_pack",
]
