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
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any

from ifc_console.core.artifacts import ArtifactGCPlan, ArtifactGCResult, ArtifactRef
from ifc_console.core.changes import (
    ApprovalRecord,
    ChangeSetRecord,
    CommitRecord,
    IfcScalar,
    RestoreRecord,
)
from ifc_console.core.context import WorkspaceContext
from ifc_console.core.jobs import JobRecord
from ifc_console.core.operation_data import QueryElementsData, ValidationData
from ifc_console.core.operations import OperationDefinition
from ifc_console.core.results import Envelope, ToolError

__all__ = [
    "AsyncWorkbench",
    "ApprovalRecord",
    "ArtifactRef",
    "ArtifactGCPlan",
    "ArtifactGCResult",
    "ChangeSetRecord",
    "CommitRecord",
    "IfcConsoleError",
    "JobRecord",
    "OperationDefinition",
    "QueryElementsData",
    "RestoreRecord",
    "ValidationData",
    "Workbench",
    "WorkspaceContext",
]

_DEFAULT_TIMEOUT = 600.0


class IfcConsoleError(RuntimeError):
    """A tool refused the call. Carries the same code and hint the LLM sees."""

    def __init__(self, code: str, message: str, hint: str = "") -> None:
        super().__init__(f"{code}: {message}" + (f" ({hint})" if hint else ""))
        self.code = code
        self.message = message
        self.hint = hint


def _sdk_error(exc: ToolError) -> IfcConsoleError:
    return IfcConsoleError(exc.code, exc.message, exc.hint)


