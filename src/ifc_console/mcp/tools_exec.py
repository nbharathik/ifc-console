"""execute_ifc_code, the power tool, gated per call by classifier + mode."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

from ifc_console.mcp.compat import MCPServer, ToolAnnotations
from ifc_console.mcp.envelope import Envelope, ToolError, ok
from ifc_console.mcp.server import enveloped
from ifc_console.policy.classify import classify
from ifc_console.policy.guards import GuardError, build_namespace, entity_mutation_lock
from ifc_console.policy.modes import OpClass, Verdict
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
    "audit log. After mutating, the model is dirty: finish batches with "
    "save_ifc_file. Do not import os/subprocess/network modules; that class "
    "of code is blocked. This is not Blender; there is no bpy."
)


def register(mcp: MCPServer, core: AppCore) -> None:
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

        try:
            cls = classify(
                code, extra_system_modules=tuple(settings.exec.system_modules_extra)
            )
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
        if verdict is Verdict.DENY_SYSTEM:
            raise ToolError(
                "EXEC_BLOCKED",
                f"SYSTEM-class code ({reasons}) is disabled: exec.allow_system_access "
                "is false.",
                "Rewrite without OS/network/file access, or ask the user to run "
                "`ifc-console settings set exec.allow_system_access true` and restart.",
            )

        # past the gate: EDIT/SYSTEM only reach here in edit mode
        allow_mutation = cls.op_class in (OpClass.EDIT, OpClass.SYSTEM)
        allow_system = cls.op_class is OpClass.SYSTEM

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
        )

        def job() -> tuple[executor.ExecResult, int | None, int | None]:
            pre = session.max_id()
            with entity_mutation_lock(enabled=not allow_mutation):
                result = executor.run(
                    compiled, namespace, output_limit=settings.exec.output_char_limit
                )
            post = session.max_id()
            return result, pre, post

        start = time.perf_counter()
        try:
            result, pre, post = await session.run(
                job, timeout=settings.exec.timeout_seconds, timeout_code="EXEC_TIMEOUT"
            )
        except ToolError:
            raise
        except GuardError as exc:
            core.audit.record(
                "exec", ok=False, blocked=True, op_class=cls.op_class.value, code=code
            )
            raise ToolError(
                "EXEC_BLOCKED",
                str(exc),
                "The runtime guard blocked this operation. If the mutation is "
                "intended, ask the user to run /mode edit in the ifc-console "
                "terminal and resubmit.",
            ) from exc
        except Exception as exc:
            if allow_mutation:
                # The crashed code may have mutated before raising; a false
                # dirty costs a save prompt, a false clean loses edits.
                session.mark_dirty()
                core.events.emit("model_mutated", tool="execute_ifc_code")
            core.audit.record(
                "exec", ok=False, op_class=cls.op_class.value, error=repr(exc), code=code
            )
            raise ToolError(
                "EXEC_ERROR",
                f"{type(exc).__name__}: {exc}",
                "Read the traceback in data, fix the code, and resubmit.",
                data={"traceback": executor.format_traceback(exc)},
            ) from exc
        duration_ms = int((time.perf_counter() - start) * 1000)

        mutated = False
        if allow_mutation:
            session.mark_dirty()
            mutated = True
            # Lets live consumers (the web viewer) refresh their copy.
            core.events.emit("model_mutated", tool="execute_ifc_code")
        elif pre is not None and post is not None and post > pre:
            # a guarded run grew the model: classifier false negative
            session.tainted = True
            core.audit.record("taint", pre_max_id=pre, post_max_id=post, code=code)
            core.events.emit("session_tainted", pre=pre, post=post)

        core.audit.record(
            "exec",
            ok=True,
            op_class=cls.op_class.value,
            reasons=cls.reasons,
            mutated=mutated,
            duration_ms=duration_ms,
            desc=description,
            code=code,
        )

        data = {
            "stdout": result.stdout,
            "result": result.result_repr,
            "classification": cls.op_class.value,
            "mutated": mutated,
            "duration_ms": duration_ms,
        }
        if mutated:
            data["note"] = "model is dirty; call save_ifc_file when the batch is done"
        return ok(data, core.session_meta(), char_limit=settings.exec.output_char_limit)
