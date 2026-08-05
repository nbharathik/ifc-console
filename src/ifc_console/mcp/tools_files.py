"""File tools: list_ifc_files, open_ifc_file, save_ifc_file."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from ifc_console.mcp.compat import MCPServer, ToolAnnotations
from ifc_console.mcp.envelope import Envelope, ToolError, ok
from ifc_console.mcp.server import enveloped
from ifc_console.policy.modes import Mode

if TYPE_CHECKING:
    from ifc_console.app import AppCore

_IFC_SUFFIXES = (".ifc", ".ifczip", ".ifcxml")
_SCHEMA_RE = re.compile(r"FILE_SCHEMA\s*\(\s*\(\s*'([^']+)'")
# Directory entries examined per root: keeps a recursive scan of a huge tree
# from stalling the tool. /open with a full path always works regardless.
_SCAN_CAP = 10_000


def _peek_schema(path: Path) -> str | None:
    try:
        head = path.read_bytes()[:4096].decode("ascii", errors="ignore")
    except OSError:
        return None
    match = _SCHEMA_RE.search(head)
    return match.group(1) if match else None


def register(mcp: MCPServer, core: AppCore) -> None:
    limit_ = core.settings.exec.output_char_limit

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False),
        description=(
            "[QUERY] IFC files the server may open: recent files plus .ifc/.ifczip/"
            ".ifcxml found in the allowed directories. Returns path, size, modified "
            "time, and (when cheaply readable) schema."
        ),
    )
    @enveloped(core, "list_ifc_files")
    async def list_ifc_files(
        dir: Annotated[
            str | None,
            Field(description="Restrict to one directory (must be inside allowed dirs)."),
        ] = None,
        recursive: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> Envelope:
        if dir is not None:
            root = core.require_path_allowed(Path(dir))
            if not root.is_dir():
                raise ToolError(
                    "FILE_NOT_FOUND", f"{root} is not a directory.", "Pass a directory path."
                )
            roots = [root]
        else:
            roots = list(core.allowed_dirs)

        recent_paths = {e["path"] for e in core.recents.entries()}
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []
        for root in roots:
            pattern = "**/*" if recursive else "*"
            scanned = 0
            try:
                for p in root.glob(pattern):
                    scanned += 1
                    if scanned > _SCAN_CAP:
                        break
                    if p.suffix.lower() not in _IFC_SUFFIXES or not p.is_file():
                        continue
                    key = str(p.resolve())
                    if key in seen:
                        continue
                    seen.add(key)
                    try:
                        stat = p.stat()
                        row = {
                            "path": key,
                            "size_bytes": stat.st_size,
                            "mtime": datetime.fromtimestamp(
                                stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                            "schema": _peek_schema(p),
                            "recent": key in recent_paths,
                        }
                    except OSError:
                        # one unreadable file (broken symlink, permissions)
                        # must not hide the rest of the root
                        continue
                    rows.append(row)
            except OSError:
                continue
        rows.sort(key=lambda r: r["mtime"], reverse=True)
        total = len(rows)
        rows = rows[:limit]
        return ok(
            {"files": rows},
            core.session_meta(),
            char_limit=limit_,
            total=total,
            returned=len(rows),
        )

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
        description=(
            "[SESSION] Load a different IFC file into the session (replaces the "
            "current model; the user sees the switch in their terminal). Path "
            "must be inside the allowed directories; see list_ifc_files. Fails "
            "if there are unsaved changes."
        ),
    )
    @enveloped(core, "open_ifc_file")
    async def open_ifc_file(
        path: Annotated[str, Field(description="Path to an .ifc/.ifczip/.ifcxml file.")],
    ) -> Envelope:
        target = core.require_path_allowed(Path(path))
        if target.suffix.lower() not in _IFC_SUFFIXES:
            raise ToolError(
                "INVALID_INPUT",
                f"{target.name} does not look like an IFC file.",
                f"Expected one of: {', '.join(_IFC_SUFFIXES)}.",
            )
        if not target.exists():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{target} does not exist.",
                "Use list_ifc_files to see available files.",
            )
        if core.session.dirty:
            raise ToolError(
                "UNSAVED_CHANGES",
                "the current model has unsaved changes.",
                "Call save_ifc_file first, or ask the user to save/discard in the "
                "ifc-console terminal.",
            )
        await core.open_model(target)
        s = core.session
        data = {
            "loaded": True,
            "path": str(s.path),
            "name": s.name,
            "schema": s.schema,
            "size_bytes": s.size_bytes,
        }
        return ok(data, core.session_meta(), char_limit=limit_)

    @mcp.tool(
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True),
        description=(
            "[EDIT] Save the model. No arguments = save to its own file (in "
            "place). output_path = save-as (refuses to overwrite unless "
            "overwrite=true). A timestamped backup of any file being replaced is "
            "stored automatically. Clears the dirty flag. Unavailable in ask "
            "mode."
        ),
    )
    @enveloped(core, "save_ifc_file")
    @core.active_model_operation
    async def save_ifc_file(
        output_path: Annotated[
            str | None, Field(description="Save-as path; omit to save in place.")
        ] = None,
        overwrite: bool = False,
    ) -> Envelope:
        session = core.session
        if core.policy.mode is Mode.ASK:
            raise ToolError(
                "ASK_MODE_BLOCKED",
                "save_ifc_file is disabled in ask mode: the AI may query, never "
                "change, the model.",
                "Ask the user to run /mode edit in the ifc-console terminal.",
            )
        if output_path is not None:
            target = core.require_path_allowed(Path(output_path))
            in_place = session.path is not None and target == session.path
            if target.exists() and not overwrite and not in_place:
                raise ToolError(
                    "FILE_EXISTS",
                    f"{target} already exists.",
                    "Pass overwrite=true to replace it (a backup is made), or pick "
                    "another path.",
                )
        else:
            assert session.path is not None
            target = session.path

        owner_id = core.models.id_for_path(target)
        if owner_id is not None and owner_id != session.model_id:
            raise ToolError(
                "FILE_EXISTS",
                f"{target} belongs to resident model {owner_id!r}.",
                "Detach that model first, or save to a different path.",
            )

        result = await session.save(target, core.backups)
        core.recents.touch(
            Path(result["path"]),
            size_bytes=result["size_bytes"],
            schema=session.schema or "?",
            mode=core.policy.mode.value,
        )
        core.audit.record("save", **result)
        core.events.emit("model_saved", **result)
        return ok(result, core.session_meta(), char_limit=limit_)
