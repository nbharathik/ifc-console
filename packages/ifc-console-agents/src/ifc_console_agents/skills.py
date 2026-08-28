"""Agent skills: reusable measurement procedures saved as markdown.

A skill is one worked method the agent (or the user) recorded after solving a
task, e.g. how to measure a sheet pile profile. Skills live as plain markdown
files with a small front-matter header in the project workspace, so they are
reviewable, versionable, and editable by hand. Agents list them, load the one
that matches the task, and follow it instead of rediscovering the method.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ifc_console.core.results import ToolError

SKILLS_DIRNAME = Path(".ifc-console") / "agents" / "skills"
MAX_SKILL_BYTES = 64 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_HEADER_KEYS = ("name", "description", "applies_to")


def skills_dir(project_dir: Path) -> Path:
    return project_dir / SKILLS_DIRNAME


def _valid_name(name: str) -> str:
    if not _NAME.match(name or ""):
        raise ToolError(
            "INVALID_INPUT",
            f"skill name {name!r} is not a lowercase-slug",
            "Use lowercase letters, digits and dashes, e.g. 'sheet-pile-profile'.",
        )
    return name


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    header: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return header, "\n".join(lines[index + 1 :]).lstrip("\n")
        key, sep, value = line.partition(":")
        if sep and key.strip() in _HEADER_KEYS:
            header[key.strip()] = value.strip()
    return {}, text


def _first_line(body: str) -> str:
    for line in body.splitlines():
        text = line.strip().lstrip("#").strip()
        if text:
            return text[:160]
    return ""


class AgentSkillStore:
    """Markdown skill files under `.ifc-console/agents/skills/`."""

    def __init__(self, project_dir: Path) -> None:
        self.directory = skills_dir(project_dir)

    def path_for(self, name: str) -> Path:
        return self.directory / f"{_valid_name(name)}.md"

    def _parse(self, path: Path) -> dict[str, Any]:
        raw = path.read_text(encoding="utf-8", errors="replace")
        header, body = _split_front_matter(raw)
        stat = path.stat()
        return {
            "name": header.get("name") or path.stem,
            "description": header.get("description") or _first_line(body),
            "applies_to": header.get("applies_to") or None,
            "path": str(SKILLS_DIRNAME / path.name),
            "size_bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            ),
            "content": body,
        }

    def entries(self) -> list[dict[str, Any]]:
        """All skills, newest first, without their bodies."""
        if not self.directory.is_dir():
            return []
        rows = []
        for path in sorted(self.directory.glob("*.md")):
            if path.stat().st_size > MAX_SKILL_BYTES:
                continue
            row = self._parse(path)
            row.pop("content")
            rows.append(row)
        rows.sort(key=lambda row: row["updated_at"], reverse=True)
        return rows

    def read(self, name: str) -> dict[str, Any]:
        path = self.path_for(name)
        if not path.is_file():
            known = ", ".join(row["name"] for row in self.entries()[:10]) or "none saved yet"
            raise ToolError(
                "NOT_FOUND",
                f"no skill named {name!r}",
                f"list_agent_skills shows what exists (currently: {known}).",
            )
        return self._parse(path)

    def import_file(self, filename: str, data: bytes) -> dict[str, Any]:
        """One markdown file becomes one skill, written elsewhere and dropped in.

        Front matter wins over the filename for the name and description; a
        taken name gets a numeric suffix rather than clobbering the original.
        """
        if len(data) > MAX_SKILL_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                f"{filename} is larger than {MAX_SKILL_BYTES // 1024} KB",
                "Split it: one skill per file, one procedure per skill.",
            )
        text = data.decode("utf-8", errors="replace")
        header, body = _split_front_matter(text)
        base = re.sub(r"[^a-z0-9]+", "-", (header.get("name") or Path(filename).stem).lower())
        base = base.strip("-")[:64]
        if not _NAME.match(base):
            raise ToolError(
                "INVALID_INPUT",
                f"cannot derive a skill name from {filename!r}",
                "Name the file or the front-matter `name:` with letters and dashes.",
            )
        name = base
        counter = 2
        while self.path_for(name).exists():
            suffix = f"-{counter}"
            name = base[: 64 - len(suffix)] + suffix
            counter += 1
        description = header.get("description") or _first_line(body) or "Imported skill"
        return self.save(
            name,
            body,
            description=description,
            applies_to=header.get("applies_to") or None,
        )

    def save(
        self,
        name: str,
        content: str,
        *,
        description: str,
        applies_to: str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        path = self.path_for(name)
        if path.exists() and not overwrite:
            raise ToolError(
                "FILE_EXISTS",
                f"skill {name!r} already exists",
                "Pass overwrite=true to update it, or pick another name.",
            )
        _, body = _split_front_matter(content)
        header = [
            "---",
            f"name: {name}",
            f"description: {' '.join(description.split())}",
        ]
        if applies_to:
            header.append(f"applies_to: {' '.join(applies_to.split())}")
        header.append("---")
        text = "\n".join(header) + "\n\n" + body.strip() + "\n"
        if len(text.encode("utf-8")) > MAX_SKILL_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                f"skill is larger than {MAX_SKILL_BYTES // 1024} KB",
                "Keep skills short: the goal, the tool calls in order, the checks.",
            )
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        row = self._parse(path)
        row.pop("content")
        return row


__all__ = ["AgentSkillStore", "MAX_SKILL_BYTES", "SKILLS_DIRNAME", "skills_dir"]
