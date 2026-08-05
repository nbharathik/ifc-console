"""Workspace tools: find files in the allowed folders, attach extra models and
companion files, and move the active model.

One active model stays the default. These tools make the second mode
reachable: they never guess which file you meant, and only the active model
is ever writable.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ifc_console.mcp.compat import MCPServer, ToolAnnotations
from ifc_console.mcp.envelope import Envelope, ToolError, ok
from ifc_console.mcp.server import enveloped
from ifc_console.workspace.kinds import KINDS, detect_kind

if TYPE_CHECKING:
    from ifc_console.app import AppCore

QUERY_ANN = ToolAnnotations(readOnlyHint=True, destructiveHint=False)
SESSION_ANN = ToolAnnotations(readOnlyHint=False, destructiveHint=False)

_KIND_NAMES = tuple(k.name for k in KINDS)

TOOL_NAMES = ("find_files", "list_models", "attach", "detach", "set_active_model")


async def _ensure_index(core: AppCore) -> None:
    """Scan on first use and after a new root appears; off the event loop."""
    if core.workspace.scanned_at is None:
        await asyncio.to_thread(core.workspace.scan)


def register(mcp: MCPServer, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Search the allowed folders for BIM files of any supported "
            f"kind ({', '.join(_KIND_NAMES)}) and return ranked candidates with "
            "path, kind, size, IFC schema or IDS spec count, and a guessed "
            "discipline. Opens nothing: pass a returned path to open_ifc_file or "
            "attach. Omit query to list what is there. If meta.ambiguous is "
            "true, ask the user which file they meant instead of picking one."
        ),
    )
    @enveloped(core, "find_files")
    async def find_files(
        query: Annotated[
            str | None,
            Field(description="Free text, e.g. 'architecture', 'ids', 'rev3'. Omit to list."),
        ] = None,
        kinds: Annotated[
            list[str] | None,
            Field(description=f"Restrict to these kinds. Allowed: {list(_KIND_NAMES)}."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=200)] = 30,
        refresh: Annotated[bool, Field(description="Re-walk the folders first.")] = False,
    ) -> Envelope:
        if kinds:
            unknown = [k for k in kinds if k not in _KIND_NAMES]
            if unknown:
                raise ToolError(
                    "INVALID_INPUT",
                    f"unknown kind(s): {unknown}",
                    f"Allowed kinds: {list(_KIND_NAMES)}.",
                )
        if refresh:
            await asyncio.to_thread(core.workspace.scan)
        else:
            await _ensure_index(core)
        entries, ambiguous = core.workspace.find(query, kinds, limit)
        stats = core.workspace.stats()
        data: dict[str, Any] = {
            "files": [e.to_dict() for e in entries],
            "workspace": stats,
        }
        if ambiguous:
            data["hint"] = (
                "several files match about equally well; ask the user which one "
                "before opening anything"
            )
        return ok(
            data,
            core.session_meta(),
            char_limit=limit_,
            returned=len(entries),
            indexed=stats["files"],
            ambiguous=ambiguous,
        )

    @mcp.tool(
        annotations=QUERY_ANN,
        description=(
            "[QUERY] Every model held in this session and every attached "
            "companion file. Shows which model is active (the only writable "
            "one), which are attached read-only, their dirty flags, and the "
            "memory budget. Use the model_id values with set_active_model, "
            "detach, or the `model` parameter of the read tools."
        ),
    )
    @enveloped(core, "list_models")
    async def list_models() -> Envelope:
        registry = core.models
        data = {
            "active": registry.active_id,
            "models": registry.model_rows(),
            "attachments": [a.to_dict() for a in registry.attachments.values()],
            "budget": {
                "resident": len(registry.sessions),
                "max_resident": registry.max_resident,
                "resident_mb": round(registry.resident_bytes() / 1_048_576, 1),
                "max_total_mb": registry.max_total_mb,
            },
            "note": (
                "one active model is the norm; attached models are read-only "
                "until set_active_model moves the focus"
            ),
        }
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SESSION_ANN,
        description=(
            "[SESSION] Attach a file alongside the active model, without "
            "replacing it. An IFC file is loaded read-only as an extra model "
            "(use list_models for its model_id); an IDS, BCF, or CSV is "
            "registered as a companion file so its path is available to the "
            "tools that read it, for example validate_ids. Allowed in ask mode: "
            "attaching reads, it never changes a model. To switch the active "
            "model instead, use open_ifc_file or set_active_model."
        ),
    )
    @enveloped(core, "attach")
    async def attach(
        path: Annotated[str, Field(description="Path to the file; see find_files.")],
        alias: Annotated[
            str | None, Field(description="Short name to refer to it by; defaults to the stem.")
        ] = None,
    ) -> Envelope:
        target = core.require_path_allowed(Path(path))
        if not target.is_file():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{target} does not exist.",
                "Call find_files to see what the workspace holds.",
            )
        kind_name = await asyncio.to_thread(detect_kind, target)
        if kind_name == "ifc":
            # "auto" resolves under the model lock: with nothing loaded this
            # just opens the model, otherwise it attaches alongside.
            model_id = await core.open_model(target, attach="auto", alias=alias)
            session = core.models.require(model_id)
            data: dict[str, Any] = {
                "attached": "model",
                "model_id": model_id,
                "active": model_id == core.models.active_id,
                "path": str(session.path),
                "name": session.name,
                "schema": session.schema,
                "writable": not session.read_only,
            }
            return ok(data, core.session_meta(), char_limit=limit_)
        attachment = await core.attach_file(target, alias)
        data = {"attached": "file", **attachment.to_dict()}
        if attachment.consumed_by:
            data["next"] = f"call {attachment.consumed_by} with this path"
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=SESSION_ANN,
        description=(
            "[SESSION] Release an attached model (by model_id) or companion file "
            "(by alias). Refuses to drop a model with unsaved changes. The "
            "active model cannot be detached while others are attached; switch "
            "with set_active_model first."
        ),
    )
    @enveloped(core, "detach")
    async def detach(
        id: Annotated[str, Field(description="A model_id or an attachment alias.")],
    ) -> Envelope:
        if id in core.models.attachments:
            attachment = core.detach_file(id)
            return ok(
                {"detached": "file", "alias": id, "path": str(attachment.path)},
                core.session_meta(),
                char_limit=limit_,
            )
        session = core.models.get(id)
        if session is None:
            known = sorted([*core.models.sessions, *core.models.attachments])
            raise ToolError(
                "NOT_FOUND",
                f"nothing attached with id {id!r}.",
                f"Known ids: {known or '(none)'}. Call list_models.",
            )
        await core.detach_model(id)
        return ok(
            {"detached": "model", "model_id": id, "active": core.models.active_id},
            core.session_meta(),
            char_limit=limit_,
        )

    @mcp.tool(
        annotations=SESSION_ANN,
        description=(
            "[SESSION] Make an already-resident model the active one, so it "
            "becomes the model every tool reads by default and the only "
            "writable one. The model that was active stays resident, read-only, "
            "and keeps any unsaved changes."
        ),
    )
    @enveloped(core, "set_active_model")
    async def set_active_model(
        model_id: Annotated[str, Field(description="A model_id from list_models.")],
    ) -> Envelope:
        previous = core.models.active_id
        session = await core.set_active_model(model_id)
        data = {
            "active": model_id,
            "previous": previous,
            "name": session.name,
            "schema": session.schema,
            "path": str(session.path),
        }
        if previous is not None and previous in core.models.sessions:
            data["previous_dirty"] = core.models.sessions[previous].dirty
        return ok(data, core.session_meta(), char_limit=limit_)
