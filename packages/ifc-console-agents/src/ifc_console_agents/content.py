"""Persist and enforce per-agent access to project reference content."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path, PurePosixPath
from typing import Any

from ifc_console.toolsets import ToolCall, ToolCallNext

from ifc_console_agents.files import REFERENCES_DIR, is_turn_reference_path

CONTENT_ACCESS_FILE = Path(".ifc-console") / "agents" / "content-access.json"
_MANAGED_PREFIX = REFERENCES_DIR.as_posix().rstrip("/") + "/"
_MAX_CONTENT_PATHS = 500
_UNSAFE_PATH = "<unsafe-project-content-path>"


def normalize_content_path(value: Any) -> str | None:
    """Return one portable project path, or None for an unsafe value."""
    if not isinstance(value, str):
        return None
    raw = value.strip().replace("\\", "/")
    if not raw or len(raw) > 4096 or "\x00" in raw:
        return None
    path = PurePosixPath(raw)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return None
    return path.as_posix()


def _clean_paths(values: Iterable[Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for value in values:
        normalized = normalize_content_path(value)
        if normalized is not None and normalized not in paths:
            paths.append(normalized)
        if len(paths) >= _MAX_CONTENT_PATHS:
            break
    return tuple(paths)


class AgentContentAccessStore:
    """Atomic project-local access settings for built-in and host agents."""

    def __init__(self, project_dir: str | Path, *, path: str | Path | None = None) -> None:
        self.project_dir = Path(project_dir).expanduser().resolve()
        # the panel keeps this under the console home; the project-local
        # default remains for hosts that embed the store directly
        self.path = Path(path).expanduser() if path else self.project_dir / CONTENT_ACCESS_FILE
        self._lock = threading.Lock()

    def _read(self) -> dict[str, tuple[str, ...]] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return None
        agents = payload.get("agents") if isinstance(payload, dict) else None
        if not isinstance(agents, dict):
            return None
        parsed: dict[str, tuple[str, ...]] = {}
        for name, paths in agents.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", name):
                continue
            parsed[name] = _clean_paths(paths) if isinstance(paths, list) else ()
        return parsed

    def get(self, agent: str) -> tuple[str, ...] | None:
        """None means legacy access to all project references."""
        with self._lock:
            agents = self._read()
            return () if agents is None else agents.get(agent)

    def set(self, agent: str, paths: Iterable[str] | None) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent or ""):
            raise ValueError("invalid agent name")
        with self._lock:
            agents = self._read() or {}
            if paths is None:
                agents.pop(agent, None)
            else:
                agents[agent] = _clean_paths(paths)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = self.path
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            payload = {"version": 1, "agents": {key: list(agents[key]) for key in sorted(agents)}}
            try:
                with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)


def configured_paths(pack: Any, store: AgentContentAccessStore) -> tuple[str, ...] | None:
    """Resolve a custom blueprint setting before the shared agent setting."""
    blueprint = getattr(pack, "blueprint", None)
    paths = getattr(blueprint, "content_paths", None) if blueprint is not None else None
    if paths is not None:
        return _clean_paths(paths)
    return store.get(pack.info.name)


def content_access_payload(
    configured: tuple[str, ...] | None,
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = set(configured or ())
    mode = "all" if configured is None else "selected"
    rows = [
        {
            **row,
            "allowed": mode == "all" or str(row.get("path") or "") in selected,
        }
        for row in files
    ]
    return {
        "access": {"mode": mode, "paths": sorted(selected)},
        "files": rows,
    }


def _path_from_row(row: Any) -> str | None:
    if not isinstance(row, Mapping):
        return None
    raw = row.get("path")
    if isinstance(raw, str) and raw.strip():
        return normalize_content_path(raw) or _UNSAFE_PATH
    meta = row.get("meta")
    if isinstance(meta, Mapping):
        raw = meta.get("path")
        if isinstance(raw, str) and raw.strip():
            return normalize_content_path(raw) or _UNSAFE_PATH
    return None


def _document_path_from_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("doc:"):
        return None
    body = value[4:]
    path, separator, _suffix = body.rpartition("#")
    return normalize_content_path(path if separator else body) or _UNSAFE_PATH


def _denied(path: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "CONTENT_ACCESS_DENIED",
            "message": f"this agent cannot access project content {path!r}",
            "hint": "Enable the file in Agent workspace or attach it to this message.",
        },
        "meta": {"tool_source": "agent-content"},
    }


class AgentContentGate:
    """Tool middleware that keeps project content inside an agent's selection."""

    def __init__(self, configured: tuple[str, ...] | None) -> None:
        self.configured = None if configured is None else frozenset(_clean_paths(configured))
        self._temporary: ContextVar[frozenset[str]] = ContextVar(
            f"agent_content_{id(self)}", default=frozenset()
        )

    @property
    def signature(self) -> str:
        if self.configured is None:
            return "content:all"
        return "content:selected:" + ",".join(sorted(self.configured))

    def allows(self, path: Any) -> bool:
        normalized = normalize_content_path(path)
        if normalized is None:
            return False
        temporary = self._temporary.get()
        if is_turn_reference_path(normalized):
            return normalized in temporary
        return self.configured is None or normalized in self.configured or normalized in temporary

    @contextmanager
    def temporary(self, paths: Iterable[str]):
        token = self._temporary.set(frozenset(_clean_paths(paths)))
        try:
            yield
        finally:
            self._temporary.reset(token)

    def _filter_rows(self, rows: Any) -> tuple[list[Any], int]:
        """The rows this agent may see, and how many were hidden."""
        if not isinstance(rows, list):
            return [], 0
        kept = []
        hidden = 0
        for row in rows:
            path = _path_from_row(row)
            if path is None or self.allows(path):
                kept.append(row)
            else:
                hidden += 1
        return kept, hidden

    @staticmethod
    def _note(hidden: int, what: str) -> str:
        return (
            f"{hidden} {what} hidden: this agent's content access does not include their "
            "files. Do not guess the values; ask the user to enable the collection in Agent "
            "workspace > Content (or attach the file / a skill of that collection to the "
            "message)."
        )

    async def __call__(self, call: ToolCall, call_next: ToolCallNext) -> dict[str, Any]:
        name = call.name
        if name in {"get_project_reference_image", "get_project_document_page"}:
            path = call.arguments.get("path")
            if not self.allows(path):
                return _denied(str(path or ""))
        elif name == "get_knowledge_record":
            path = _document_path_from_key(call.arguments.get("key"))
            if path is not None and not self.allows(path):
                return _denied(path)

        result = await call_next(call)
        if not result.get("ok"):
            return result
        data = result.get("data")
        if not isinstance(data, Mapping):
            return result
        filtered = dict(data)
        # A silently emptied result reads as "nothing there" and sends the
        # agent guessing; the note says what was hidden and who can open it.
        hidden = 0
        if name == "list_project_documents":
            filtered["files"], hidden = self._filter_rows(data.get("files"))
            what = "document(s)"
        elif name == "lookup_table_rows":
            filtered["rows"], hidden = self._filter_rows(data.get("rows"))
            what = "matching row(s)"
        elif name == "search_ifc_knowledge":
            corpus = call.arguments.get("corpus", "all")
            what = "hit(s)"
            if corpus == "project":
                filtered["hits"], hidden = self._filter_rows(data.get("hits"))
            if "project_hits" in data:
                filtered["project_hits"], more = self._filter_rows(data.get("project_hits"))
                hidden += more
        else:
            return result
        if hidden:
            filtered["hidden"] = hidden
            filtered["access_note"] = self._note(hidden, what)
        meta = dict(result.get("meta") or {})
        visible = filtered.get("files")
        if not isinstance(visible, list):
            visible = filtered.get("hits")
        if isinstance(visible, list):
            meta["returned"] = len(visible)
        return {**result, "data": filtered, "meta": meta}


def managed_content_path(path: str) -> bool:
    """A path inside a references store: the legacy project folder or the home."""
    normalized = normalize_content_path(path)
    if normalized is None:
        return False
    return normalized.startswith(_MANAGED_PREFIX) or (
        normalized.startswith("agents/") and "/references/" in normalized
    )


__all__ = [
    "CONTENT_ACCESS_FILE",
    "AgentContentAccessStore",
    "AgentContentGate",
    "configured_paths",
    "content_access_payload",
    "managed_content_path",
    "normalize_content_path",
]
