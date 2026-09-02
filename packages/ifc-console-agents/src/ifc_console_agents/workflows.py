"""Preconfigured workflows: named runs a user starts with one click.

A workflow is a small YAML file naming ordered steps. A step either calls one
console tool, runs one agent over a scoped set of capability blocks, stops for
a human decision, or writes an artifact. Nothing here is a second agent
implementation: agent steps go through the same :func:`~ifc_console_agents.blocks.compose`
call the panel uses, so a workflow inherits the same tool surface, approval
gates, and AI provenance the chat panel has.

This is deliberately not the batch engine in ``ifc_console.core.workflows``.
That one fans one validation or query operation out over many files and hashes
every source into an immutable plan. A workflow here is a short staged pipeline
over the open model, where a stage's output is the next stage's prompt.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import yaml
from ifc_console.core.results import ToolError
from pydantic import BaseModel, ConfigDict, Field, model_validator

WORKFLOWS_DIRNAME = Path(".ifc-console") / "agents" / "workflows"
MAX_WORKFLOW_BYTES = 128 * 1024
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]-]+)\s*\}\}")


def workflows_dir(project_dir: Path) -> Path:
    return project_dir / WORKFLOWS_DIRNAME


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkflowInput(BaseModel):
    """One value the user supplies before the run starts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    label: str = Field(min_length=1, max_length=80)
    type: Literal["text", "number", "boolean", "choice", "path", "model"] = "text"
    required: bool = False
    default: str | float | bool | None = None
    choices: tuple[str, ...] = ()
    help: str = Field(default="", max_length=300)

    @model_validator(mode="after")
    def check_choices(self) -> WorkflowInput:
        if self.type == "choice" and not self.choices:
            raise ValueError(f"input {self.id!r} is a choice but lists no choices")
        return self


class ToolStep(BaseModel):
    """Call one console tool with templated arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["tool"] = "tool"
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(default="", max_length=100)
    needs: tuple[str, ...] = ()
    tool: str = Field(min_length=1, max_length=64)
    arguments: dict[str, Any] = Field(default_factory=dict)
    # A tool that fails does not have to end the run: a workflow may prefer to
    # let the agent step report what was missing.
    optional: bool = False


class AgentStep(BaseModel):
    """Run one agent over a scoped set of blocks.

    ``blocks`` is the performance lever: a stage that only needs validation
    results should not carry the geometry, document, and code tool schemas.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["agent"] = "agent"
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(default="", max_length=100)
    needs: tuple[str, ...] = ()
    prompt: str = Field(min_length=1, max_length=20_000)
    role: str = Field(default="", max_length=8000)
    agent: str = Field(default="", max_length=64)
    preset: str = Field(default="", max_length=64)
    blocks: tuple[str, ...] = ()
    max_tool_rounds: int = Field(default=8, ge=1, le=60)
    max_tool_calls: int = Field(default=32, ge=0, le=400)

    @model_validator(mode="after")
    def needs_a_source(self) -> AgentStep:
        if not self.agent and not self.preset and not self.blocks:
            raise ValueError(
                f"agent step {self.id!r} names no agent, preset, or capability blocks"
            )
        return self


class GateStep(BaseModel):
    """Stop and ask a human before the rest of the run continues."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["gate"] = "gate"
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(default="", max_length=100)
    needs: tuple[str, ...] = ()
    message: str = Field(min_length=1, max_length=2000)
    detail: str = Field(default="", max_length=20_000)


class ExportStep(BaseModel):
    """Write the run's findings to an artifact the user can open."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["export"] = "export"
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    title: str = Field(default="", max_length=100)
    needs: tuple[str, ...] = ()
    name: str = Field(default="workflow-report", max_length=64)
    body: str = Field(min_length=1, max_length=40_000)


WorkflowStep = ToolStep | AgentStep | GateStep | ExportStep


