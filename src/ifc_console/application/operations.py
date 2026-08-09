"""Operation registration, invocation, and common execution behavior."""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ifc_console.core.capabilities import Authority
from ifc_console.core.context import (
    ModelContext,
    OperationContext,
    WorkspaceContext,
    bind_operation_context,
    current_operation_context,
    new_correlation_id,
)
from ifc_console.core.operations import OperationDefinition, OperationRegistry
from ifc_console.core.results import Envelope, ToolError, err, from_tool_error

if TYPE_CHECKING:
    from ifc_console.app import AppCore

log = logging.getLogger("ifc-console.operations")


def enveloped(core: AppCore, operation_name: str) -> Callable:
    def decorate(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Envelope:
            start = time.perf_counter()
            ok_flag, detail = True, ""
            try:
                return await fn(*args, **kwargs)
            except ToolError as exc:
                ok_flag, detail = False, exc.code
                return from_tool_error(exc, core.session_meta())
            except Exception as exc:
                log.exception("operation %s failed", operation_name)
                ok_flag, detail = False, "INTERNAL_ERROR"
                return err(
                    "INTERNAL_ERROR",
                    f"{type(exc).__name__}: {exc}",
                    "This is an ifc-console bug; the audit log has details.",
                    core.session_meta(),
                )
            finally:
                core.tool_event(
                    operation_name,
                    ok=ok_flag,
                    duration_ms=int((time.perf_counter() - start) * 1000),
                    detail=detail,
                )

        return wrapper

    return decorate


class OperationService:
    def __init__(self, core: AppCore, registry: OperationRegistry) -> None:
        self.core = core
        self.registry = registry

    def definitions(self) -> list[OperationDefinition]:
        return self.registry.definitions()

    def workspace_context(self) -> WorkspaceContext:
        session = self.core.session
        revision_id = None
        if session.loaded:
            revision_id = f"{session.fingerprint}:{session.revision}"
        model = ModelContext(
            model_id=getattr(session, "model_id", None),
            revision_id=revision_id,
            name=session.name,
            schema_name=session.schema,
            dirty=session.dirty,
            content_sha256=session.source_sha256,
        )
        return WorkspaceContext(
            workspace_id=self.core.workspace_id,
            active_model=model,
            attached_model_ids=tuple(self.core.models.attached_ids),
        )

    def context(
        self,
        *,
        operation: str | None = None,
        actor: str | None = None,
        client: str | None = None,
        authority: Authority | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        job_id: str | None = None,
    ) -> OperationContext:
        workspace = self.workspace_context()
        parent = current_operation_context()
        return OperationContext(
            correlation_id=correlation_id
            or (parent.correlation_id if parent else new_correlation_id()),
            workspace_id=workspace.workspace_id,
            transport=self.core.transport,
            mode=self.core.policy.mode.value,
            model_id=workspace.active_model.model_id,
            revision_id=workspace.active_model.revision_id,
            actor=actor or (parent.actor if parent else "local-user"),
            client=client or (parent.client if parent else self.core.transport),
            authority=authority or (parent.authority if parent else "tool"),
            operation=operation or (parent.operation if parent else None),
            request_id=request_id or (parent.request_id if parent else None),
            job_id=job_id or (parent.job_id if parent else None),
        )

    @contextmanager
    def invocation(
        self,
        operation: str,
        *,
        actor: str | None = None,
        client: str | None = None,
        authority: Authority | None = None,
        request_id: str | None = None,
        correlation_id: str | None = None,
        job_id: str | None = None,
    ) -> Iterator[OperationContext]:
        context = self.context(
            operation=operation,
            actor=actor,
            client=client,
            authority=authority,
            request_id=request_id,
            correlation_id=correlation_id,
            job_id=job_id,
        )
        with bind_operation_context(context):
            yield context

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        with self.invocation(name) as invocation:
            correlation_meta = {"correlation_id": invocation.correlation_id}
            if invocation.request_id:
                correlation_meta["request_id"] = invocation.request_id
            error_meta = {**self.core.session_meta(), **correlation_meta}
            spec = self.registry.get(name)
            if spec is None:
                known = ", ".join(sorted(self.registry.handlers))
                self.core.audit.record("operation_rejected", outcome="not_found")
                return err(
                    "NOT_FOUND",
                    f"no operation named {name!r}",
                    f"Operations: {known}",
                    error_meta,
                )
            decision = self.core.policy.evaluate(
                list(spec.required_capabilities), authority=invocation.authority
            )
            self.core.audit.record(
                "policy_decision",
                allowed=decision.allowed,
                profile=decision.profile,
                required=[item.value for item in decision.required],
                missing=[item.value for item in decision.missing],
                rule=decision.rule,
            )
            if not decision.allowed:
                try:
                    self.core.policy.require(
                        list(spec.required_capabilities),
                        authority=invocation.authority,
                        action=name,
                    )
                except ToolError as exc:
                    self.core.tool_event(name, ok=False, duration_ms=0, detail=exc.code)
                    return from_tool_error(exc, error_meta)
            try:
                validated = spec.validate_arguments(arguments)
            except ValidationError as exc:
                self.core.audit.record("operation_rejected", outcome="invalid_input")
                return err(
                    "INVALID_INPUT",
                    f"arguments for {name} are invalid: {exc.errors(include_url=False)}",
                    f"Use the input schema for {name}.",
                    error_meta,
                )
            result = await spec.handler(**validated)
            if (
                spec.data_model is not None
                and isinstance(result, Envelope)
                and result.ok
                and not result.meta.get("truncated")
            ):
                try:
                    spec.data_model.model_validate(result.data or {})
                except ValidationError as exc:
                    log.error("operation %s violated its data contract: %s", name, exc)
                    return err(
                        "INTERNAL_ERROR",
                        f"operation {name} returned data outside its declared contract",
                        "This is an ifc-console bug; the audit log has details.",
                        {**self.core.session_meta(), **correlation_meta},
                    )
            if isinstance(result, Envelope):
                result = result.model_copy(
                    update={"meta": {**result.meta, **correlation_meta}}
                )
            return result


def register_viewer_operations(core: AppCore) -> None:
    from ifc_console.mcp import tools_viewer

    if all(name in core.operations for name in tools_viewer.TOOL_NAMES):
        return
    tools_viewer.register(core.operations, core)


def build_operations(core: AppCore) -> OperationService:
    if core._operations_registered:
        return core.operation_service

    from ifc_console.mcp import (
        tools_analysis,
        tools_exec,
        tools_files,
        tools_knowledge,
        tools_query,
        tools_workspace,
    )
    from ifc_console.operations import changes, jobs

    registry = core.operations
    tools_query.register(registry, core)
    tools_knowledge.register(registry, core)
    tools_analysis.register(registry, core)
    tools_exec.register(registry, core)
    tools_files.register(registry, core)
    tools_workspace.register(registry, core)
    jobs.register(registry, core)
    changes.register(registry, core)
    core.plugins.load_configured(core, registry)
    core._operations_registered = True
    core._sync_viewer_tools()
    return core.operation_service