def _apply_settings(store: Any, overrides: dict[str, Any]) -> None:
    """In-memory settings for this workbench only; the user's file is untouched."""
    for dotted, value in overrides.items():
        target = store.settings
        *parents, leaf = dotted.split(".")
        for part in (*parents, leaf):
            if not hasattr(target, part):
                raise IfcConsoleError(
                    "INVALID_INPUT", f"unknown setting {dotted!r}", "See docs/settings.md."
                )
            if part != leaf:
                target = getattr(target, part)
        setattr(target, leaf, value)


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
    ) -> AsyncWorkbench:
        from ifc_console.app import AppCore
        from ifc_console.application.operations import build_operations
        from ifc_console.policy.modes import Mode
        from ifc_console.settings import SettingsStore

        store = SettingsStore(home=Path(home) if home else None)
        _apply_settings(store, settings or {})
        core = AppCore(store, mode=Mode(mode), transport="sdk")
        core.start_audit()
        for directory in allowed_dirs:
            core.add_allowed_dir(Path(directory))
        workbench = cls(core)
        build_operations(core)
        if path is not None:
            await workbench.open_model(path)
        return workbench

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

        self._core.set_mode(Mode(mode), by="sdk")
        return self.mode

    async def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        target = Path(path).expanduser().resolve()
        self._core.add_allowed_dir(target.parent)
        await self._core.open_model(target, **kwargs)
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
        self._core.shutdown()

    # -- the tool surface -----------------------------------------------------
    async def tools(self) -> list[dict[str, Any]]:
        """Every tool as a provider-neutral JSON Schema definition."""
        return [
            {
                "name": definition.name,
                "description": definition.description,
                "input_schema": definition.input_schema,
                "output_schema": definition.output_schema,
                "data_schema": definition.data_schema,
                "annotations": definition.annotations.model_dump(exclude_none=True),
            }
            for definition in self.operation_definitions()
        ]

    def operation_definitions(self) -> list[OperationDefinition]:
        """Typed definitions for embedding IFC operations in another client."""
        return self._core.operation_service.definitions()

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
            raise IfcConsoleError(
                "INVALID_OUTPUT",
                f"operation {name!r} returns transport content, not an envelope",
                "Call it through its supported transport.",
            )
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

    def artifact(self, artifact_id: str) -> ArtifactRef:
        try:
            return self._core.artifacts.get(artifact_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def artifacts(self, *, limit: int = 100) -> list[ArtifactRef]:
        return self._core.artifacts.list(limit=limit)

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

    def collect_artifacts(
        self, plan: ArtifactGCPlan, *, confirm: bool = False
    ) -> ArtifactGCResult:
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
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        ids = (global_ids,) if isinstance(global_ids, str) else tuple(global_ids)
        try:
            return await self._core.transactions.preview_property_value(
                global_ids=ids,
                pset_name=pset_name,
                property_name=property_name,
                value=value,
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
        try:
            return await self._core.transactions.commit(change_set_id, approval_id=approval_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    def commit_record(self, commit_id: str) -> CommitRecord:
        try:
            return self._core.transactions.get_commit(commit_id)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

    async def restore_commit(self, commit_id: str, *, confirm: bool = False) -> RestoreRecord:
        try:
            return await self._core.transactions.restore(commit_id, confirm=confirm)
        except ToolError as exc:
            raise _sdk_error(exc) from exc

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

    async def search_knowledge(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        data = await self._data("search_ifc_knowledge", query=query, **kwargs)
        return data.get("hits", [])

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

    def run(self, coro: Coroutine, timeout: float | None = _DEFAULT_TIMEOUT) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

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
                )
            )
        except BaseException:
            loop.close()
            raise
        return cls(workbench, loop)

    def __enter__(self) -> Workbench:
        return self

    def __exit__(
        self, exc_type: type | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._wb.close()
        finally:
            self._loop.close()

    def _run(self, factory: Callable[[], Coroutine], timeout: float | None = None) -> Any:
        if self._loop.closed:
            raise RuntimeError("this Workbench is closed; open a new one")
        return self._loop.run(factory(), timeout or _DEFAULT_TIMEOUT)

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
    def model(self) -> dict[str, Any]:
        return self._wb.model

    def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        return self._run(lambda: self._wb.open_model(path, **kwargs))

    # -- tools ----------------------------------------------------------------
    def tools(self) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.tools())

    def operation_definitions(self) -> list[OperationDefinition]:
        return self._wb.operation_definitions()

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
    def submit_validation_job(self, **kwargs: Any) -> JobRecord:
        return self._run(lambda: self._wb.submit_validation_job(**kwargs))

    def job(self, job_id: str) -> JobRecord:
        return self._wb.job(job_id)

    def jobs(self, *, limit: int = 100) -> list[JobRecord]:
        return self._wb.jobs(limit=limit)

    def wait_job(self, job_id: str, *, timeout: float | None = None) -> JobRecord:
        return self._run(lambda: self._wb.wait_job(job_id, timeout=timeout))

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

    def artifact(self, artifact_id: str) -> ArtifactRef:
        return self._wb.artifact(artifact_id)

    def artifacts(self, *, limit: int = 100) -> list[ArtifactRef]:
        return self._wb.artifacts(limit=limit)

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

    def collect_artifacts(
        self, plan: ArtifactGCPlan, *, confirm: bool = False
    ) -> ArtifactGCResult:
        return self._wb.collect_artifacts(plan, confirm=confirm)

    # -- safe structured changes --------------------------------------------
    def preview_property_change(
        self,
        global_ids: str | list[str] | tuple[str, ...],
        *,
        pset_name: str,
        property_name: str,
        value: IfcScalar,
        expected_revision: str | None = None,
    ) -> ChangeSetRecord:
        return self._run(
            lambda: self._wb.preview_property_change(
                global_ids,
                pset_name=pset_name,
                property_name=property_name,
                value=value,
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
    def ask(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """One question to an LLM that may call the ifc-console tools."""
        return self._run(lambda: self._wb.ask(prompt, **kwargs), timeout=1800.0)

    # -- knowledge ------------------------------------------------------------
    def build_knowledge(self, *, force: bool = False) -> dict[str, Any]:
        return self._wb.build_knowledge(force=force)

    def search_knowledge(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._run(lambda: self._wb.search_knowledge(query, **kwargs))

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
