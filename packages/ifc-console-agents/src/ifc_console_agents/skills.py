"""Agent skills: reusable measurement procedures saved as markdown.

A skill is one worked method the agent (or the user) recorded after solving a
task, e.g. how to measure a sheet pile profile. Skills live as plain markdown
files with a small front-matter header in the project workspace, so they are
reviewable, versionable, and editable by hand. Agents list them, load the one
that matches the task, and follow it instead of rediscovering the method.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ifc_console.core.results import ToolError
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SKILLS_DIRNAME = Path(".ifc-console") / "agents" / "skills"
MAX_SKILL_BYTES = 64 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_HEADER_KEYS = ("name", "description", "applies_to", "kind", "schema_version")
_MEASUREMENT_ID = r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+$"
MEASUREMENT_SPEC_FENCE = "measurement-spec"
_EXPLICIT_MEASUREMENT_ID = re.compile(
    r"\b(?:profile|envelope|section|mass|material|topology|opening)"
    r"\.[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\b"
)
_IFC_CLASS = re.compile(r"\bIfc[A-Z][A-Za-z0-9_]*\b")
_MIGRATION_HINTS = (
    ("web thickness", "profile.web_thickness"),
    ("flange thickness", "profile.flange_thickness"),
    ("wall thickness", "profile.wall_thickness"),
    ("overall length", "envelope.overall_length"),
    ("overall width", "envelope.overall_width"),
    ("overall height", "envelope.overall_height"),
    ("cross section area", "section.area"),
    ("section area", "section.area"),
    ("section perimeter", "section.perimeter"),
    ("surface area", "mass.surface_area"),
    ("volume", "mass.volume"),
    ("opening count", "opening.count"),
)


class _SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MeasurementTolerance(_SpecModel):
    """Tolerance used for exemplar matching and replay comparisons."""

    absolute_si: float | None = Field(default=None, ge=0.0)
    relative: float | None = Field(default=0.02, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def has_a_bound(self) -> MeasurementTolerance:
        if self.absolute_si is None and self.relative is None:
            raise ValueError("at least one tolerance bound is required")
        return self


class MeasurementObjectRole(_SpecModel):
    """One endpoint object retained for a recorded relationship intent."""

    role: Literal["from", "to"]
    global_id: str = Field(min_length=1, max_length=64)
    anchor_index: int = Field(ge=0, le=15)
    local_point: tuple[float, float, float] | None = None
    reach_si: float | None = Field(default=None, ge=0.0)


class MeasurementIntent(_SpecModel):
    """Reviewable viewer intent retained beside an executable rule."""

    viewer_kind: Literal["distance", "dimensions", "area", "path", "angle", "laser", "unknown"]
    viewer_index: int = Field(ge=0, le=10_000)
    label: str | None = Field(default=None, max_length=200)
    value_si: float | None = None
    semantic_direction: str | None = Field(default=None, max_length=80)
    local_direction: tuple[float, float, float] | None = None
    world_axis: Literal["x", "y", "z"] | None = None
    snap_kinds: tuple[str, ...] = Field(default=(), max_length=12)
    object_roles: tuple[MeasurementObjectRole, ...] = Field(default=(), max_length=2)
    anchor_relationship: Literal[
        "same_object", "between_objects", "unanchored", "element_record"
    ] = "unanchored"
    matched_by: tuple[str, ...] = Field(default=(), max_length=12)
    candidate_outputs: tuple[str, ...] = Field(default=(), max_length=12)
    match_delta_si: float | None = Field(default=None, ge=0.0)
    match_tolerance_si: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def relationship_roles_are_unique(self) -> MeasurementIntent:
        roles = [item.role for item in self.object_roles]
        if len(roles) != len(set(roles)):
            raise ValueError("object relationship roles must be unique")
        return self


class MeasurementRule(_SpecModel):
    """One deterministic output requested from geometry analysis v2."""

    output: str | None = Field(default=None, pattern=_MEASUREMENT_ID)
    rule_type: Literal[
        "object_measurement",
        "relationship",
        "area",
        "path",
        "angle",
        "clearance",
        "element_size",
    ] = "object_measurement"
    preferred_sources: tuple[str, ...] = Field(default=(), max_length=12)
    fallbacks: tuple[str, ...] = Field(default=(), max_length=12)
    frame: Literal["semantic", "placement", "principal", "local", "world"] = "semantic"
    direction: str | None = Field(default=None, max_length=80)
    minimum_confidence: Literal["low", "medium", "high", "exact"] = "medium"
    tolerance: MeasurementTolerance = Field(default_factory=MeasurementTolerance)
    unresolved: bool = False
    intent: MeasurementIntent = Field(
        default_factory=lambda: MeasurementIntent(viewer_kind="unknown", viewer_index=0)
    )

    @model_validator(mode="after")
    def resolved_rules_have_an_output(self) -> MeasurementRule:
        if not self.unresolved and self.output is None:
            raise ValueError("a resolved measurement rule requires output")
        return self


class MeasurementApplicability(_SpecModel):
    ifc_classes: tuple[str, ...] = Field(default=(), max_length=32)
    profile_families: tuple[str, ...] = Field(default=(), max_length=32)
    geometry_families: tuple[str, ...] = Field(default=(), max_length=32)
    hard_requirements: tuple[str, ...] = Field(default=(), max_length=32)
    similarity_threshold: float = Field(default=0.85, ge=0.0, le=1.0)


class MeasurementModelRevision(_SpecModel):
    model_id: str | None = Field(default=None, max_length=500)
    fingerprint: str | None = Field(default=None, max_length=500)
    revision: int | None = Field(default=None, ge=0)


class MeasurementExemplarObject(_SpecModel):
    global_id: str = Field(min_length=1, max_length=64)
    ifc_class: str | None = Field(default=None, max_length=100)
    type_name: str | None = Field(default=None, max_length=300)
    geometry_family: str | None = Field(default=None, max_length=100)
    geometry_signature: dict[str, Any] = Field(default_factory=dict)


class MeasurementExemplar(_SpecModel):
    model_name: str | None = Field(default=None, max_length=500)
    recorded_at: str | None = Field(default=None, max_length=100)
    model_revision: MeasurementModelRevision = Field(default_factory=MeasurementModelRevision)
    objects: tuple[MeasurementExemplarObject, ...] = Field(default=(), max_length=40)


class MeasurementVerification(_SpecModel):
    cross_check: Literal["second_source_when_available", "none"] = "second_source_when_available"
    on_conflict: Literal["report", "report_and_refuse_property_proposal"] = (
        "report_and_refuse_property_proposal"
    )


class MeasurementSkillSpec(_SpecModel):
    """Validated executable payload embedded in one Markdown skill."""

    schema_version: Literal[2] = 2
    kind: Literal["parametric_measurement"] = "parametric_measurement"
    applicability: MeasurementApplicability = Field(default_factory=MeasurementApplicability)
    measurements: tuple[MeasurementRule, ...] = Field(min_length=1, max_length=80)
    exemplar: MeasurementExemplar | None = None
    verification: MeasurementVerification = Field(default_factory=MeasurementVerification)
    outputs: tuple[str, ...] = Field(default=(), max_length=80)

    @model_validator(mode="after")
    def outputs_are_unique_and_known(self) -> MeasurementSkillSpec:
        resolved = [
            rule.output for rule in self.measurements if rule.output and not rule.unresolved
        ]
        if len(resolved) != len(set(resolved)):
            raise ValueError("resolved measurement outputs must be unique")
        if self.outputs and set(self.outputs) != set(resolved):
            raise ValueError("outputs must list exactly the resolved measurement outputs")
        return self

    @property
    def executable(self) -> bool:
        return all(not rule.unresolved for rule in self.measurements)

    @property
    def measurement_ids(self) -> tuple[str, ...]:
        return tuple(
            rule.output
            for rule in self.measurements
            if rule.output is not None and not rule.unresolved
        )


def _invalid_spec(message: str, hint: str) -> ToolError:
    return ToolError("INVALID_INPUT", message, hint)


def _measurement_spec_blocks(text: str) -> list[str]:
    """Return strict ``measurement-spec`` fences, rejecting unclosed blocks."""
    lines = text.splitlines()
    blocks: list[str] = []
    index = 0
    while index < len(lines):
        opening = lines[index].strip()
        if opening.startswith(f"```{MEASUREMENT_SPEC_FENCE}") and opening != (
            f"```{MEASUREMENT_SPEC_FENCE}"
        ):
            raise _invalid_spec(
                f"measurement spec fence on line {index + 1} has an invalid info string",
                f"Use exactly ```{MEASUREMENT_SPEC_FENCE} on its own line.",
            )
        if opening != f"```{MEASUREMENT_SPEC_FENCE}":
            index += 1
            continue
        start = index
        index += 1
        body: list[str] = []
        while index < len(lines) and lines[index].strip() != "```":
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            raise _invalid_spec(
                f"measurement spec fence starting on line {start + 1} is not closed",
                f"Close it with ``` and keep exactly one ```{MEASUREMENT_SPEC_FENCE} block.",
            )
        blocks.append("\n".join(body))
        index += 1
    return blocks


def parse_measurement_spec(text: str, *, required: bool = False) -> MeasurementSkillSpec | None:
    """Parse the sole executable block without interpreting surrounding prose."""
    blocks = _measurement_spec_blocks(text)
    if not blocks:
        if required:
            raise ToolError(
                "INVALID_INPUT",
                "the skill is prose-only and has no executable measurement spec",
                "Use it as agent guidance, or record/review a version 2 measurement skill.",
            )
        return None
    if len(blocks) != 1:
        raise _invalid_spec(
            f"the skill contains {len(blocks)} measurement spec blocks",
            f"Keep exactly one ```{MEASUREMENT_SPEC_FENCE} JSON block.",
        )
    try:
        payload = json.loads(blocks[0])
    except (json.JSONDecodeError, TypeError) as exc:
        reason = exc.msg if isinstance(exc, json.JSONDecodeError) else "invalid value"
        raise _invalid_spec(
            f"measurement spec is not valid JSON ({reason})",
            "Review and save the skill again; executable specs use strict JSON, not YAML.",
        ) from None
    if not isinstance(payload, dict):
        raise _invalid_spec(
            "measurement spec must be a JSON object",
            "Put schema_version, kind, applicability and measurements in one object.",
        )
    try:
        return MeasurementSkillSpec.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc") or ()) or "spec"
        raise _invalid_spec(
            f"measurement spec field {location} is invalid: {first.get('msg', 'invalid value')}",
            "Review the version 2 measurement-spec schema and save the skill again.",
        ) from None


def measurement_spec_block(spec: MeasurementSkillSpec) -> str:
    """Canonical fenced JSON for a reviewable Markdown skill."""
    payload = spec.model_dump(mode="json", exclude_none=True)
    return (
        f"```{MEASUREMENT_SPEC_FENCE}\n"
        + json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        + "\n```"
    )


def _migration_candidate_ids(text: str) -> list[tuple[str, str]]:
    """Conservative output hints from prose, never executable conclusions."""
    found: dict[str, str] = {}
    for match in _EXPLICIT_MEASUREMENT_ID.finditer(text):
        found.setdefault(match.group(0), "explicit_measurement_id")
    folded = " ".join(text.casefold().split())
    for phrase, output in _MIGRATION_HINTS:
        if phrase in folded:
            found.setdefault(output, "prose_keyword")
    return list(found.items())[:40]


def _migration_ifc_classes(text: str) -> tuple[str, ...]:
    values = (
        value
        for value in _IFC_CLASS.findall(text)
        if not value.startswith(("IfcConsole", "IfcOpenShell"))
    )
    return tuple(dict.fromkeys(values))[:32]


def _migration_spec(*, content: str, applies_to: str | None) -> tuple[MeasurementSkillSpec, dict]:
    """Build a review-required v2 suggestion without treating prose as authority."""
    candidates = _migration_candidate_ids(content)
    rules: list[MeasurementRule] = []
    for index, (output, matched_by) in enumerate(candidates):
        rule_type: Literal["object_measurement", "area", "element_size"]
        if output == "section.area":
            rule_type = "area"
        elif output.startswith("envelope."):
            rule_type = "element_size"
        else:
            rule_type = "object_measurement"
        rules.append(
            MeasurementRule(
                output=None,
                rule_type=rule_type,
                unresolved=True,
                frame="semantic",
                minimum_confidence="medium",
                intent=MeasurementIntent(
                    viewer_kind="area" if rule_type == "area" else "unknown",
                    viewer_index=index,
                    label=f"Migration candidate: {output}",
                    matched_by=(matched_by,),
                    candidate_outputs=(output,),
                ),
            )
        )
    if not rules:
        rules.append(
            MeasurementRule(
                output=None,
                unresolved=True,
                intent=MeasurementIntent(
                    viewer_kind="unknown",
                    viewer_index=0,
                    label="Review the prose and choose stable measurement ids",
                    matched_by=("prose_migration_preview",),
                ),
            )
        )
    classes = _migration_ifc_classes("\n".join(filter(None, (applies_to, content))))
    spec = MeasurementSkillSpec(
        applicability=MeasurementApplicability(ifc_classes=classes),
        measurements=tuple(rules),
        outputs=(),
    )
    return spec, {
        "ifc_classes": list(classes),
        "candidate_outputs": [output for output, _ in candidates],
        "candidate_sources": [source for _, source in candidates],
    }


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
        spec: MeasurementSkillSpec | None = None
        spec_status = "none"
        spec_error: str | None = None
        try:
            spec = parse_measurement_spec(body)
            if spec is not None:
                header_kind = header.get("kind")
                header_version = header.get("schema_version")
                if header_kind and header_kind != spec.kind:
                    raise _invalid_spec(
                        "front matter kind does not match the measurement spec",
                        "Use kind: parametric_measurement for a version 2 measurement skill.",
                    )
                if header_version and header_version != str(spec.schema_version):
                    raise _invalid_spec(
                        "front matter schema_version does not match the measurement spec",
                        "Use schema_version: 2 for this measurement spec.",
                    )
                spec_status = "valid" if spec.executable else "review_required"
        except ToolError as exc:
            spec_status = "invalid"
            spec_error = str(exc)
        declared_version: int | str | None = header.get("schema_version") or None
        if isinstance(declared_version, str) and declared_version.isdigit():
            declared_version = int(declared_version)
        structured = bool(
            spec is not None
            or header.get("kind") == "parametric_measurement"
            or f"```{MEASUREMENT_SPEC_FENCE}" in body
        )
        row: dict[str, Any] = {
            "name": header.get("name") or path.stem,
            "description": header.get("description") or _first_line(body),
            "applies_to": header.get("applies_to") or None,
            "kind": header.get("kind") or (spec.kind if spec else "prose"),
            "schema_version": spec.schema_version if spec else declared_version,
            "structured": structured,
            "executable": bool(spec and spec.executable),
            "spec_status": spec_status,
            "path": str(SKILLS_DIRNAME / path.name),
            "size_bytes": stat.st_size,
            "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            ),
            "content": body,
        }
        if spec is not None:
            row["measurement_spec"] = spec.model_dump(mode="json", exclude_none=True)
        if spec_error:
            row["spec_error"] = spec_error
        return row

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
            row.pop("measurement_spec", None)
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

    def measurement_spec(self, name: str) -> MeasurementSkillSpec:
        """The validated v2 spec for deterministic replay, or a safe refusal."""
        row = self.read(name)
        status = row.get("spec_status")
        if status == "none":
            raise ToolError(
                "INVALID_INPUT",
                f"skill {name!r} is prose-only and cannot be applied deterministically",
                "Load it as guidance, or record and review a version 2 measurement skill.",
            )
        if status == "invalid":
            raise _invalid_spec(
                f"skill {name!r} has an invalid measurement spec",
                "Open the skill, repair its single version 2 JSON block, and retry.",
            )
        payload = row.get("measurement_spec")
        try:
            return MeasurementSkillSpec.model_validate(payload)
        except ValidationError:
            raise _invalid_spec(
                f"skill {name!r} has an invalid measurement spec",
                "Open the skill, repair its single version 2 JSON block, and retry.",
            ) from None

    def migration_preview(self, name: str) -> dict[str, Any]:
        """Suggest a review-required v2 body without changing the source skill."""
        row = self.read(name)
        status = row.get("spec_status")
        if status == "invalid":
            raise _invalid_spec(
                f"skill {name!r} already contains an invalid measurement spec",
                "Repair the existing block instead of adding a second migration block.",
            )
        if status != "none":
            raise ToolError(
                "INVALID_INPUT",
                f"skill {name!r} already contains a version 2 measurement spec",
                "Review or apply the existing structured skill; migration is for prose-only skills.",
            )
        content = str(row.get("content") or "")
        spec, inferred = _migration_spec(
            content=content,
            applies_to=str(row.get("applies_to") or "") or None,
        )
        block = measurement_spec_block(spec)
        suggested_content = (
            content.rstrip()
            + "\n\n## Suggested executable measurement spec\n\n"
            + "Review every unresolved row before saving.\n\n"
            + block
            + "\n"
        )
        suggested_bytes = len(suggested_content.encode("utf-8"))
        review_items = [
            "Confirm every candidate output against geometry analysis v2.",
            "Resolve or remove every unresolved measurement before deterministic replay.",
            "Preview applicability and replay on the exemplar before broad application.",
            "Use save_agent_skill with explicit overwrite approval only after review.",
        ]
        if suggested_bytes > MAX_SKILL_BYTES:
            review_items.append("Shorten the prose so the reviewed skill fits the size limit.")
        return {
            "source": {
                "name": row["name"],
                "path": row["path"],
                "kind": row["kind"],
                "spec_status": status,
                "updated_at": row["updated_at"],
                "size_bytes": row["size_bytes"],
            },
            "suggestion": {
                "kind": spec.kind,
                "schema_version": spec.schema_version,
                "executable": False,
                "review_required": True,
                "measurement_spec": spec.model_dump(mode="json", exclude_none=True),
                "measurement_spec_block": block,
                "content": suggested_content,
                "size_bytes": suggested_bytes,
                "fits_size_limit": suggested_bytes <= MAX_SKILL_BYTES,
                "inferred": inferred,
            },
            "review_items": review_items,
            "source_unchanged": True,
            "read_only": True,
            "side_effects": {"file_writes": 0, "property_writes": 0, "proposals": 0},
        }

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
            kind=header.get("kind") or None,
            schema_version=header.get("schema_version") or None,
        )

    def save(
        self,
        name: str,
        content: str,
        *,
        description: str,
        applies_to: str | None = None,
        kind: str | None = None,
        schema_version: int | str | None = None,
        overwrite: bool = False,
    ) -> dict[str, Any]:
        path = self.path_for(name)
        if path.exists() and not overwrite:
            raise ToolError(
                "FILE_EXISTS",
                f"skill {name!r} already exists",
                "Pass overwrite=true to update it, or pick another name.",
            )
        content_header, body = _split_front_matter(content)
        declared_kind = kind or content_header.get("kind") or None
        declared_version = schema_version or content_header.get("schema_version") or None
        spec = parse_measurement_spec(body)
        if spec is not None:
            if declared_kind is not None and str(declared_kind) != spec.kind:
                raise _invalid_spec(
                    "declared skill kind does not match its measurement spec",
                    "Use kind parametric_measurement, or remove the executable block.",
                )
            if declared_version is not None and str(declared_version) != str(spec.schema_version):
                raise _invalid_spec(
                    "declared schema_version does not match its measurement spec",
                    "Use schema_version 2 for a version 2 measurement skill.",
                )
            declared_kind = spec.kind
            declared_version = spec.schema_version
        elif declared_kind == "parametric_measurement" or declared_version is not None:
            raise _invalid_spec(
                "structured skill front matter has no measurement spec",
                f"Add exactly one ```{MEASUREMENT_SPEC_FENCE} JSON block, or save it as prose.",
            )
        header = [
            "---",
            f"name: {name}",
            f"description: {' '.join(description.split())}",
        ]
        if applies_to:
            header.append(f"applies_to: {' '.join(applies_to.split())}")
        if declared_kind:
            header.append(f"kind: {declared_kind}")
        if declared_version is not None:
            header.append(f"schema_version: {declared_version}")
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


__all__ = [
    "AgentSkillStore",
    "MAX_SKILL_BYTES",
    "MEASUREMENT_SPEC_FENCE",
    "MeasurementApplicability",
    "MeasurementExemplar",
    "MeasurementExemplarObject",
    "MeasurementIntent",
    "MeasurementModelRevision",
    "MeasurementObjectRole",
    "MeasurementRule",
    "MeasurementSkillSpec",
    "MeasurementTolerance",
    "MeasurementVerification",
    "SKILLS_DIRNAME",
    "measurement_spec_block",
    "parse_measurement_spec",
    "skills_dir",
]