class WorkflowSpec(BaseModel):
    """One preconfigured workflow, as loaded from disk."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal["1"] = "1"
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    tags: tuple[str, ...] = ()
    scope: Literal["model", "selection", "either"] = "model"
    system_prompt: str = Field(default="", max_length=20_000)
    additional_instructions: str = Field(default="", max_length=8000)
    settings: dict[str, str] = Field(default_factory=dict)
    inputs: tuple[WorkflowInput, ...] = ()
    steps: tuple[WorkflowStep, ...] = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowSpec:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("workflow step IDs must be unique")
        input_ids = [item.id for item in self.inputs]
        if len(input_ids) != len(set(input_ids)):
            raise ValueError("workflow input IDs must be unique")
        if len(self.settings) > 32:
            raise ValueError("workflow settings are limited to 32 entries")
        for key, value in self.settings.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]{0,63}", key):
                raise ValueError(f"workflow setting key {key!r} is invalid")
            if len(value) > 2000:
                raise ValueError(f"workflow setting {key!r} is too long")
        seen: set[str] = set()
        for step in self.steps:
            unknown = set(step.needs).difference(seen)
            if unknown:
                raise ValueError(
                    f"step {step.id!r} depends on {sorted(unknown)}, which do not "
                    "run before it"
                )
            seen.add(step.id)
        return self

    def summary(self) -> dict[str, Any]:
        """The listing shape the panel renders, without step bodies."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "tags": list(self.tags),
            "scope": self.scope,
            "system_prompt": self.system_prompt or _primary_prompt(self),
            "additional_instructions": self.additional_instructions,
            "settings": dict(self.settings),
            "requires_model": any(isinstance(step, AgentStep) for step in self.steps),
            "has_gate": any(isinstance(step, GateStep) for step in self.steps),
            "agents": list(
                dict.fromkeys(
                    step.agent or step.preset
                    for step in self.steps
                    if isinstance(step, AgentStep) and (step.agent or step.preset)
                )
            ),
            "inputs": [item.model_dump(mode="json") for item in self.inputs],
            "steps": [
                {
                    "id": step.id,
                    "kind": step.kind,
                    "title": step.title or _default_title(step),
                }
                for step in self.steps
            ],
        }


def _primary_prompt(spec: WorkflowSpec) -> str:
    """Best prompt preview for older workflows without ``system_prompt``."""
    for step in spec.steps:
        if isinstance(step, AgentStep):
            return step.prompt
    return ""


def _default_title(step: WorkflowStep) -> str:
    if isinstance(step, ToolStep):
        return step.tool.replace("_", " ")
    if isinstance(step, AgentStep):
        return f"{step.agent or step.preset or 'agent'} step"
    if isinstance(step, GateStep):
        return "Approval"
    return "Report"


def parse_workflow(text: str, *, source: str = "workflow") -> WorkflowSpec:
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ToolError(
            "WORKFLOW_INVALID",
            f"{source} is not valid YAML: {exc}",
            "Fix the YAML syntax and reload.",
        ) from exc
    if not isinstance(payload, Mapping):
        raise ToolError(
            "WORKFLOW_INVALID",
            f"{source} does not describe a workflow mapping.",
            "A workflow file is a mapping with name, title, and steps.",
        )
    try:
        return WorkflowSpec.model_validate(payload)
    except ValueError as exc:
        raise ToolError(
            "WORKFLOW_INVALID",
            f"{source} is not a valid workflow: {exc}",
            "Check it against an existing workflow file.",
        ) from exc


