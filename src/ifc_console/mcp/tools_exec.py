"""execute_ifc_code, the power tool, gated per call by classifier + mode.

Two execution paths. Eligible non-mutating code goes to the sandbox worker, a
separate process with no network, no subprocesses, or inherited credential
environment. Auto mode can report and use guarded in-process fallback; strict
mode refuses it. Mutating code always runs in-process because the edit has to
land in the live model and already required edit mode.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ifc_console.application.operations import enveloped
from ifc_console.core.operations import OperationAnnotations as ToolAnnotations
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError, ok
from ifc_console.mcp.target_context import TargetContext, execution_context, require_target_context
from ifc_console.policy.classify import classify
from ifc_console.policy.guards import (
    GuardError,
    build_namespace,
    entity_mutation_lock,
    exec_environment,
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
    from ifc_console.session.model import MutationOutcome

EXEC_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=True)

# The static half of the description. The environment half is resolved at
# registration, so a model reads what this installation actually has instead
# of writing a script against a library that is not here.
_DESCRIPTION_HEAD = "[EDIT-capable] Run Python on the loaded IFC (IfcOpenShell pre-imported). "

_DESCRIPTION_TAIL = (
    "stdout is captured; the last bare expression is returned. "
    "Ask mode (the default) blocks mutating code: show it to the user or have "
    "them switch to edit mode, which runs it. "
    "Pass a one-line `description`; the user sees it. "
    "Pass the analysis `target_context` as `expected_context` to reject stale "
    "context before code runs (it does not restrict which objects your code "
    "touches). "
    "An edit is one undo step and code that raises is rolled back whole. "
    "AI saving is off by default: the user runs /save or /reload; if enabled, "
    "finish batches with save_ifc_file. "
    "Read-only runs use an isolated sandbox with no network."
)


def build_description(core: AppCore) -> str:
    """The tool description, with this installation's libraries spelled out.

    An import that is going to be refused should cost nothing: the model is
    told what it may use before it writes the first line, not after a whole
    script comes back as an ImportError.
    """
    settings = core.settings.exec
    environment = exec_environment(
        policy=settings.import_policy,
        extra_import_roots=tuple(settings.import_roots_extra),
    )
    injected = ", ".join(
        f"`{name}` ({what})" for name, what in environment["injected"].items()
    )
    installed = "; ".join(
        f"{label}: {', '.join(names)}" for label, names in environment["installed"].items()
    )
    blocked = ", ".join(environment["blocked"])
    reach = (
        "Any other installed package imports too."
        if environment["policy"] == "open"
        else "Nothing outside that list may be imported."
    )
    return (
        f"{_DESCRIPTION_HEAD}Injected (no import): {injected}. "
        f"Installed: {installed}. {reach} Blocked: {blocked} and anything else "
        f"reaching {environment['blocked_reason']}. No bpy. "
        "open() is read-only, allowed directories only; write IFC with "
        f"save_ifc_file. {_DESCRIPTION_TAIL}"
    )


# Sandbox failures that mean "the worker could not serve this run" rather
# than "the code was wrong": auto falls back, strict refuses.
_UNAVAILABLE_KINDS = frozenset({"worker", "protocol"})
_MAX_CODE_CHARS = 1_000_000


def register(mcp: OperationRegistry, core: AppCore) -> None:
    settings = core.settings

    @mcp.tool(annotations=EXEC_ANN, description=build_description(core))
    @enveloped(core, "execute_ifc_code")
    @core.active_model_operation
    async def execute_ifc_code(
        code: Annotated[str, Field(description="Python source (no bpy).")],
        description: Annotated[
            str,
            Field(
                max_length=200,
                description="One-line intent, shown in the user's terminal and audit log.",
            ),
        ] = "",
        expected_context: Annotated[
            TargetContext | None,
            Field(description="Copy target_context from analysis/selection; rejects stale context."),
        ] = None,
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
                "Mutate the model and call save_ifc_file, which writes the working "
                "copy for you."
                if core.policy.allow_copy_save
                else "Keep the changes in memory, then tell the user to run /save or "
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
            if expected_context is not None:
                await session.run(lambda: require_target_context(core, expected_context), timeout=60)
            envelope, sandbox_failure = await _run_sandboxed(
                core, code, cls, description, expected_context=expected_context
            )
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

        async with contextlib.AsyncExitStack() as stack:
            if allow_mutation:
                await stack.enter_async_context(session.edit_lock)
                if core.policy.decide(cls.op_class) is not verdict:
                    raise ToolError(
                        "EXEC_BLOCKED",
                        "the session's policy changed while this call waited for an earlier edit.",
                        "Resubmit the call.",
                    )
            return await _run_in_process(
                core,
                code,
                cls,
                description,
                allow_mutation=allow_mutation,
                allow_system=allow_system,
                expected_context=expected_context,
                # Only worth saying when the sandbox was wanted and could not run;
                # a user who turned it off does not need telling every call.
                fallback_reason=(
                    sandbox_failure
                    or (decision.reason if not decision.use and core.sandbox.enabled else "")
                ),
            )


async def _run_sandboxed(
    core: AppCore,
    code: str,
    cls: Any,
    description: str,
    *,
    expected_context: TargetContext | None = None,
) -> tuple[Envelope | None, str]:
    """Run in the worker.

    Returns (envelope, reason). A None envelope means the caller should fall
    back, and reason says why so the fallback is never silent.
    """
    settings = core.settings
    context = execution_context(core.session, expected_context)
    try:
        result: SandboxResult = await core.sandbox.run(
            code,
            session=core.session,
            output_limit=settings.exec.output_char_limit,
            timeout=settings.exec.timeout_seconds,
            extra_system_modules=tuple(settings.exec.system_modules_extra),
            extra_import_roots=tuple(settings.exec.import_roots_extra),
            import_policy=settings.exec.import_policy,
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
        "input_context": context,
        "target_context": context,
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


def _rolled_back(failure: BaseException | None) -> bool:
    """Whether a failed edit was undone, and the undo checked."""
    outcome = getattr(failure, "mutation_outcome", None)
    return bool(outcome is not None and outcome.rolled_back and outcome.verified)


async def _run_in_process(
    core: AppCore,
    code: str,
    cls: Any,
    description: str,
    *,
    allow_mutation: bool,
    allow_system: bool,
    fallback_reason: str,
    expected_context: TargetContext | None = None,
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
        extra_import_roots=tuple(settings.exec.import_roots_extra),
        import_policy=settings.exec.import_policy,
        deny_dirs=core.generated_code_deny_paths(),
    )

    mutation_started = False
    input_context: dict[str, Any] = {}
    outcome: MutationOutcome | None = None
    # Set when the caller stopped waiting (timeout, cancel) while the worker
    # went on; the worker then announces the finished edit itself.
    abandoned = False
    loop = asyncio.get_running_loop()

    def announce_late() -> None:
        record = outcome.record if outcome is not None else None
        if record is not None:
            core.events.emit(
                "model_mutated",
                tool="execute_ifc_code",
                description=description,
                changes=session.change_count,
                **record.event_fields(),
            )

    def run_code() -> executor.ExecResult:
        with (
            entity_mutation_lock(enabled=not allow_mutation),
            model_write_lock(enabled=not core.policy.allow_ai_save),
        ):
            return executor.run(compiled, namespace, output_limit=settings.exec.output_char_limit)

    def job() -> tuple[executor.ExecResult, int | None, int | None]:
        nonlocal mutation_started, input_context, outcome
        if expected_context is not None:
            require_target_context(core, expected_context)
        input_context = execution_context(session, expected_context)
        pre = session.max_id()
        if allow_mutation:
            # The thread that performs the edit owns the flags. A cancelled or
            # timed-out await unwinds while this thread keeps mutating, and a
            # false clean flag silently discards the edit at the next open. The
            # edit is one transaction: it is kept whole or rolled back whole.
            mutation_started = True
            result, outcome = session.mutate(
                run_code, tool="execute_ifc_code", description=description
            )
            if abandoned:
                loop.call_soon_threadsafe(announce_late)
        else:
            result = run_code()
        post = session.max_id()
        return result, pre, post

    announced = False

    def announce_mutation(failure: BaseException | None = None) -> None:
        """Publish what job() flagged, so live consumers refresh."""
        nonlocal announced
        if not mutation_started or announced:
            return
        announced = True
        if _rolled_back(failure):
            return  # the model is as it was; there is nothing to show
        record = outcome.record if outcome is not None else None
        core.events.emit(
            "model_mutated",
            tool="execute_ifc_code",
            description=description,
            changes=session.change_count,
            **(record.event_fields() if record is not None else {}),
        )

    def failure_data(failure: BaseException | None = None) -> dict[str, Any]:
        if not mutation_started:
            return {}
        rollback = getattr(failure, "mutation_outcome", None)
        if _rolled_back(failure):
            return {
                "rolled_back": True,
                "verified": True,
                "target_context": execution_context(session, expected_context),
            }
        data: dict[str, Any] = {
            "partial_changes_possible": True,
            "input_context": input_context,
            "target_context": execution_context(session, expected_context),
        }
        if rollback is not None:
            data.update(rolled_back=rollback.rolled_back, verified=rollback.verified)
        return data

    # Generating geometry takes longer than answering a question, and a
    # mutating run that times out costs the user their changes, so an edit is
    # given the larger budget.
    budget = (
        settings.exec.edit_timeout_seconds if allow_mutation else settings.exec.timeout_seconds
    )
    start = time.perf_counter()
    try:
        result, pre, post = await session.run(
            job, timeout=budget, timeout_code="EXEC_TIMEOUT"
        )
    except ToolError as exc:
        # includes EXEC_TIMEOUT, where the worker is still mutating
        abandoned = True
        announce_mutation(exc)
        if mutation_started:
            exc.data = {**(exc.data or {}), **failure_data(exc)}
        raise
    except asyncio.CancelledError:
        # a BaseException, so the handlers below never see it
        abandoned = True
        announce_mutation()
        raise
    except GuardError as exc:
        announce_mutation(exc)
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
            data=failure_data(exc) or None,
        ) from exc
    except Exception as exc:
        announce_mutation(exc)
        core.audit.record("exec", ok=False, op_class=cls.op_class.value, error=repr(exc), code=code)
        if _rolled_back(exc):
            hint = (
                "The run was rolled back, so the model is unchanged. Read the "
                "traceback in data, fix the code, and resubmit."
            )
        elif mutation_started:
            hint = (
                "Read the traceback in data. Changes may be partial: read back the "
                "intended objects and retry only unfinished edits."
            )
        else:
            hint = "Read the traceback in data, fix the code, and resubmit."
        raise ToolError(
            "EXEC_ERROR",
            f"{type(exc).__name__}: {exc}",
            hint,
            data={"traceback": executor.format_traceback(exc), **failure_data(exc)},
        ) from exc
    duration_ms = int((time.perf_counter() - start) * 1000)

    mutated = False
    record = outcome.record if outcome is not None else None
    if allow_mutation:
        unrecorded = (
            record is None and pre is not None and post is not None and post > pre
        )
        if unrecorded:
            # Entities the transaction log never saw (a low-level call): the
            # edit stays, but nothing here can undo it.
            session.mark_dirty()
            session.record_change(description, tool="execute_ifc_code")
        mutated = record is not None or unrecorded
        if mutated:
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
        "input_context": input_context,
        "target_context": execution_context(session, expected_context),
    }
    if record is not None:
        data["change"] = record.to_dict(detail=True)
    if allow_mutation and not mutated:
        data["note"] = "the code ran but changed nothing in the model"
    elif mutated:
        copy = session.working_copy
        if copy is not None:
            data["note"] = (
                f"the viewer already shows this change; {session.change_count} change(s) "
                f"are in memory. call save_ifc_file to write the working copy "
                f"({copy.path.name}); {copy.origin.name} is never touched"
            )
        elif core.policy.allow_ai_save:
            data["note"] = "model is dirty; call save_ifc_file when the batch is done"
        else:
            data["note"] = (
                "model is dirty and the viewer already shows it; only the user can "
                "persist it with /save or discard it with /reload"
            )
    elif fallback_reason:
        data["note"] = f"ran with in-process guards instead of the sandbox: {fallback_reason}"
    return ok(data, core.session_meta(), char_limit=settings.exec.output_char_limit)
