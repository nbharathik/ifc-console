"""The Python SDK: use ifc-console from a script or an agent.

No server, no terminal, no port. `Workbench` opens a model, runs the same
transport-neutral operations that MCP projects as tools, and returns plain
Python data.

    from ifc_console import Workbench

    with Workbench.open("tower.ifc") as wb:
        walls = wb.query("IfcWall, Pset_WallCommon.FireRating=F30")
        report = wb.validate()

Agent bindings are model agnostic: `wb.tools()` hands out JSON Schema tool
definitions any provider can consume, and `wb.call(name, **kwargs)` runs one.
Nothing here talks to an LLM API or depends on a particular vendor.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import (
    AsyncIterator,
    Callable,
    Coroutine,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Any

from pydantic import ValidationError

from ifc_console.audit import AuditVerification
from ifc_console.core.artifacts import ArtifactGCPlan, ArtifactGCResult, ArtifactRef
from ifc_console.core.batches import (
    BatchChildRecord,
    BatchRecord,
    BatchSpec,
    BatchState,
    QueryBatchOperation,
    ValidationBatchOperation,
)
from ifc_console.core.capabilities import Authority, Capability, CapabilityDecision
from ifc_console.core.changes import (
    Approval,
    ApprovalRecord,
    ChangeSet,
    ChangeSetRecord,
    ClassificationAssignmentChange,
    CommitRecord,
    CommitResult,
    IfcScalar,
    PropertyCreateChange,
    PropertyValueChange,
    RestoreRecord,
    RestoreResult,
)
from ifc_console.core.context import ModelContext, OperationContext, WorkspaceContext
from ifc_console.core.jobs import (
    CommitJobSpec,
    JobEvent,
    JobFailure,
    JobRecord,
    JobState,
    QueryJobSpec,
    RestoreJobSpec,
    SourceFileRef,
    ValidationJobSpec,
)
from ifc_console.core.operation_data import (
    QueryElementsData,
    SearchElementsData,
    ValidationData,
    ValidationIssue,
)
from ifc_console.core.operations import OperationAnnotations, OperationDefinition, OperationImage
from ifc_console.core.results import Envelope, ErrorInfo, ToolError
from ifc_console.core.revisions import RevisionRef
from ifc_console.core.transaction_journal import (
    TransactionJournal,
    TransactionKind,
    TransactionPhase,
)
from ifc_console.core.workflows import (
    WorkflowInputSpec,
    WorkflowPlan,
    WorkflowQueryOperation,
    WorkflowRecord,
    WorkflowSpec,
    WorkflowState,
    WorkflowStepPlan,
    WorkflowStepRecord,
    WorkflowStepSpec,
    WorkflowStepState,
    WorkflowValidationOperation,
)
from ifc_console.plugins import (
    OperationPlugin,
    PluginAPI,
    PluginManifest,
    PluginRecord,
)

__all__ = [
    "AsyncWorkbench",
    "Approval",
    "ApprovalRecord",
    "Authority",
    "ArtifactRef",
    "ArtifactGCPlan",
    "ArtifactGCResult",
    "AuditVerification",
    "BatchChildRecord",
    "BatchRecord",
    "BatchSpec",
    "BatchState",
    "Capability",
    "CapabilityDecision",
    "ChangeSet",
    "ChangeSetRecord",
    "ClassificationAssignmentChange",
    "CommitJobSpec",
    "CommitRecord",
    "CommitResult",
    "Envelope",
    "ErrorInfo",
    "IfcConsoleError",
    "IfcScalar",
    "JobRecord",
    "JobEvent",
    "JobFailure",
    "JobState",
    "ModelContext",
    "OperationAnnotations",
    "OperationContext",
    "OperationDefinition",
    "OperationPlugin",
    "PluginAPI",
    "PluginManifest",
    "PluginRecord",
    "PropertyCreateChange",
    "PropertyValueChange",
    "QueryBatchOperation",
    "QueryElementsData",
    "QueryJobSpec",
    "RestoreJobSpec",
    "RestoreRecord",
    "RestoreResult",
    "RevisionRef",
    "SearchElementsData",
    "SourceFileRef",
    "TransactionJournal",
    "TransactionKind",
    "TransactionPhase",
    "ValidationData",
    "ValidationBatchOperation",
    "ValidationIssue",
    "ValidationJobSpec",
    "WorkflowInputSpec",
    "WorkflowPlan",
    "WorkflowQueryOperation",
    "WorkflowRecord",
    "WorkflowSpec",
    "WorkflowState",
    "WorkflowStepPlan",
    "WorkflowStepRecord",
    "WorkflowStepSpec",
    "WorkflowStepState",
    "WorkflowValidationOperation",
    "Workbench",
    "WorkspaceContext",
]

_DEFAULT_TIMEOUT = 600.0
_DEFAULT_TIMEOUT_SENTINEL = object()
_LIFECYCLE_SETTINGS = frozenset(
    {
        "chat.enabled_default",
        "files.allowed_dirs",
        "knowledge.schemas",
        "logging.file_enabled",
        "logging.level",
        "mode.default",
        "server.persistent_token",
        "server.port",
        "viewer.enabled_default",
    }
)


class IfcConsoleError(RuntimeError):
    """A tool refused the call. Carries the same code and hint the LLM sees."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" ({hint})" if hint else ""))
        self.code = code
        self.message = message
        self.hint = hint


def _sdk_error(exc: ToolError) -> IfcConsoleError:
    return IfcConsoleError(exc.code, exc.message, exc.hint)