class WorkflowRegistry:
    """Built-in workflows plus the project's own, by name.

    A project file wins over a built-in of the same name, so a company can
    adapt a shipped workflow without forking the package.
    """

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = project_dir
        self.directory = workflows_dir(project_dir)

    @staticmethod
    def builtin_dir() -> Path:
        return Path(__file__).resolve().parent / "builtin" / "workflows"

    def _load_dir(self, directory: Path, origin: str) -> dict[str, WorkflowSpec]:
        found: dict[str, WorkflowSpec] = {}
        if not directory.is_dir():
            return found
        for path in sorted(directory.glob("*.yaml")):
            try:
                if path.stat().st_size > MAX_WORKFLOW_BYTES:
                    continue
                spec = parse_workflow(
                    path.read_text(encoding="utf-8"), source=f"{origin}/{path.name}"
                )
            except (OSError, ToolError):
                # One broken file must not hide every other workflow.
                continue
            found[spec.name] = spec
        return found

    def all(self) -> dict[str, WorkflowSpec]:
        specs = self._load_dir(self.builtin_dir(), "builtin")
        specs.update(self._load_dir(self.directory, "project"))
        return specs

    def entries(self) -> list[dict[str, Any]]:
        # Origin names the file actually in effect, so a project copy that
        # shadows a built-in reads as the project's own.
        project = set(self._load_dir(self.directory, "project"))
        rows = []
        for name, spec in sorted(self.all().items()):
            row = spec.summary()
            row["origin"] = "project" if name in project else "builtin"
            rows.append(row)
        return rows

    def get(self, name: str) -> WorkflowSpec:
        specs = self.all()
        spec = specs.get(name)
        if spec is None:
            known = ", ".join(sorted(specs)[:10]) or "none installed"
            raise ToolError(
                "NOT_FOUND",
                f"no workflow named {name!r}",
                f"Available workflows: {known}.",
            )
        return spec

    def save(self, spec: WorkflowSpec, *, overwrite: bool = False) -> Path:
        """Save one project workflow without replacing it accidentally."""
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f"{spec.name}.yaml"
        if target.exists() and not overwrite:
            raise ToolError(
                "FILE_EXISTS",
                f"workflow {spec.name!r} already exists",
                "Choose another name or edit the existing project workflow.",
            )
        payload = spec.model_dump(mode="json", exclude_defaults=True)
        payload["version"] = "1"
        text = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
        if len(text.encode("utf-8")) > MAX_WORKFLOW_BYTES:
            raise ToolError(
                "INVALID_INPUT",
                "workflow is too large",
                f"Keep the workflow below {MAX_WORKFLOW_BYTES // 1024} KB.",
            )
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


def _lookup(context: Mapping[str, Any], path: str) -> Any:
    current: Any = context
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return None
            current = current[part]
        elif isinstance(current, Sequence) and not isinstance(current, str):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return current


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    import json

    return json.dumps(value, ensure_ascii=False, default=str)[:4000]


def render(template: str, context: Mapping[str, Any]) -> str:
    """Substitute ``{{ inputs.x }}`` and ``{{ steps.y.text }}`` references.

    Deliberately not a template language. Workflow files are content a user
    edits, so the substitution is literal, total, and cannot execute anything.
    """
    return _PLACEHOLDER.sub(lambda match: _as_text(_lookup(context, match.group(1))), template)


def render_arguments(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return render(value, context)
    if isinstance(value, Mapping):
        return {key: render_arguments(item, context) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [render_arguments(item, context) for item in value]
    return value


@dataclass
class StepOutcome:
    """What one finished step contributes to later steps and to the reader."""

    id: str
    kind: str
    title: str
    state: Literal["succeeded", "failed", "skipped", "cancelled"]
    text: str = ""
    data: Any = None
    error: str = ""
    artifact: dict[str, Any] | None = None
    started_at: datetime = field(default_factory=_utc_now)
    finished_at: datetime | None = None

    def as_context(self) -> dict[str, Any]:
        return {"state": self.state, "text": self.text, "data": self.data, "error": self.error}

    def as_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "state": self.state,
            "text": self.text,
            "error": self.error,
            "artifact": self.artifact,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": (
                self.finished_at.isoformat(timespec="seconds") if self.finished_at else None
            ),
        }


