"""execute_ifc_code, the power tool, gated per call by classifier + mode.

Two execution paths. Eligible non-mutating code goes to the sandbox worker, a
separate process with no network, no subprocesses, or inherited credential
environment. Auto mode can report and use guarded in-process fallback; strict
mode refuses it. Mutating code always runs in-process because the edit has to
land in the live model and already required edit mode.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.policy.classify import classify
from ifc_console.policy.guards import (
    GuardError,
    build_namespace,
    entity_mutation_lock,
    model_write_lock,
)
from ifc_console.policy.modes import OpClass, Verdict
from ifc_console.sandbox import (
    SandboxError,
    SandboxNotReady,
    SandboxResult,
    SandboxTimeout,
)
from ifc_console.session import executor

if TYPE_CHECKING:
    from ifc_console.app import AppCore

EXEC_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

_DESCRIPTION = (
    "[EDIT-capable] Run Python against the loaded IFC with IfcOpenShell "
    "pre-imported. Pre-injected: `ifc` (the loaded file), `ifcopenshell`, "
    "`ifc_api` (ifcopenshell.api), `element_util` (ifcopenshell.util.element), "
    "`selector_util` (ifcopenshell.util.selector), `unit_util`, `query(sel)` "
    "(selector shortcut), `get_ifc_file()`. stdout is captured; the value of a "
    "final bare expression is returned like a REPL. The session mode gates "
    "mutation: in ask mode (the default), code that would mutate the model is "
    "rejected with an error; generate and show code to the user instead, or "
    "ask them to switch to edit mode. In edit mode mutations run. Include a "
    "one-line `description` of intent; the user sees it in their terminal and "
    "audit log. After mutating, the model is dirty. AI saving is disabled by "
    "default, so the user reviews and runs /save or /reload; when explicitly "
    "enabled, finish batches with save_ifc_file. Eligible read-only runs use "
    "an isolated sandbox with no "
    "network and no file access outside the model directories. Auto mode can "
    "report and use guarded in-process fallback; strict mode refuses it. Do not "
    "import os/subprocess/network modules; that class of code is blocked. This "
    "is not Blender; there is no bpy."
)

# Sandbox failures that mean "the worker could not serve this run" rather
# than "the code was wrong": auto falls back, strict refuses.
_UNAVAILABLE_KINDS = frozenset({"worker", "protocol"})
_MAX_CODE_CHARS = 1_000_000


def register(mcp: OperationRegistry, core: AppCore) -> None:
    settings = core.settings

    @mcp.tool(annotations=EXEC_ANN, description=_DESCRIPTION)
    @enveloped(core, "execute_ifc_code")
    @core.active_model_operation
    async def execute_ifc_code(
        code: Annotated[str, Field(description="Python source. No bpy; this is not Blender.")],
        description: Annotated[
            str,
            Field(
                max_length=200,
                description="One-line intent, shown in the user's terminal and audit log.",
            ),
        ] = "",
    ) -> Envelope:
        session = core.session

        if len(code) > _MAX_CODE_CHARS:
            raise ToolError(
                "INVALID_INPUT",
                f"code exceeds the {_MAX_CODE_CHARS:,} character limit.",
                "Submit a smaller program or move reusable logic into a trusted plugin.",
            )

        try:
            cls = classify(code, extra_system_modules=tuple(settings.exec.system_modules_extra))
        except SyntaxError as exc:
            raise ToolError(
                "EXEC_ERROR",
                f"syntax error: {exc}",
                "Fix the Python syntax and resubmit.",
            ) from exc

        verdict = core.policy.decide(cls.op_class)
        reasons = "; ".join(cls.reasons[:4]) or "no mutation indicators"
        if verdict is Verdict.DENY_ASK:
            raise ToolError(
                "ASK_MODE_BLOCKED",
                f"this code was classified as {cls.op_class.value} ({reasons}) but the "
                "session is in ask mode: the AI may query, never change, the model.",
                "Ask the user to run /mode edit in the ifc-console terminal if they "
                "want this change made. Writing code and showing it to the user "
                "is always fine.",
            )
        if cls.model_write and not core.policy.allow_ai_save:
            raise ToolError(
                "AI_SAVE_DISABLED",
                "generated code cannot write an IFC file while files.allow_ai_save is false.",
                "Keep the changes in memory, then tell the user to run /save or "
                "/reload after reviewing them.",
            )
        if verdict is Verdict.DENY_AI_SAVE:
            raise ToolError(
                "AI_SAVE_DISABLED",
                f"SYSTEM-class code ({reasons}) is disabled while AI saving is off; "
                "unrestricted system access could write files.",
                "Keep the operation in memory, then tell the user to run /save or "
                "/reload after reviewing the result.",
            )
        if verdict is Verdict.DENY_SYSTEM:
            raise ToolError(
                "EXEC_BLOCKED",
                f"SYSTEM-class code ({reasons}) is disabled: exec.allow_system_access is false.",
                "Rewrite without OS/network/file access, or ask the user to run "
                "`ifc-console settings set exec.allow_system_access true` and restart.",
            )

        # past the gate: EDIT/SYSTEM only reach here in edit mode
        allow_mutation = cls.op_class in (OpClass.EDIT, OpClass.SYSTEM)
        allow_system = cls.op_class is OpClass.SYSTEM

        decision = core.sandbox.decide(session, mutating=allow_mutation)
        sandbox_failure = ""
        if decision.use:
            envelope, sandbox_failure = await _run_sandboxed(core, code, cls, description)
            if envelope is not None:
                return envelope
        elif core.sandbox.enabled and core.sandbox.strict and not allow_mutation:
            raise ToolError(
                "SANDBOX_UNAVAILABLE",
                f"this run cannot be sandboxed: {decision.reason}.",
                "sandbox.mode is strict, so the run was refused rather than "
                "executed with in-process guards only. Ask the user to resolve the "
                "reason above, or to set sandbox.mode to auto.",
            )

        return await _run_in_process(
            core,
            code,
            cls,
            description,
            allow_mutation=allow_mutation,
            allow_system=allow_system,
            # Only worth saying when the sandbox was wanted and could not run;
            # a user who turned it off does not need telling every call.
            fallback_reason=(
                sandbox_failure
                or (decision.reason if not decision.use and core.sandbox.enabled else "")
            ),
        )


async def _run_sandboxed(
    core: AppCore, code: str, cls: Any, description: str
) -> tuple[Envelope | None, str]:
    """Run in the worker.

    Returns (envelope, reason). A None envelope means the caller should fall
    back, and reason says why so the fallback is never silent.
    """
    settings = core.settings
    try:
        result: SandboxResult = await core.sandbox.run(
            code,
            session=core.session,
            output_limit=settings.exec.output_char_limit,
            timeout=settings.exec.timeout_seconds,
            extra_system_modules=tuple(settings.exec.system_modules_extra),
        )
    except SandboxNotReady as exc:
        # The worker never got as far as running the code, so exec.timeout_seconds
        # is not the limit that was hit.
        if core.sandbox.strict:
            raise ToolError(
                "SANDBOX_UNAVAILABLE",
                f"the sandbox could not be made ready: {exc}",
                "sandbox.mode is strict, so the run was refused. Raise "
                "sandbox.startup_timeout or sandbox.load_timeout, or check "
                "`/sandbox` in the ifc-console terminal.",
            ) from exc
        return None, str(exc)
    except SandboxTimeout:
        core.audit.record(
            "exec",
            ok=False,
            sandboxed=True,
            op_class=cls.op_class.value,
            code=code,
            error="timeout",
        )
        raise ToolError(
            "EXEC_TIMEOUT",
            f"code exceeded the {settings.exec.timeout_seconds:.0f}s execution timeout.",
            "The sandbox process was killed; the session is unaffected and the next "
            "call will work. Narrow the query or raise exec.timeout_seconds.",
        ) from None
    except (SandboxError, OSError) as exc:
        if core.sandbox.strict:
            raise ToolError(
                "SANDBOX_UNAVAILABLE",
                f"the sandbox worker could not run this code: {exc}",
                "sandbox.mode is strict, so the run was refused. Ask the user to "
                "check `/sandbox` in the ifc-console terminal.",
            ) from exc
        return None, str(exc)

    if not result.ok and result.kind in _UNAVAILABLE_KINDS:
        if core.sandbox.strict:
            raise ToolError(
                "SANDBOX_UNAVAILABLE",
                f"the sandbox worker failed: {result.message}",
                "sandbox.mode is strict, so the run was refused. Ask the user to "
                "check `/sandbox` in the ifc-console terminal.",
            )
        return None, result.message

    if not result.ok:
        _raise_sandbox_failure(core, code, cls, result)

    if result.contained:
        # The classifier and the guards both missed a mutation and the sandbox
        # copy absorbed it. Unlike the in-process path, nothing to recover.
        core.audit.record("taint_contained", code=code, op_class=cls.op_class.value)
        core.events.emit("sandbox_contained", tool="execute_ifc_code")

    core.audit.record(
        "exec",
        ok=True,
        sandboxed=True,
        op_class=cls.op_class.value,
        reasons=cls.reasons,
        mutated=False,
        contained=result.contained,
        duration_ms=result.info.get("duration_ms"),
        desc=description,
        code=code,
    )
    data: dict[str, Any] = {
        "stdout": result.stdout,
        "result": result.result_repr,
        "classification": cls.op_class.value,
        "mutated": False,
        "sandboxed": True,
        "duration_ms": result.info.get("duration_ms"),
    }
    if result.contained:
        data["note"] = (
            "this code changed the sandbox's throwaway copy of the model; the "
            "console's model is untouched. Nothing was saved."
        )
    return ok(data, core.session_meta(), char_limit=core.settings.exec.output_char_limit), ""


def _raise_sandbox_failure(core: AppCore, code: str, cls: Any, result: SandboxResult) -> None:
    if result.kind == "syntax":
        raise ToolError("EXEC_ERROR", f"syntax error: {result.message}", "Fix and resubmit.")
    if result.kind in ("guard", "violation"):
        core.audit.record(
            "exec",
            ok=False,
            blocked=True,
            sandboxed=True,
            violation=result.kind == "violation",
            op_class=cls.op_class.value,
            code=code,
        )
        hint = (
            "The sandbox policy blocked this. It has no network, no subprocesses, "
            "and no file access outside the model directories; rewrite the code "
            "without them."
            if result.kind == "violation"
            else "The runtime guard blocked this operation. If the mutation is "
            "intended, ask the user to run /mode edit in the ifc-console "
            "terminal and resubmit."
        )
        raise ToolError("EXEC_BLOCKED", result.message, hint)
    core.audit.record(
        "exec",
        ok=False,
        sandboxed=True,
        op_class=cls.op_class.value,
        error=result.message,
        code=code,
    )
    raise ToolError(
        "EXEC_ERROR",
        f"{result.info.get('type') or 'error'}: {result.message}"
        if result.info.get("type")
        else result.message,
        "Read the traceback in data, fix the code, and resubmit.",
        data={"traceback": result.traceback} if result.traceback else None,
    )


async def _run_in_process(
    core: AppCore,
    code: str,
    cls: Any,
    description: str,
    *,
    allow_mutation: bool,
    allow_system: bool,
    fallback_reason: str,
) -> Envelope:
    """The original path: guarded execution on the model worker thread."""
    settings = core.settings
    session = core.session
    try:
        compiled = executor.prepare(code)
    except SyntaxError as exc:  # already screened; belt and braces
        raise ToolError("EXEC_ERROR", f"syntax error: {exc}", "Fix and resubmit.") from exc

    namespace = build_namespace(
        session.ifc,
        allow_mutation=allow_mutation,
        allow_system=allow_system,
        allowed_dirs=list(core.allowed_dirs),
        extra_system_modules=tuple(settings.exec.system_modules_extra),
        deny_dirs=core.generated_code_deny_paths(),
    )

    def job() -> tuple[executor.ExecResult, int | None, int | None]:
        pre = session.max_id()
        if allow_mutation:
            # The thread that performs the edit owns the flag. A cancelled or
            # timed-out await unwinds while this thread keeps mutating, and a
            # false clean flag silently discards the edit at the next open.
            session.mark_dirty()
        with (
            entity_mutation_lock(enabled=not allow_mutation),
            model_write_lock(enabled=not core.policy.allow_ai_save),
        ):
            result = executor.run(compiled, namespace, output_limit=settings.exec.output_char_limit)
        post = session.max_id()
        return result, pre, post

    def announce_mutation() -> None:
        """Publish what job() already flagged, so live consumers refresh."""
        if allow_mutation and session.dirty:
            core.events.emit("model_mutated", tool="execute_ifc_code")

    start = time.perf_counter()
    try:
        result, pre, post = await session.run(
            job, timeout=settings.exec.timeout_seconds, timeout_code="EXEC_TIMEOUT"
        )
    except ToolError:
        # includes EXEC_TIMEOUT, where the worker is still mutating
        announce_mutation()
        raise
    except asyncio.CancelledError:
        # a BaseException, so the handlers below never see it
        announce_mutation()
        raise
    except GuardError as exc:
        announce_mutation()
        core.audit.record("exec", ok=False, blocked=True, op_class=cls.op_class.value, code=code)
        ai_save_blocked = not core.policy.allow_ai_save and "writing an IFC file" in str(exc)
        raise ToolError(
            "AI_SAVE_DISABLED" if ai_save_blocked else "EXEC_BLOCKED",
            str(exc),
            (
                "Tell the user to run /save to keep the in-memory changes or "
                "/reload to discard them."
                if ai_save_blocked
                else "The runtime guard blocked this operation. If the mutation is "
                "intended, ask the user to run /mode edit in the ifc-console "
                "terminal and resubmit."
            ),
        ) from exc
    except Exception as exc:
        announce_mutation()
        core.audit.record("exec", ok=False, op_class=cls.op_class.value, error=repr(exc), code=code)
        raise ToolError(
            "EXEC_ERROR",
            f"{type(exc).__name__}: {exc}",
            "Read the traceback in data, fix the code, and resubmit.",
            data={"traceback": executor.format_traceback(exc)},
        ) from exc
    duration_ms = int((time.perf_counter() - start) * 1000)

    mutated = False
    if allow_mutation:
        mutated = True
        # Lets live consumers (the web viewer) refresh their copy.
        announce_mutation()
    elif pre is not None and post is not None and post > pre:
        # a guarded run grew the model: classifier false negative
        session.tainted = True
        core.audit.record("taint", pre_max_id=pre, post_max_id=post, code=code)
        core.events.emit("session_tainted", pre=pre, post=post)

    core.audit.record(
        "exec",
        ok=True,
        sandboxed=False,
        op_class=cls.op_class.value,
        reasons=cls.reasons,
        mutated=mutated,
        duration_ms=duration_ms,
        desc=description,
        code=code,
    )

    data: dict[str, Any] = {
        "stdout": result.stdout,
        "result": result.result_repr,
        "classification": cls.op_class.value,
        "mutated": mutated,
        "sandboxed": False,
        "duration_ms": duration_ms,
    }
    if mutated:
        data["note"] = (
            "model is dirty; call save_ifc_file when the batch is done"
            if core.policy.allow_ai_save
            else "model is dirty; only the user can persist it with /save or discard it with /reload"
        )
    elif fallback_reason:
        data["note"] = f"ran with in-process guards instead of the sandbox: {fallback_reason}"
    return ok(data, core.session_meta(), char_limit=settings.exec.output_char_limit)