def _apply_settings(store: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """In-memory settings for this workbench only; the user's file is untouched."""
    payload = store.settings.model_dump()
    for dotted, value in overrides.items():
        target = payload
        *parents, leaf = dotted.split(".")
        if not leaf or any(not part for part in parents):
            raise IfcConsoleError(
                "INVALID_INPUT", f"unknown setting {dotted!r}", "See docs/settings.md."
            )
        for part in parents:
            nested = target.get(part)
            if not isinstance(nested, dict):
                raise IfcConsoleError(
                    "INVALID_INPUT", f"unknown setting {dotted!r}", "See docs/settings.md."
                )
            target = nested
        if leaf not in target:
            raise IfcConsoleError(
                "INVALID_INPUT", f"unknown setting {dotted!r}", "See docs/settings.md."
            )
        target[leaf] = value
    try:
        store.settings = type(store.settings).model_validate(payload)
    except ValidationError as exc:
        issues = "; ".join(
            f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
            for issue in exc.errors(include_url=False, include_input=False)
        )
        raise IfcConsoleError(
            "INVALID_INPUT",
            f"invalid setting overrides: {issues}",
            "See docs/settings.md for accepted values and limits.",
        ) from None
    return {key: store.get(key) for key in overrides}


def _refresh_live_settings(core: Any, keys: Iterable[str]) -> None:
    """Refresh constructor-captured policy values after session overrides."""

    apply = {
        "workspace.max_resident": lambda value: setattr(core.models, "max_resident", max(1, value)),
        "workspace.max_total_mb": lambda value: setattr(core.models, "max_total_mb", max(0, value)),
        "workspace.scan_cap": lambda value: setattr(core.workspace, "cap", value),
        "workspace.scan_depth": lambda value: setattr(core.workspace, "depth", value),
        "exec.allow_system_access": lambda value: setattr(
            core.policy, "allow_system_access", value
        ),
        "files.allow_ai_save": lambda value: setattr(core.policy, "allow_ai_save", value),
        "files.backup_retention": lambda value: setattr(core.backups, "retention", value),
        "recents.max": lambda value: setattr(core.recents, "max_entries", value),
        "sessions.retention": lambda value: setattr(core.audit, "retention", value),
        "automation.artifact_retention_days": lambda value: setattr(
            core.artifact_retention, "default_retention_days", value
        ),
        "automation.jobs_retention": lambda value: (
            setattr(core.jobs, "retention", value),
            setattr(core.batches, "retention", value),
            setattr(core.workflows, "retention", value),
        ),
        "chat.provider": lambda value: setattr(core.chat, "provider", value),
        "chat.model": lambda value: setattr(core.chat, "model", value),
        "chat.base_url": lambda value: setattr(core.chat, "base_url", value),
        "tui.theme": lambda value: setattr(core, "ui_theme", value),
    }
    for key in keys:
        refresh = apply.get(key)
        if refresh is not None:
            refresh(core.store.get(key))


def _transport_envelope(result: Any, meta: dict[str, Any]) -> Envelope | None:
    """Image transport content as an envelope, so every caller can consume it.

    MCP clients receive native image content; SDK and agent callers get the
    same pixels as base64 in data.images. Anything unrecognized stays None
    and keeps the INVALID_OUTPUT contract.
    """
    import base64

    items = result if isinstance(result, (list, tuple)) else [result]
    images: list[dict[str, str]] = []
    notes: list[str] = []
    for item in items:
        if isinstance(item, OperationImage):
            images.append(
                {
                    "media_type": f"image/{item.format.lower()}",
                    "data": base64.b64encode(item.data).decode("ascii"),
                }
            )
        elif isinstance(item, str):
            notes.append(item)
        else:
            return None
    if not images:
        return None
    data: dict[str, Any] = {"images": images}
    if notes:
        data["note"] = " ".join(notes)
    return Envelope(ok=True, data=data, meta={**meta, "transport": "content"})


def _unwrap(envelope: Any) -> dict[str, Any]:
    payload = envelope.model_dump() if hasattr(envelope, "model_dump") else dict(envelope)
    if payload.get("ok"):
        return payload.get("data") or {}
    error = payload.get("error") or {}
    raise IfcConsoleError(
        error.get("code", "INTERNAL_ERROR"),
        error.get("message", "the tool failed"),
        error.get("hint", ""),
    )


class AsyncWorkbench:
    """The async face. Every method is a coroutine; see Workbench for sync use."""

    def __init__(self, core: Any) -> None:
        self._core = core
        self._closed = False

    # -- construction ---------------------------------------------------------
    @classmethod
    async def create(
        cls,
        path: str | Path | None = None,
        *,
        mode: str = "ask",
        home: str | Path | None = None,
        allowed_dirs: tuple[str | Path, ...] = (),
        settings: dict[str, Any] | None = None,
        project_dir: str | Path | None = None,
    ) -> AsyncWorkbench:
        from ifc_console.app import AppCore
        from ifc_console.application.operations import build_operations
        from ifc_console.policy.modes import Mode
        from ifc_console.settings import SettingsStore

        store = SettingsStore(
            home=Path(home) if home else None,
            project_dir=Path(project_dir) if project_dir else None,
        )
        _apply_settings(store, settings or {})
        try:
            selected_mode = Mode(mode)
        except ValueError:
            raise IfcConsoleError(
                "INVALID_INPUT",
                f"unknown mode {mode!r}",
                "Use 'ask' for read-only work or 'edit' for mutations.",
            ) from None
        core = AppCore(store, mode=selected_mode, transport="sdk")
        try:
            core.start_audit()
            if project_dir:
                core.add_allowed_dir(Path(project_dir))
            for directory in allowed_dirs:
                core.add_allowed_dir(Path(directory))
            workbench = cls(core)
            build_operations(core)
            if path is not None:
                await workbench.open_model(path)
            return workbench
        except BaseException:
            with suppress(Exception):
                await core.ashutdown()
            raise

    async def __aenter__(self) -> AsyncWorkbench:
        return self

    async def __aexit__(
        self, exc_type: type | None, exc: BaseException | None, _tb: TracebackType | None
    ) -> None:
        await self.aclose()

    # -- session --------------------------------------------------------------
    @property
    def core(self) -> Any:
        """The AppCore underneath, for anything the SDK does not wrap yet."""
        return self._core

    @property
    def mode(self) -> str:
        return self._core.policy.mode.value

    @property
    def context(self) -> WorkspaceContext:
        """Stable identity and revision context for the current workspace."""
        return self._core.operation_service.workspace_context()

    def set_mode(self, mode: str) -> str:
        """Change what this process may do to the model.

        In the console the user owns this switch and the LLM cannot touch it.
        In a script you are the user, so it is yours; an agent loop should keep
        it in the caller's hands, not hand it to the model as a tool.
        """
        from ifc_console.policy.modes import Mode

        try:
            selected_mode = Mode(mode)
        except ValueError:
            raise IfcConsoleError(
                "INVALID_INPUT",
                f"unknown mode {mode!r}",
                "Use 'ask' for read-only work or 'edit' for mutations.",
            ) from None
        self._core.set_mode(selected_mode, by="sdk")
        return self.mode

    @property
    def settings(self) -> dict[str, Any]:
        """A flat snapshot of this workbench's effective settings."""

        return self._core.store.flat()

    def get_setting(self, key: str) -> Any:
        """Read one effective setting by dotted name."""

        try:
            return self._core.store.get(key)
        except KeyError:
            raise IfcConsoleError(
                "INVALID_INPUT", f"unknown setting {key!r}", "See docs/settings.md."
            ) from None

    def configure(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and apply in-memory settings without editing user files.

        Operational settings are read on each tool call. Lifecycle settings,
        including the server port and persistent token policy, belong in
        ``create(settings=...)`` so they are available while the core starts.
        """

        deferred = sorted(_LIFECYCLE_SETTINGS.intersection(overrides))
        if deferred:
            raise IfcConsoleError(
                "INVALID_INPUT",
                f"lifecycle settings cannot change after startup: {', '.join(deferred)}",
                "Pass them to Workbench.open() or LocalRuntime.open() in settings=.",
            )
        values = _apply_settings(self._core.store, dict(overrides))
        _refresh_live_settings(self._core, values)
        return values

    async def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        self._core.add_allowed_dir(target.parent)
        try:
            await self._core.open_model(target, **kwargs)
        except ToolError as exc:
            raise _sdk_error(exc) from exc
        return self.model

    @property
    def model(self) -> dict[str, Any]:
        session = self._core.session
        return {
            "loaded": session.loaded,
            "name": session.name,
            "path": str(session.path) if session.path else None,
            "schema": session.schema,
            "dirty": session.dirty,
            "fingerprint": session.fingerprint,
            "content_sha256": session.source_sha256,
            "model_id": getattr(session, "model_id", None),
        }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._core.shutdown()

    async def aclose(self) -> None:
        """Await supervised job shutdown and transaction rollback."""
        if self._closed:
            return
        self._closed = True
        await self._core.ashutdown()

    # -- the tool surface -----------------------------------------------------
    async def tools(self, *, permitted_only: bool = False) -> list[dict[str, Any]]:
        """Return provider-neutral JSON Schema definitions for the live tool set."""
        tools: list[dict[str, Any]] = []
        for definition in self.operation_definitions():
            permitted = self._core.policy.evaluate(list(definition.required_capabilities)).allowed
            if permitted_only and not permitted:
                continue
            tools.append(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "input_schema": definition.input_schema,
                    "output_schema": definition.output_schema,
                    "data_schema": definition.data_schema,
                    "annotations": definition.annotations.model_dump(exclude_none=True),
                    "required_capabilities": [
                        capability.value for capability in definition.required_capabilities
                    ],
                    "permitted": permitted,
                }
            )
        return tools

    def operation_definitions(self) -> list[OperationDefinition]:
        """Typed definitions for embedding IFC operations in another client."""
        return self._core.operation_service.definitions()

    def granted_capabilities(self, *, authority: Authority = "tool") -> tuple[Capability, ...]:
        """Capabilities granted by the current ask/edit compatibility profile."""
        return self._core.policy.granted_capabilities(authority=authority)

    def capability_decision(
        self,
        required: list[Capability | str] | tuple[Capability | str, ...],
        *,
        authority: Authority = "tool",
    ) -> CapabilityDecision:
        return self._core.policy.evaluate(list(required), authority=authority)

    def verify_audit(self, session_id: str | None = None) -> AuditVerification:
        """Verify the current or selected local audit hash chain."""
        return self._core.audit.verify_session(session_id)

    async def call_result(self, name: str, **kwargs: Any) -> Envelope:
        """Run one structured operation and return its typed envelope."""
        spec = self._core.operations.get(name)
        if spec is None:
            known = ", ".join(sorted(self._core.operations.handlers))
            raise IfcConsoleError(
                "NOT_FOUND", f"no operation named {name!r}", f"Operations: {known}"
            )
        result = await self._core.operation_service.call(name, kwargs)
        if not isinstance(result, Envelope):
            converted = _transport_envelope(result, self._core.session_meta())
            if converted is None:
                raise IfcConsoleError(
                    "INVALID_OUTPUT",
                    f"operation {name!r} returns transport content, not an envelope",
                    "Call it through its supported transport.",
                )
            return converted
        return result

    async def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        """Run one tool and return its envelope, errors included.

        This is what an agent loop should use: the envelope's error code and
        hint are written for a model to read and retry from.
        """
        return (await self.call_result(name, **kwargs)).model_dump()

    async def _data(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return _unwrap(await self.call(name, **kwargs))

    # -- reads ----------------------------------------------------------------
    async def info(self) -> dict[str, Any]:
        return await self._data("get_ifc_project_info")

    async def status(self) -> dict[str, Any]:
        return await self._data("get_session_status")

    async def orient(self) -> dict[str, Any]:
        return await self._data("orient")

    async def tree(self, root: str | None = None, depth: int = 10) -> dict[str, Any]:
        return await self._data("get_spatial_structure", root_global_id=root, depth=depth)

    async def query(self, selector: str, **kwargs: Any) -> list[dict[str, Any]]:
        data = await self._data("query_elements", query=selector, **kwargs)
        return data.get("rows", [])

    async def search(self, term: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Resolve a name, partial name, GlobalId, or simple selector."""

        data = await self._data("search_elements", term=term, **kwargs)
        return data.get("results", [])

    async def search_result(self, term: str, **kwargs: Any) -> SearchElementsData:
        result = await self.call_result("search_elements", term=term, **kwargs)
        return SearchElementsData.model_validate(_unwrap(result))

    async def query_result(self, selector: str, **kwargs: Any) -> QueryElementsData:
        """Query elements and validate the operation-specific data contract."""
        result = await self.call_result("query_elements", query=selector, **kwargs)
        data = _unwrap(result)
        return QueryElementsData.model_validate(data)

    async def element(self, global_ids: str | list[str], **kwargs: Any) -> list[dict[str, Any]]:
        ids = [global_ids] if isinstance(global_ids, str) else list(global_ids)
        data = await self._data("get_element", global_ids=ids, **kwargs)
        return data.get("elements", [])

    async def psets(self, global_ids: str | list[str], **kwargs: Any) -> dict[str, Any]:
        ids = [global_ids] if isinstance(global_ids, str) else list(global_ids)
        return await self._data("get_psets", global_ids=ids, **kwargs)

    async def quantities(self, selector: str, by: str = "class", **kwargs: Any) -> dict[str, Any]:
        return await self._data("compute_quantities", selector=selector, aggregate_by=by, **kwargs)

    async def validate(self, **kwargs: Any) -> dict[str, Any]:
        return await self._data("validate_model", **kwargs)

    async def validation_result(self, **kwargs: Any) -> ValidationData:
        """Validate the model and return a typed validation report."""
        result = await self.call_result("validate_model", **kwargs)
        data = _unwrap(result)
        return ValidationData.model_validate(data)

    # -- durable jobs and artifacts -------------------------------------------
    async def submit_validation_job(
        self,
        *,
        model: str | None = None,
        ids_paths: tuple[str | Path, ...] = (),
        express_rules: bool = False,
        max_issues: int = 200,
        expected_revision: str | None = None,
    ) -> JobRecord:
        """Submit isolated validation and return immediately with a job record."""
        try:
            return await self._core.jobs.submit_validation(
                model=model,
                ids_paths=ids_paths,
                express_rules=express_rules,
                max_issues=max_issues,
                expected_revision=expected_revision,
            )
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def submit_commit_job(self, change_set_id: str, *, approval_id: str) -> JobRecord:
        """Submit a journaled commit and return before it completes."""
        try:
            return await self._core.jobs.submit_commit(change_set_id, approval_id=approval_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def submit_restore_job(self, commit_id: str, *, confirm: bool = False) -> JobRecord:
        """Submit a journaled restore and return before it completes."""
        try:
            return await self._core.jobs.submit_restore(commit_id, confirm=confirm)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def job(self, job_id: str) -> JobRecord:
        try:
            return self._core.jobs.get(job_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def jobs(self, *, limit: int = 100) -> list[JobRecord]:
        return self._core.jobs.list(limit=limit)

    async def wait_job(self, job_id: str, *, timeout: float | None = None) -> JobRecord:
        try:
            return await self._core.jobs.wait(job_id, timeout=timeout)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def watch_job(
        self, job_id: str, *, poll_interval: float = 0.1
    ) -> AsyncIterator[JobRecord]:
        """Yield durable progress snapshots until the job is terminal."""
        try:
            async for record in self._core.jobs.watch(job_id, poll_interval=poll_interval):
                yield record
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def cancel_job(self, job_id: str) -> JobRecord:
        try:
            return await self._core.jobs.cancel(job_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def submit_validation_batch(
        self,
        inputs: tuple[str | Path, ...] | list[str | Path],
        *,
        ids_paths: tuple[str | Path, ...] = (),
        express_rules: bool = False,
        max_issues: int = 200,
        concurrency: int = 2,
        failure_policy: str = "continue",
    ) -> BatchRecord:
        """Capture and submit bounded validation for one or more IFC files."""
        input_paths = tuple(Path(path).expanduser().resolve() for path in inputs)
        ids = tuple(Path(path).expanduser().resolve() for path in ids_paths)
        for path in (*input_paths, *ids):
            self._core.add_allowed_dir(path.parent)
        try:
            return await self._core.batches.submit_validation(
                input_paths,
                ids_paths=ids,
                express_rules=express_rules,
                max_issues=max_issues,
                concurrency=concurrency,
                failure_policy=failure_policy,
            )
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def submit_query_batch(
        self,
        inputs: tuple[str | Path, ...] | list[str | Path],
        *,
        query: str,
        fields: tuple[str, ...] = ("name", "storey", "type_name"),
        order_by: str = "class",
        output_format: str = "jsonl",
        limit: int = 100_000,
        concurrency: int = 2,
        failure_policy: str = "continue",
    ) -> BatchRecord:
        """Stream one selector query result artifact per captured IFC file."""
        input_paths = tuple(Path(path).expanduser().resolve() for path in inputs)
        for path in input_paths:
            self._core.add_allowed_dir(path.parent)
        try:
            return await self._core.batches.submit_query(
                input_paths,
                query=query,
                fields=fields,
                order_by=order_by,
                output_format=output_format,
                limit=limit,
                concurrency=concurrency,
                failure_policy=failure_policy,
            )
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def batch(self, batch_id: str) -> BatchRecord:
        try:
            return self._core.batches.get(batch_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def batches(self, *, limit: int = 100) -> list[BatchRecord]:
        return self._core.batches.list(limit=limit)

    async def wait_batch(self, batch_id: str, *, timeout: float | None = None) -> BatchRecord:
        try:
            return await self._core.batches.wait(batch_id, timeout=timeout)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def watch_batch(
        self, batch_id: str, *, poll_interval: float = 0.1
    ) -> AsyncIterator[BatchRecord]:
        try:
            async for record in self._core.batches.watch(batch_id, poll_interval=poll_interval):
                yield record
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def cancel_batch(self, batch_id: str) -> BatchRecord:
        try:
            return await self._core.batches.cancel(batch_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def resume_batch(self, batch_id: str) -> BatchRecord:
        try:
            return await self._core.batches.resume(batch_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def plan_workflow(self, manifest_path: str | Path) -> WorkflowPlan:
        """Resolve and hash a workflow manifest without scheduling any work."""
        path = Path(manifest_path).expanduser().resolve()
        self._core.add_allowed_dir(path.parent)
        try:
            return await self._core.workflows.plan_manifest(path)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def plan_workflow_spec(self, spec: WorkflowSpec, *, base_dir: str | Path) -> WorkflowPlan:
        """Resolve a typed in-memory workflow relative to an explicit directory."""
        base = Path(base_dir).expanduser().resolve()
        self._core.add_allowed_dir(base)
        try:
            return await self._core.workflows.plan_spec(spec, base_dir=base)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def submit_workflow(self, manifest_path: str | Path) -> WorkflowRecord:
        """Plan and submit a versioned read-only workflow manifest."""
        path = Path(manifest_path).expanduser().resolve()
        self._core.add_allowed_dir(path.parent)
        try:
            return await self._core.workflows.submit_manifest(path)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def submit_workflow_plan(self, plan: WorkflowPlan) -> WorkflowRecord:
        try:
            return await self._core.workflows.submit_plan(plan)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def workflow(self, workflow_id: str) -> WorkflowRecord:
        try:
            return self._core.workflows.get(workflow_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def workflows(self, *, limit: int = 100) -> list[WorkflowRecord]:
        return self._core.workflows.list(limit=limit)

    async def wait_workflow(
        self, workflow_id: str, *, timeout: float | None = None
    ) -> WorkflowRecord:
        try:
            return await self._core.workflows.wait(workflow_id, timeout=timeout)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def watch_workflow(
        self, workflow_id: str, *, poll_interval: float = 0.1
    ) -> AsyncIterator[WorkflowRecord]:
        try:
            async for record in self._core.workflows.watch(
                workflow_id, poll_interval=poll_interval
            ):
                yield record
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def cancel_workflow(self, workflow_id: str) -> WorkflowRecord:
        try:
            return await self._core.workflows.cancel(workflow_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def resume_workflow(self, workflow_id: str) -> WorkflowRecord:
        try:
            return await self._core.workflows.resume(workflow_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def artifact(self, artifact_id: str) -> ArtifactRef:
        try:
            return self._core.artifacts.get(artifact_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def artifacts(self, *, limit: int = 100) -> list[ArtifactRef]:
        return self._core.artifacts.list(limit=limit)

    def transaction_journal(self, transaction_id: str) -> TransactionJournal:
        """Return one durable commit or restore state machine."""
        try:
            return self._core.transactions.journals.get(transaction_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def transaction_journals(self) -> list[TransactionJournal]:
        """List durable commit and restore journals oldest first."""
        return self._core.transactions.journals.list()

    def read_artifact(self, artifact_id: str) -> bytes:
        try:
            return self._core.artifacts.read_bytes(artifact_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def read_artifact_text(self, artifact_id: str) -> str:
        return self.read_artifact(artifact_id).decode("utf-8")

    def export_artifact(
        self, artifact_id: str, path: str | Path, *, overwrite: bool = False
    ) -> Path:
        try:
            return self._core.artifacts.export(artifact_id, Path(path), overwrite=overwrite)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def pin_artifact(self, artifact_id: str) -> ArtifactRef:
        try:
            ref = self._core.artifact_retention.pin(artifact_id)
            self._core.audit.record("artifact_pinned", artifact_id=ref.artifact_id)
            return ref
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def unpin_artifact(self, artifact_id: str) -> bool:
        try:
            removed = self._core.artifact_retention.unpin(artifact_id)
            self._core.audit.record(
                "artifact_unpinned", artifact_id=artifact_id, pin_existed=removed
            )
            return removed
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def plan_artifact_gc(self, *, older_than_days: int | None = None) -> ArtifactGCPlan:
        try:
            plan = self._core.artifact_retention.plan(older_than_days=older_than_days)
            self._core.audit.record(
                "artifact_gc_planned",
                cutoff=plan.cutoff.isoformat(),
                candidate_count=plan.candidate_count,
                candidate_bytes=plan.candidate_bytes,
            )
            return plan
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def collect_artifacts(self, plan: ArtifactGCPlan, *, confirm: bool = False) -> ArtifactGCResult:
        try:
            result = self._core.artifact_retention.collect(plan, confirm=confirm)
            self._core.audit.record(
                "artifact_gc_completed",
                deleted_count=result.deleted_count,
                deleted_bytes=result.deleted_bytes,
                deleted_ids=list(result.deleted_ids),
            )
            return result
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    # -- safe structured changes --------------------------------------------
    async def preview_property_change(
        self,
        global_ids: str | list[str] | tuple[str, ...],
        *,
        pset_name: str,
        property_name: str,
        value: IfcScalar,
        create_missing: bool = False,
        nominal_type: str | None = None,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        ids = (global_ids,) if isinstance(global_ids, str) else tuple(global_ids)
        try:
            return await self._core.transactions.preview_property_value(
                global_ids=ids,
                pset_name=pset_name,
                property_name=property_name,
                value=value,
                create_missing=create_missing,
                nominal_type=nominal_type,
                expected_revision=expected_revision,
            )
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def preview_classification_assignment(
        self,
        global_ids: str | list[str] | tuple[str, ...],
        *,
        classification_name: str,
        identification: str,
        reference_name: str,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        ids = (global_ids,) if isinstance(global_ids, str) else tuple(global_ids)
        try:
            return await self._core.transactions.preview_classification_assignment(
                global_ids=ids,
                classification_name=classification_name,
                identification=identification,
                reference_name=reference_name,
                expected_revision=expected_revision,
            )
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def change_set(self, change_set_id: str) -> ChangeSetRecord:
        try:
            return self._core.transactions.get_change_set(change_set_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def approve_change_set(
        self, change_set_id: str, *, approved_by: str, reason: str = ""
    ) -> ApprovalRecord:
        try:
            return self._core.transactions.approve(
                change_set_id, approved_by=approved_by, reason=reason
            )
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def commit_change_set(self, change_set_id: str, *, approval_id: str) -> CommitRecord:
        submitted = await self.submit_commit_job(change_set_id, approval_id=approval_id)
        completed = await self.wait_job(submitted.job_id)
        self._raise_failed_job(completed)
        commit_id = str(completed.summary.get("commit_id") or "")
        if not commit_id:
            raise IfcConsoleError(
                "JOB_RESULT_INVALID",
                "the completed commit job has no commit receipt ID",
                "Inspect the job and transaction journal.",
            )
        return self.commit_record(commit_id)

    def commit_record(self, commit_id: str) -> CommitRecord:
        try:
            return self._core.transactions.get_commit(commit_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def restore_commit(self, commit_id: str, *, confirm: bool = False) -> RestoreRecord:
        submitted = await self.submit_restore_job(commit_id, confirm=confirm)
        completed = await self.wait_job(submitted.job_id)
        self._raise_failed_job(completed)
        restore_id = str(completed.summary.get("restore_id") or "")
        if not restore_id:
            raise IfcConsoleError(
                "JOB_RESULT_INVALID",
                "the completed restore job has no restore receipt ID",
                "Inspect the job and transaction journal.",
            )
        try:
            return self._core.transactions.get_restore(restore_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    @staticmethod
    def _raise_failed_job(record: JobRecord) -> None:
        if record.state.value == "succeeded":
            return
        if record.failure is not None:
            raise IfcConsoleError(
                record.failure.code,
                record.failure.message,
                record.failure.hint,
            )
        raise IfcConsoleError(
            "JOB_CANCELLED",
            f"{record.kind} job {record.job_id} did not complete",
            "Inspect the job record and submit a fresh transaction if appropriate.",
        )

    async def validate_ids(self, ids_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return await self._data("validate_ids", ids_path=str(ids_path), **kwargs)

    async def clashes(self, set_a: str, set_b: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._data("detect_clashes", set_a=set_a, set_b=set_b, **kwargs)

    async def georeferencing(self) -> dict[str, Any]:
        return await self._data("get_georeferencing")

    async def schema_docs(self, **kwargs: Any) -> dict[str, Any]:
        return await self._data("get_schema_docs", **kwargs)

    # -- talking to a model ---------------------------------------------------
    async def ask(
        self,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        system: str | None = None,
        tools: bool = True,
        history: list[dict[str, Any]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """Run one question through an LLM that can call the ifc-console tools.

        Provider neutral: OpenAI, Anthropic, OpenRouter, or any OpenAI
        compatible server (vLLM, LM Studio, Ollama). The key comes from the
        environment unless you pass one. Returns
        {"text", "tool_calls", "usage", "turns"}; pass on_event to watch the
        stream as it happens.
        """
        from ifc_console.chat.agent import converse

        turns = list(history or []) + [{"role": "user", "text": prompt}]
        parts: list[str] = []
        calls: list[dict[str, Any]] = []
        usage: dict[str, Any] = {}
        error: str | None = None
        async for event in converse(
            self._core,
            turns=turns,
            provider_id=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            system=system,
            use_tools=tools,
            options=options,
        ):
            if on_event is not None:
                on_event(event)
            kind = event.get("type")
            if kind == "content":
                parts.append(event["text"])
            elif kind == "tool_result":
                calls.append(
                    {"name": event["name"], "ok": event["ok"], "summary": event["summary"]}
                )
            elif kind == "usage":
                usage = {"in": event.get("in"), "out": event.get("out")}
            elif kind == "error":
                error = event["text"]
        text = "".join(parts).strip()
        if error and not text:
            raise IfcConsoleError("CHAT_FAILED", error, "Check the provider, model, and key.")
        turns.append({"role": "assistant", "text": text})
        return {"text": text, "tool_calls": calls, "usage": usage, "turns": turns, "error": error}

    # -- knowledge ------------------------------------------------------------
    def build_knowledge(self, *, force: bool = False) -> dict[str, Any]:
        """Build the offline reference index (seconds, no network)."""
        return self._core.knowledge.build(force=force)

    async def ingest_docs(
        self, paths: Sequence[str | Path], *, replace: bool = False
    ) -> dict[str, Any]:
        """Index project documents (md, txt, pdf, images) for retrieval.

        Host-owned: the model can search the result but never ingest. Paths
        must lie inside the allowed directories.
        """
        resolved = [self._core.require_path_allowed(Path(p)) for p in paths]
        return await asyncio.to_thread(
            self._core.project_knowledge.ingest, resolved, replace=replace
        )

    async def search_knowledge(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        data = await self._data("search_ifc_knowledge", query=query, **kwargs)
        hits = list(data.get("hits", []))
        hits.extend(data.get("project_hits", []))
        return hits

    async def knowledge_record(self, key: str) -> dict[str, Any]:
        """Fetch a complete knowledge or recipe record by search-result key."""
        return await self._data("get_knowledge_record", key=key)

    async def api_docs(self, function: str) -> dict[str, Any]:
        return await self._data("get_api_docs", function=function)

    # -- writes ---------------------------------------------------------------
    async def run_code(self, code: str, description: str = "") -> dict[str, Any]:
        return await self._data("execute_ifc_code", code=code, description=description)

    async def export_csv(self, selector: str, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return await self._data("export_csv", selector=selector, path=str(path), **kwargs)

    async def save(self, path: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._data("save_ifc_file", output_path=str(path) if path else None, **kwargs)

    # -- more than one model --------------------------------------------------
    async def attach(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        self._core.add_allowed_dir(target.parent)
        return await self._data("attach", path=str(target), **kwargs)

    async def detach(self, ref: str) -> dict[str, Any]:
        return await self._data("detach", id=ref)

    async def models(self) -> dict[str, Any]:
        return await self._data("list_models")

    async def use(self, model_id: str) -> dict[str, Any]:
        return await self._data("set_active_model", model_id=model_id)

    async def find_files(self, query: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return await self._data("find_files", query=query, **kwargs)


class _Loop:
    """A private event loop on its own thread, so the SDK stays synchronous."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.closed = False
        self._thread = threading.Thread(target=self._run, name="ifc-console-sdk", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @staticmethod
    async def _cancel_and_drain(coro: Coroutine) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task is not current and task.get_coro() is coro
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def run(self, coro: Coroutine, timeout: float | None = _DEFAULT_TIMEOUT) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except FutureTimeoutError:
            future.cancel()
            drain = asyncio.run_coroutine_threadsafe(self._cancel_and_drain(coro), self.loop)
            with suppress(FutureCancelledError, FutureTimeoutError):
                drain.result(timeout=10.0)
            with suppress(FutureCancelledError, FutureTimeoutError):
                future.result(timeout=1.0)
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)
        self.loop.close()


class Workbench:
    """The synchronous face. One model open, the same tools the LLM gets."""

    def __init__(self, workbench: AsyncWorkbench, loop: _Loop) -> None:
        self._wb = workbench
        self._loop = loop

    @classmethod
    def open(
        cls,
        path: str | Path | None = None,
        *,
        mode: str = "ask",
        home: str | Path | None = None,
        allowed_dirs: tuple[str | Path, ...] = (),
        settings: dict[str, Any] | None = None,
        project_dir: str | Path | None = None,
    ) -> Workbench:
        """Open a model (or none) and return a ready workbench."""
        loop = _Loop()
        try:
            workbench = loop.run(
                AsyncWorkbench.create(
                    path,
                    mode=mode,
                    home=home,
                    allowed_dirs=allowed_dirs,
                    settings=settings,
                    project_dir=project_dir,
                )
            )
        except BaseException:
            loop.close()
            raise
        return cls(workbench, loop)

    def __enter__(self) -> Workbench:
        return self

    def __exit__(
        self, exc_type: type | None, exc: BaseException | None, _tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._loop.closed:
            return
        try:
            self._run(lambda: self._wb.aclose())
        finally:
            self._loop.close()

    def _run(
        self,
        factory: Callable[[], Coroutine],
        timeout: float | None | object = _DEFAULT_TIMEOUT_SENTINEL,
    ) -> Any:
        if self._loop.closed:
            raise RuntimeError("this Workbench is closed; open a new one")
        selected_timeout = _DEFAULT_TIMEOUT if timeout is _DEFAULT_TIMEOUT_SENTINEL else timeout
        return self._loop.run(factory(), selected_timeout)

    # -- session --------------------------------------------------------------
    @property
    def core(self) -> Any:
        return self._wb.core

    @property
    def mode(self) -> str:
        return self._wb.mode

    @property
    def context(self) -> WorkspaceContext:
        return self._wb.context

    def set_mode(self, mode: str) -> str:
        return self._wb.set_mode(mode)

    @property
    def settings(self) -> dict[str, Any]:
        return self._wb.settings

    def get_setting(self, key: str) -> Any:
        return self._wb.get_setting(key)

    def configure(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        return self._wb.configure(overrides)

    @property
    def model(self) -> dict[str, Any]:
        return self._wb.model

    def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.open_model(path, **kwargs))

    # -- tools ----------------------------------------------------------------
    def tools(self, *, permitted_only: bool = False) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.tools(permitted_only=permitted_only))

    def operation_definitions(self) -> list[OperationDefinition]:
        return self._wb.operation_definitions()

    def granted_capabilities(self, *, authority: Authority = "tool") -> tuple[Capability, ...]:
        return self._wb.granted_capabilities(authority=authority)

    def capability_decision(
        self,
        required: list[Capability | str] | tuple[Capability | str, ...],
        *,
        authority: Authority = "tool",
    ) -> CapabilityDecision:
        return self._wb.capability_decision(required, authority=authority)

    def verify_audit(self, session_id: str | None = None) -> AuditVerification:
        return self._wb.verify_audit(session_id)

    def call_result(self, name: str, **kwargs: Any) -> Envelope:
        return self._run(lambda: self._wb.call_result(name, **kwargs))

    def call(self, name: str, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.call(name, **kwargs))

    # -- reads ----------------------------------------------------------------
    def info(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.info())

    def status(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.status())

    def orient(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.orient())

    def tree(self, root: str | None = None, depth: int = 10) -> dict[str, Any]:
        return self._run(lambda: self._wb.tree(root, depth))

    def query(self, selector: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.query(selector, **kwargs))

    def search(self, term: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.search(term, **kwargs))

    def search_result(self, term: str, **kwargs: Any) -> SearchElementsData:
        return self._run(lambda: self._wb.search_result(term, **kwargs))

    def query_result(self, selector: str, **kwargs: Any) -> QueryElementsData:
        return self._run(lambda: self._wb.query_result(selector, **kwargs))

    def element(self, global_ids: str | list[str], **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.element(global_ids, **kwargs))

    def psets(self, global_ids: str | list[str], **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.psets(global_ids, **kwargs))

    def quantities(self, selector: str, by: str = "class", **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.quantities(selector, by, **kwargs))

    def validate(self, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.validate(**kwargs))

    def validation_result(self, **kwargs: Any) -> ValidationData:
        return self._run(lambda: self._wb.validation_result(**kwargs))

    # -- durable jobs and artifacts -------------------------------------------
    def submit_validation_job(
        self,
        *,
        model: str | None = None,
        ids_paths: tuple[str | Path, ...] = (),
        express_rules: bool = False,
        max_issues: int = 200,
        expected_revision: str | None = None,
    ) -> JobRecord:
        return self._run(
            lambda: self._wb.submit_validation_job(
                model=model,
                ids_paths=ids_paths,
                express_rules=express_rules,
                max_issues=max_issues,
                expected_revision=expected_revision,
            )
        )

    def submit_commit_job(self, change_set_id: str, *, approval_id: str) -> JobRecord:
        return self._run(lambda: self._wb.submit_commit_job(change_set_id, approval_id=approval_id))

    def submit_restore_job(self, commit_id: str, *, confirm: bool = False) -> JobRecord:
        return self._run(lambda: self._wb.submit_restore_job(commit_id, confirm=confirm))

    def job(self, job_id: str) -> JobRecord:
        return self._wb.job(job_id)

    def jobs(self, *, limit: int = 100) -> list[JobRecord]:
        return self._wb.jobs(limit=limit)

    def wait_job(self, job_id: str, *, timeout: float | None = None) -> JobRecord:
        return self._run(lambda: self._wb.wait_job(job_id, timeout=timeout), timeout=None)

    def watch_job(self, job_id: str, *, poll_interval: float = 0.1) -> Iterator[JobRecord]:
        last_update = None
        while True:
            record = self.job(job_id)
            if record.updated_at != last_update:
                last_update = record.updated_at
                yield record
            if record.state.value in {"succeeded", "failed", "cancelled"}:
                return
            threading.Event().wait(max(0.01, poll_interval))

    def cancel_job(self, job_id: str) -> JobRecord:
        return self._run(lambda: self._wb.cancel_job(job_id))

    def submit_validation_batch(
        self,
        inputs: tuple[str | Path, ...] | list[str | Path],
        *,
        ids_paths: tuple[str | Path, ...] = (),
        express_rules: bool = False,
        max_issues: int = 200,
        concurrency: int = 2,
        failure_policy: str = "continue",
    ) -> BatchRecord:
        return self._run(
            lambda: self._wb.submit_validation_batch(
                inputs,
                ids_paths=ids_paths,
                express_rules=express_rules,
                max_issues=max_issues,
                concurrency=concurrency,
                failure_policy=failure_policy,
            )
        )

    def submit_query_batch(
        self,
        inputs: tuple[str | Path, ...] | list[str | Path],
        *,
        query: str,
        fields: tuple[str, ...] = ("name", "storey", "type_name"),
        order_by: str = "class",
        output_format: str = "jsonl",
        limit: int = 100_000,
        concurrency: int = 2,
        failure_policy: str = "continue",
    ) -> BatchRecord:
        return self._run(
            lambda: self._wb.submit_query_batch(
                inputs,
                query=query,
                fields=fields,
                order_by=order_by,
                output_format=output_format,
                limit=limit,
                concurrency=concurrency,
                failure_policy=failure_policy,
            )
        )

    def batch(self, batch_id: str) -> BatchRecord:
        return self._wb.batch(batch_id)

    def batches(self, *, limit: int = 100) -> list[BatchRecord]:
        return self._wb.batches(limit=limit)

    def wait_batch(self, batch_id: str, *, timeout: float | None = None) -> BatchRecord:
        return self._run(lambda: self._wb.wait_batch(batch_id, timeout=timeout), timeout=None)

    def watch_batch(self, batch_id: str, *, poll_interval: float = 0.1) -> Iterator[BatchRecord]:
        last_update = None
        while True:
            record = self.batch(batch_id)
            if record.updated_at != last_update:
                last_update = record.updated_at
                yield record
            if record.state.value in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return
            threading.Event().wait(max(0.01, poll_interval))

    def cancel_batch(self, batch_id: str) -> BatchRecord:
        return self._run(lambda: self._wb.cancel_batch(batch_id))

    def resume_batch(self, batch_id: str) -> BatchRecord:
        return self._run(lambda: self._wb.resume_batch(batch_id))

    def plan_workflow(self, manifest_path: str | Path) -> WorkflowPlan:
        return self._run(lambda: self._wb.plan_workflow(manifest_path))

    def plan_workflow_spec(self, spec: WorkflowSpec, *, base_dir: str | Path) -> WorkflowPlan:
        return self._run(lambda: self._wb.plan_workflow_spec(spec, base_dir=base_dir))

    def submit_workflow(self, manifest_path: str | Path) -> WorkflowRecord:
        return self._run(lambda: self._wb.submit_workflow(manifest_path))

    def submit_workflow_plan(self, plan: WorkflowPlan) -> WorkflowRecord:
        return self._run(lambda: self._wb.submit_workflow_plan(plan))

    def workflow(self, workflow_id: str) -> WorkflowRecord:
        return self._wb.workflow(workflow_id)

    def workflows(self, *, limit: int = 100) -> list[WorkflowRecord]:
        return self._wb.workflows(limit=limit)

    def wait_workflow(self, workflow_id: str, *, timeout: float | None = None) -> WorkflowRecord:
        return self._run(lambda: self._wb.wait_workflow(workflow_id, timeout=timeout), timeout=None)

    def watch_workflow(
        self, workflow_id: str, *, poll_interval: float = 0.1
    ) -> Iterator[WorkflowRecord]:
        last_update = None
        while True:
            record = self.workflow(workflow_id)
            if record.updated_at != last_update:
                last_update = record.updated_at
                yield record
            if record.state.value in {
                "succeeded",
                "partial",
                "failed",
                "cancelled",
                "interrupted",
            }:
                return
            threading.Event().wait(max(0.01, poll_interval))

    def cancel_workflow(self, workflow_id: str) -> WorkflowRecord:
        return self._run(lambda: self._wb.cancel_workflow(workflow_id))

    def resume_workflow(self, workflow_id: str) -> WorkflowRecord:
        return self._run(lambda: self._wb.resume_workflow(workflow_id))

    def artifact(self, artifact_id: str) -> ArtifactRef:
        return self._wb.artifact(artifact_id)

    def artifacts(self, *, limit: int = 100) -> list[ArtifactRef]:
        return self._wb.artifacts(limit=limit)

    def transaction_journal(self, transaction_id: str) -> TransactionJournal:
        return self._wb.transaction_journal(transaction_id)

    def transaction_journals(self) -> list[TransactionJournal]:
        return self._wb.transaction_journals()

    def read_artifact(self, artifact_id: str) -> bytes:
        return self._wb.read_artifact(artifact_id)

    def read_artifact_text(self, artifact_id: str) -> str:
        return self._wb.read_artifact_text(artifact_id)

    def export_artifact(
        self, artifact_id: str, path: str | Path, *, overwrite: bool = False
    ) -> Path:
        return self._wb.export_artifact(artifact_id, path, overwrite=overwrite)

    def pin_artifact(self, artifact_id: str) -> ArtifactRef:
        return self._wb.pin_artifact(artifact_id)

    def unpin_artifact(self, artifact_id: str) -> bool:
        return self._wb.unpin_artifact(artifact_id)

    def plan_artifact_gc(self, *, older_than_days: int | None = None) -> ArtifactGCPlan:
        return self._wb.plan_artifact_gc(older_than_days=older_than_days)

    def collect_artifacts(self, plan: ArtifactGCPlan, *, confirm: bool = False) -> ArtifactGCResult:
        return self._wb.collect_artifacts(plan, confirm=confirm)

    # -- safe structured changes --------------------------------------------
    def preview_property_change(
        self,
        global_ids: str | list[str] | tuple[str, ...],
        *,
        pset_name: str,
        property_name: str,
        value: IfcScalar,
        create_missing: bool = False,
        nominal_type: str | None = None,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        return self._run(
            lambda: self._wb.preview_property_change(
                global_ids,
                pset_name=pset_name,
                property_name=property_name,
                value=value,
                create_missing=create_missing,
                nominal_type=nominal_type,
                expected_revision=expected_revision,
            )
        )

    def preview_classification_assignment(
        self,
        global_ids: str | list[str] | tuple[str, ...],
        *,
        classification_name: str,
        identification: str,
        reference_name: str,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        return self._run(
            lambda: self._wb.preview_classification_assignment(
                global_ids,
                classification_name=classification_name,
                identification=identification,
                reference_name=reference_name,
                expected_revision=expected_revision,
            )
        )

    def change_set(self, change_set_id: str) -> ChangeSetRecord:
        return self._wb.change_set(change_set_id)

    def approve_change_set(
        self, change_set_id: str, *, approved_by: str, reason: str = ""
    ) -> ApprovalRecord:
        return self._wb.approve_change_set(change_set_id, approved_by=approved_by, reason=reason)

    def commit_change_set(self, change_set_id: str, *, approval_id: str) -> CommitRecord:
        return self._run(lambda: self._wb.commit_change_set(change_set_id, approval_id=approval_id))

    def commit_record(self, commit_id: str) -> CommitRecord:
        return self._wb.commit_record(commit_id)

    def restore_commit(self, commit_id: str, *, confirm: bool = False) -> RestoreRecord:
        return self._run(lambda: self._wb.restore_commit(commit_id, confirm=confirm))

    def validate_ids(self, ids_path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.validate_ids(ids_path, **kwargs))

    def clashes(self, set_a: str, set_b: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.clashes(set_a, set_b, **kwargs))

    def georeferencing(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.georeferencing())

    def schema_docs(self, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.schema_docs(**kwargs))

    # -- talking to a model ---------------------------------------------------
    def ask(
        self,
        prompt: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        system: str | None = None,
        tools: bool = True,
        history: list[dict[str, Any]] | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        """One question to an LLM that may call the ifc-console tools."""
        return self._run(
            lambda: self._wb.ask(
                prompt,
                provider=provider,
                model=model,
                api_key=api_key,
                base_url=base_url,
                system=system,
                tools=tools,
                history=history,
                on_event=on_event,
                **options,
            ),
            timeout=1800.0,
        )

    # -- knowledge ------------------------------------------------------------
    def build_knowledge(self, *, force: bool = False) -> dict[str, Any]:
        return self._wb.build_knowledge(force=force)

    def ingest_docs(
        self, paths: Sequence[str | Path], *, replace: bool = False
    ) -> dict[str, Any]:
        return self._run(lambda: self._wb.ingest_docs(paths, replace=replace))

    def search_knowledge(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.search_knowledge(query, **kwargs))

    def knowledge_record(self, key: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.knowledge_record(key))

    def api_docs(self, function: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.api_docs(function))

    # -- writes ---------------------------------------------------------------
    def run_code(self, code: str, description: str = "") -> dict[str, Any]:
        return self._run(lambda: self._wb.run_code(code, description))

    def export_csv(self, selector: str, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.export_csv(selector, path, **kwargs))

    def save(self, path: str | Path | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.save(path, **kwargs))

    # -- more than one model --------------------------------------------------
    def attach(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.attach(path, **kwargs))

    def detach(self, ref: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.detach(ref))

    def models(self) -> dict[str, Any]:
        return self._run(lambda: self._wb.models())

    def use(self, model_id: str) -> dict[str, Any]:
        return self._run(lambda: self._wb.use(model_id))

    def find_files(self, query: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.find_files(query, **kwargs))