def validate_inputs(spec: WorkflowSpec, values: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce and check the user's answers against the declared inputs."""
    resolved: dict[str, Any] = {}
    for item in spec.inputs:
        raw = values.get(item.id, item.default)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if item.required:
                raise ToolError(
                    "INVALID_INPUT",
                    f"workflow input {item.id!r} ({item.label}) is required",
                    item.help or "Provide a value and run again.",
                )
            resolved[item.id] = "" if item.type in {"text", "path", "choice"} else None
            continue
        if item.type == "number":
            try:
                resolved[item.id] = float(raw)
            except (TypeError, ValueError) as exc:
                raise ToolError(
                    "INVALID_INPUT",
                    f"workflow input {item.id!r} must be a number",
                    "Enter a numeric value.",
                ) from exc
        elif item.type == "boolean":
            resolved[item.id] = raw if isinstance(raw, bool) else str(raw).lower() == "true"
        elif item.type == "choice":
            text = str(raw)
            if text not in item.choices:
                raise ToolError(
                    "INVALID_INPUT",
                    f"workflow input {item.id!r} must be one of {list(item.choices)}",
                    "Pick one of the offered choices.",
                )
            resolved[item.id] = text
        else:
            resolved[item.id] = str(raw)
    return resolved


def validate_settings(
    spec: WorkflowSpec, values: Mapping[str, Any] | None
) -> dict[str, str]:
    """Merge free-form run settings over a workflow's reusable defaults."""
    if values is not None and not isinstance(values, Mapping):
        raise ToolError(
            "INVALID_INPUT",
            "workflow settings must be key/value pairs",
            "Use a short setting name and a text value.",
        )
    # ``None`` means the caller did not submit settings and wants the saved
    # defaults. A mapping is the complete run-time set, which lets the Setup
    # panel remove a saved default by omitting its row.
    merged: dict[str, str] = dict(spec.settings) if values is None else {}
    for raw_key, raw_value in (values or {}).items():
        key = str(raw_key).strip()
        if not key and (raw_value is None or not str(raw_value).strip()):
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_. -]{0,63}", key):
            raise ToolError(
                "INVALID_INPUT",
                f"workflow setting key {key!r} is invalid",
                "Start with a letter and use up to 64 letters, numbers, spaces, dots, underscores, or hyphens.",
            )
        value = "" if raw_value is None else str(raw_value).strip()
        if len(value) > 2000:
            raise ToolError(
                "INVALID_INPUT",
                f"workflow setting {key!r} is too long",
                "Keep each setting value below 2,000 characters.",
            )
        if value:
            merged[key] = value
        else:
            merged.pop(key, None)
    if len(merged) > 32:
        raise ToolError(
            "INVALID_INPUT",
            "a workflow run can have at most 32 settings",
            "Remove settings that do not affect this run.",
        )
    return merged


def steps_in_order(spec: WorkflowSpec) -> Iterator[WorkflowStep]:
    """Declaration order, which validation already proved is dependency order."""
    yield from spec.steps


# A workflow attached to a chat turn travels as standing instructions, so the
# whole thing has to fit beside the agent's own prompt.
CHAT_INSTRUCTIONS_CHARS = 30_000


def _render_for_chat(template: str, context: Mapping[str, Any]) -> str:
    """Render settings and scope, but show ``{{ steps.x.text }}`` as words.

    In a chat there is no runner filling step results in, so a step reference
    is shown to the model as the thing it stands for rather than as nothing.
    """

    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        if path.startswith("steps."):
            return f"<{path}>"
        return _as_text(_lookup(context, path))

    return _PLACEHOLDER.sub(replace, template)


def _render_arguments_for_chat(value: Any, context: Mapping[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_for_chat(value, context)
    if isinstance(value, Mapping):
        return {key: _render_arguments_for_chat(item, context) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_render_arguments_for_chat(item, context) for item in value]
    return value


def _step_kind_label(step: WorkflowStep) -> str:
    if isinstance(step, ToolStep):
        return "tool"
    if isinstance(step, AgentStep):
        return "agent"
    if isinstance(step, GateStep):
        return "decision"
    return "report"


def _chat_procedure(spec: WorkflowSpec, context: Mapping[str, Any]) -> str:
    lines = ["Procedure for this workflow, in order:"]
    for number, step in enumerate(spec.steps, start=1):
        title = step.title or _default_title(step)
        head = f"{number}. {title} ({_step_kind_label(step)}): "
        if isinstance(step, ToolStep):
            import json

            arguments = _render_arguments_for_chat(dict(step.arguments), context)
            rendered = json.dumps(arguments, ensure_ascii=False, default=str)
            suffix = " Skip it if the call fails." if step.optional else ""
            lines.append(f"{head}call {step.tool} with {rendered}.{suffix}")
        elif isinstance(step, AgentStep):
            lines.append(head + _render_for_chat(step.prompt, context).strip())
        elif isinstance(step, GateStep):
            message = _render_for_chat(step.message, context).strip()
            lines.append(
                f"{head}stop and ask the user before continuing: {message} "
                "Wait for their answer; do not carry on without it."
            )
        else:
            body = _render_for_chat(step.body, context).strip()
            lines.append(f"{head}finish with a Markdown report in this shape:\n{body}")
    return "\n".join(lines)


def chat_instructions(
    spec: WorkflowSpec,
    *,
    scope: Mapping[str, Any] | None = None,
    settings: Mapping[str, str] | None = None,
    note: str = "",
) -> str:
    """The workflow as standing instructions for one chat conversation.

    The agent panel runs a workflow without the staged runner: the system
    prompt, project instructions, run settings, viewer scope, and a procedure
    written from the steps all become part of the agent's system prompt, and
    the conversation carries on from there with the same tools and approvals.
    """
    run_settings = dict(spec.settings) if settings is None else dict(settings)
    scope_context = dict(scope or {})
    context: dict[str, Any] = {
        "inputs": {},
        "settings": run_settings,
        "steps": {},
        "scope": scope_context,
        "run": {"note": note.strip()},
    }
    parts = [render(spec.system_prompt, context).strip()]
    if spec.additional_instructions.strip():
        parts.append(
            "Additional instructions:\n" + render(spec.additional_instructions, context).strip()
        )
    run_context = ["Run context (treat these values as instructions for this run):"]
    scope_text = str(scope_context.get("text") or "").strip()
    if scope_text:
        run_context.append(f"Scope:\n{scope_text}")
    if run_settings:
        rows = "\n".join(f"- {key}: {value}" for key, value in run_settings.items())
        run_context.append(f"Settings:\n{rows}")
    if note.strip():
        run_context.append(f"Additional guidance:\n{note.strip()}")
    if len(run_context) > 1:
        parts.append("\n\n".join(run_context))
    parts.append(_chat_procedure(spec, context))
    text = "\n\n".join(part for part in parts if part)
    if len(text) > CHAT_INSTRUCTIONS_CHARS:
        text = text[:CHAT_INSTRUCTIONS_CHARS].rstrip() + "\n\n[workflow instructions truncated]"
    return text


def chat_task_prompt(spec: WorkflowSpec) -> str:
    """What to ask when the person pressed Run without typing anything."""
    context = {"inputs": {}, "settings": dict(spec.settings), "steps": {}, "scope": {}, "run": {}}
    for step in spec.steps:
        if isinstance(step, AgentStep):
            prompt = _render_for_chat(step.prompt, context).strip()
            if prompt:
                return prompt
    return "Carry out the workflow procedure above and return the report."


__all__ = [
    "CHAT_INSTRUCTIONS_CHARS",
    "MAX_WORKFLOW_BYTES",
    "WORKFLOWS_DIRNAME",
    "AgentStep",
    "ExportStep",
    "GateStep",
    "StepOutcome",
    "ToolStep",
    "WorkflowInput",
    "WorkflowRegistry",
    "WorkflowSpec",
    "WorkflowStep",
    "chat_instructions",
    "chat_task_prompt",
    "parse_workflow",
    "render",
    "render_arguments",
    "steps_in_order",
    "validate_inputs",
    "validate_settings",
    "workflows_dir",
]
