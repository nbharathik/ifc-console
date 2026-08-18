"""AppCore: owns the session singletons and wires them together.

Nothing reaches the model except through this object. The MCP layer, TUI,
CLI, and viewer are all thin faces over it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Literal

from ifc_console import __version__
from ifc_console.application.artifacts import ArtifactService
from ifc_console.application.batches import BatchService
from ifc_console.application.jobs import JobService
from ifc_console.application.operations import OperationService
from ifc_console.application.retention import ArtifactRetentionService
from ifc_console.application.transactions import TransactionService
from ifc_console.application.workflows import WorkflowService
from ifc_console.audit import AuditLog
from ifc_console.chat import ChatState
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import ToolError
from ifc_console.events import EventBus
from ifc_console.knowledge import KnowledgeBase
from ifc_console.plugins import PluginManager
from ifc_console.policy.modes import Mode, PolicyEngine
from ifc_console.recents import RecentsStore
from ifc_console.sandbox.policy import COMMON_CREDENTIAL_PATHS
from ifc_console.sandbox.runner import SandboxRunner
from ifc_console.session.backups import BackupStore
from ifc_console.session.model import ModelSession
from ifc_console.settings import SettingsStore
from ifc_console.viewer.hub import ViewerHub
from ifc_console.workspace.index import WorkspaceIndex, guess_discipline, slug
from ifc_console.workspace.kinds import describe_file, detect_kind, kind
from ifc_console.workspace.registry import Attachment, ModelRegistry

log = logging.getLogger("ifc-console")


@dataclass
class ViewerState:
    enabled: bool = False
    connected: int = 0
    url: str | None = None
    selection: list[str] = field(default_factory=list)


class AppCore:
    def __init__(
        self,
        store: SettingsStore,
        *,
        mode: Mode | None = None,
        port: int | None = None,
        extra_allowed_dirs: tuple[Path, ...] = (),
        transport: str = "http",
        viewer: bool | None = None,
        chat: bool | None = None,
    ) -> None:
        store.ensure_dirs()
        self.store = store
        s = store.settings
        self.events = EventBus()
        self.audit = AuditLog(store.sessions_dir, s.sessions.retention)
        self.recents = RecentsStore(store.recents_file, s.recents.max)
        self.backups = BackupStore(store.backups_dir, s.files.backup_retention)
        self.policy = PolicyEngine(
            mode or Mode(s.mode.default),
            allow_system_access=s.exec.allow_system_access,
            allow_ai_save=s.files.allow_ai_save,
            events=self.events,
            audit=self.audit,
        )
        # One active model is the default; the registry holds the optional
        # extra read-only models and the attached companion files.
        self.models = ModelRegistry(
            max_resident=s.workspace.max_resident,
            max_total_mb=s.workspace.max_total_mb,
        )
        self.workspace = WorkspaceIndex(
            lambda: list(self.allowed_dirs),
            cap=s.workspace.scan_cap,
            depth=s.workspace.scan_depth,
            enabled=lambda: self.settings.workspace.enabled,
        )
        # Persistent by default: clients get configured once and reconnect
        # across restarts. server.persistent_token=false restores per-run
        # tokens for the stricter threat model.
        self.token = (
            store.load_server_token() if s.server.persistent_token else secrets.token_hex(16)
        )
        self.port = port if port is not None else s.server.port
        self.transport = transport
        self.viewer = ViewerState()
        self.chat = ChatState(
            provider=s.chat.provider, model=s.chat.model, base_url=s.chat.base_url
        )
        self.viewer_hub = ViewerHub(self)
        self.sandbox = SandboxRunner(self)
        self.knowledge = KnowledgeBase(store.home, schemas=tuple(s.knowledge.schemas))
        self._knowledge_thread: threading.Thread | None = None
        self.server_running = False
        # Why the last start attempt failed, so /viewer and /chat can say more
        # than "not running" (a port conflict is the usual reason).
        self.server_error: str | None = None
        # Operations are registered independently of any transport. The old
        # mapping remains as a compatibility view for integrations that used
        # it before the operation registry became public.
        self.workspace_id = f"workspace-{secrets.token_hex(8)}"
        self.operations = OperationRegistry()
        self.plugins = PluginManager()
        self.operation_service = OperationService(self, self.operations)
        self.tool_functions = self.operations.handlers
        self._operations_registered = False
        self.artifacts = ArtifactService(store.artifacts_dir)
        self.artifact_retention = ArtifactRetentionService(
            self.artifacts,
            store.jobs_dir,
            batches_root=store.batches_dir,
            workflows_root=store.workflows_dir,
            default_retention_days=s.automation.artifact_retention_days,
        )
        self.transactions = TransactionService(
            self,
            store.transactions_dir,
            self.artifacts,
        )
        self.jobs = JobService(
            self,
            store.jobs_dir,
            self.artifacts,
            retention=s.automation.jobs_retention,
        )
        self.batches = BatchService(
            self,
            store.batches_dir,
            self.artifacts,
            retention=s.automation.jobs_retention,
        )
        self.workflows = WorkflowService(
            self,
            store.workflows_dir,
            self.artifacts,
            retention=s.automation.jobs_retention,
        )
        self._mcp = None  # set by attach_mcp once the tool server exists
        self._viewer_tools_registered = False
        # Filled from public MCP listings by /tools so its synchronous
        # completion menu can offer prompt and resource names too.
        self.tool_catalog_names: dict[str, tuple[str, ...]] = {}
        self._read_cache: dict[tuple, Any] = {}
        self._model_lifecycle = asyncio.Lock()
        self.ui_theme = s.tui.theme

        # Keep the directory the console was launched from as stable session
        # state. UI file discovery is deliberately scoped to this directory,
        # even when broader directories are allowed for MCP/tool access.
        self.launch_dir = Path.cwd().resolve()

        # viewer=True/False comes from the --viewer flag; None defers to
        # settings.
        want_viewer = s.viewer.enabled_default if viewer is None else viewer
        if want_viewer:
            self.enable_viewer()
        want_chat = s.chat.enabled_default if chat is None else chat
        if want_chat:
            self.enable_chat()

        self.allowed_dirs: list[Path] = []
        for d in s.files.allowed_dirs:
            self.add_allowed_dir(Path(d))
        for d in extra_allowed_dirs:
            self.add_allowed_dir(d)
        self.add_allowed_dir(self.launch_dir)

    # -- basics ---------------------------------------------------------------
    @property
    def settings(self):
        return self.store.settings

    @property
    def session(self) -> ModelSession:
        """The active model. Everything that says `core.session` means this one."""
        return self.models.active

    @property
    def mcp_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @property
    def viewer_url(self) -> str:
        """Tokenized URL a browser needs to open the viewer.

        The token rides the fragment: it never reaches the server, its logs,
        or a referrer, and the SPA scrubs it from the address bar on load.
        """
        return f"{self.viewer_public_url}#t={self.token}"

    @property
    def viewer_public_url(self) -> str:
        """Untokenized viewer URL safe for status, audit, and event payloads."""
        return f"http://127.0.0.1:{self.port}/viewer"

    # -- viewer lifecycle -------------------------------------------------------
    def attach_mcp(self, mcp) -> None:
        """Attach the current MCP projection of the operation registry."""
        self._mcp = mcp

    def _sync_viewer_tools(self) -> None:
        """Keep operation and MCP viewer surfaces in step with viewer.enabled.

        FastMCP evaluates tools/list per request, so added or removed tools
        are visible to clients on their next listing without a restart.
        """
        if not self._operations_registered:
            return
        from ifc_console.application.operations import register_viewer_operations
        from ifc_console.mcp import tools_viewer

        if self.viewer.enabled and not self._viewer_tools_registered:
            register_viewer_operations(self)
            if self._mcp is not None:
                from ifc_console.mcp.server import register_mcp_operations

                register_mcp_operations(
                    self._mcp,
                    self.operations,
                    self.operation_service,
                    names=tools_viewer.TOOL_NAMES,
                )
            self._viewer_tools_registered = True
        elif not self.viewer.enabled and self._viewer_tools_registered:
            for name in tools_viewer.TOOL_NAMES:
                self.operations.remove_tool(name)
                if self._mcp is not None:
                    self._mcp.remove_tool(name)
            self._viewer_tools_registered = False

    def enable_viewer(self) -> bool:
        """Turn the viewer surface on (idempotent): routes answer, viewer
        tools join the MCP tool list. Return false when its optional bundle is
        not installed."""
        if self.viewer.enabled:
            return True
        from ifc_console.viewer import assets

        if not assets.available():
            log.warning(assets.INSTALL_HINT)
            return False
        self.viewer.enabled = True
        self.viewer.url = self.viewer_url
        self._sync_viewer_tools()
        self.audit.record("viewer_enabled", url=self.viewer_public_url)
        self.events.emit("viewer_enabled", url=self.viewer_public_url)
        return True

    # -- chat panel -----------------------------------------------------------
    @property
    def chat_url(self) -> str:
        """The chat docked beside the 3D view: what /chat opens. The token
        rides the fragment, so it has to stay last in the URL."""
        return f"http://127.0.0.1:{self.port}/viewer?chat=1#t={self.token}"

    @property
    def chat_solo_url(self) -> str:
        """The panel on its own page, for a session with no viewer."""
        return f"http://127.0.0.1:{self.port}/chat#t={self.token}"

    def enable_chat(self) -> bool:
        """Turn the optional browser chat panel on (idempotent)."""
        if self.chat.enabled:
            return True
        from ifc_console.viewer import assets

        if not assets.available():
            log.warning(assets.INSTALL_HINT)
            return False
        self.chat.enabled = True
        self.chat.url = self.chat_url
        self.audit.record("chat_enabled", provider=self.chat.provider, model=self.chat.model)
        self.events.emit("chat_enabled", url=self.chat.url)
        return True

    def disable_chat(self) -> None:
        """Turn it off: routes 404 again and any session key is dropped."""
        if not self.chat.enabled:
            return
        self.chat.enabled = False
        self.chat.url = None
        self.chat.keys.clear()
        self.audit.record("chat_disabled")
        self.events.emit("chat_disabled")

    def disable_viewer(self) -> None:
        """Turn the viewer surface off: routes 404 again and the viewer tools
        leave the MCP tool list. Open tabs are closed by the caller (async)."""
        if not self.viewer.enabled:
            return
        self.viewer.enabled = False
        self.viewer.url = None
        self._sync_viewer_tools()
        self.audit.record("viewer_disabled")
        self.events.emit("viewer_disabled")

    def session_meta(self) -> dict:
        """The active model's meta, plus workspace keys only once more than one
        file is in play (model_id, models, attachments)."""
        meta = self.session.meta(self.policy.mode.value)
        meta["ai_save_allowed"] = self.policy.allow_ai_save
        meta.update(self.models.meta_extras())
        return meta

    # -- fingerprint-keyed read cache ----------------------------------------------
    async def cached_read(
        self,
        name: str,
        build: Callable[[], Any],
        *,
        key: tuple = (),
        timeout: float | None = 120,
        session: ModelSession | None = None,
    ) -> tuple[Any, bool]:
        """Run a read builder on the model worker, cached per fingerprint+revision.

        Returns (value, was_cached). The revision bumps on load, save, and
        every mutation, so a hit can never be stale; entries from dead
        revisions are pruned lazily to bound memory. Keyed on the model's own
        fingerprint, so attached models never share the active model's entries.
        """
        s = session or self.session
        cache_key = (name, id(s), s.fingerprint, s.revision, *key)
        if cache_key in self._read_cache:
            return self._read_cache[cache_key], True
        value = await s.run(build, timeout=timeout)
        if len(self._read_cache) >= 64:
            # every resident model's current revision survives the prune
            live = {(id(m), m.fingerprint, m.revision) for m in self.models.sessions.values()}
            live.add((id(s), s.fingerprint, s.revision))
            for stale in [k for k in self._read_cache if k[1:4] not in live]:
                del self._read_cache[stale]
        self._read_cache[cache_key] = value
        return value, False

    # -- allowed directories ------------------------------------------------------
    def generated_code_deny_paths(self) -> list[Path]:
        """Credential locations that generated Python must never read."""
        candidates = [self.store.home]
        user_home = Path.home()
        candidates.extend(user_home / name for name in COMMON_CREDENTIAL_PATHS)
        for root in self.allowed_dirs:
            candidates.extend(root / name for name in COMMON_CREDENTIAL_PATHS)

        denied: list[Path] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                resolved = candidate.expanduser().resolve(strict=False)
            except (OSError, RuntimeError):
                resolved = Path(os.path.abspath(os.path.expanduser(os.fspath(candidate))))
            key = os.path.normcase(str(resolved))
            if key not in seen:
                seen.add(key)
                denied.append(resolved)
        return denied

    def add_allowed_dir(self, directory: Path) -> None:
        try:
            resolved = directory.resolve()
        except OSError:
            return
        if resolved.is_file():
            resolved = resolved.parent
        if resolved not in self.allowed_dirs:
            self.allowed_dirs.append(resolved)
            self.workspace.scanned_at = None  # a new root means a stale index

    def path_allowed(self, path: Path) -> bool:
        try:
            resolved = path.resolve()
        except OSError:
            return False
        for root in self.allowed_dirs:
            try:
                resolved.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def require_path_allowed(self, path: Path) -> Path:
        resolved = path.resolve()
        if not self.path_allowed(resolved):
            allowed = ", ".join(str(d) for d in self.allowed_dirs) or "(none)"
            raise ToolError(
                "PATH_NOT_ALLOWED",
                f"{resolved} is outside the allowed directories.",
                f"Allowed roots: {allowed}. Ask the user to launch with --allow-dir "
                "or to pick the file in the ifc-console terminal.",
            )
        return resolved

    # -- lifecycle ----------------------------------------------------------------
    def start_audit(self) -> str:
        session_id = self.audit.start(
            {
                "version": __version__,
                "mode": self.policy.mode.value,
                "transport": self.transport,
                "port": self.port,
                "settings": self.store.flat(),
            }
        )
        for journal in self.transactions.recovered:
            self.audit.record(
                "transaction_recovered",
                transaction_id=journal.transaction_id,
                transaction_kind=journal.kind.value,
                phase=journal.phase.value,
                target_path=journal.target_path,
                job_id=journal.job_id,
                receipt_artifact_id=journal.receipt_artifact_id,
                outcome=(
                    "succeeded"
                    if journal.phase.value == "receipt_persisted"
                    else "rolled_back"
                    if journal.phase.value in {"rolled_back", "aborted"}
                    else "failed"
                ),
            )
        return session_id

    # Longer than this and a file stem stops being something an LLM retypes
    # reliably; ISO 19650 codes are exactly that case.
    _ALIAS_MAX = 20

    def suggest_alias(self, path: Path) -> str | None:
        """A model id worth typing. A readable stem wins; an opaque one falls
        back to the discipline the name reveals (arc, str, mep)."""
        stem = slug(path.stem)
        if len(stem) <= self._ALIAS_MAX:
            return stem
        entry = self.workspace.get(str(path)) if self.workspace.scanned_at else None
        discipline = entry.discipline if entry else guess_discipline(path.stem)
        return discipline.lower() if discipline else None

    async def open_model(
        self,
        path: Path,
        *,
        attach: bool | Literal["auto"] = False,
        alias: str | None = None,
        discard_dirty: bool = False,
    ) -> str:
        """Load an IFC file. The default replaces the active model, exactly as
        before; attach=True adds it read-only alongside. "auto" resolves under
        the lock (attach only if a model is already active), so two concurrent
        calls cannot both decide to replace."""
        path = path.resolve()
        async with self._model_lifecycle:
            if attach == "auto":
                attach = self.models.active_id is not None
            existing = self.models.id_for_path(path)
            if existing is not None:
                if not attach:
                    previous_id = self.models.active_id
                    self._guard_replacement(previous_id, existing, discard_dirty)
                    if previous_id is not None and previous_id != existing:
                        session = self.models.require(existing)
                        self.models.set_active(existing)
                        self.models.drop(previous_id, force=True)
                        self.audit.record("model_active", model_id=existing, path=str(session.path))
                        self.events.emit(
                            "active_model_changed",
                            model_id=existing,
                            name=session.name,
                            schema=session.schema,
                            path=str(session.path),
                        )
                    else:
                        self._set_active_model(existing)
                return existing
            try:
                size_bytes = path.stat().st_size
            except OSError:
                size_bytes = 0

            previous_id = self.models.active_id
            replacing = () if attach or previous_id is None else (previous_id,)
            self._guard_replacement(previous_id, None, discard_dirty or attach)
            self.models.plan_room(size_bytes, replacing=replacing)
            self.events.emit("model_loading", name=path.name, size_bytes=size_bytes)
            t0 = time.perf_counter()
            session = ModelSession()
            try:
                await session.open(path, max_mb=self.settings.files.max_open_mb)
            except Exception:
                session.close()
                self.events.emit("model_load_failed", name=path.name)
                raise
            try:
                evicted = self.models.plan_room(session.size_bytes, replacing=replacing)
                model_id = self.models.make_id(path, alias or self.suggest_alias(path))
                for evicted_id in evicted:
                    evicted_session = self.models.drop(evicted_id)
                    self.audit.record(
                        "model_evict", model_id=evicted_id, path=str(evicted_session.path)
                    )
                    self.events.emit(
                        "model_evicted", model_id=evicted_id, name=evicted_session.name
                    )
                if not attach and previous_id is not None:
                    self.models.drop(previous_id, force=True)
                self.models.add(model_id, session, active=not attach)
            except Exception:
                session.close()
                raise

            assert session.path is not None
            self.add_allowed_dir(session.path.parent)
            self.recents.touch(
                session.path,
                size_bytes=session.size_bytes,
                schema=session.schema or "?",
                mode=self.policy.mode.value,
            )
            self.audit.record(
                "model_attach" if attach else "model_open",
                path=str(session.path),
                model_id=model_id,
                schema=session.schema,
                size_bytes=session.size_bytes,
                fingerprint=session.fingerprint,
            )
            self.events.emit(
                "model_attached" if attach else "model_loaded",
                path=str(session.path),
                name=session.name,
                model_id=model_id,
                schema=session.schema,
                fingerprint=session.fingerprint,
                size_bytes=session.size_bytes,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            return model_id

    def _guard_replacement(
        self, previous_id: str | None, next_id: str | None, discard_dirty: bool
    ) -> None:
        if previous_id is None or previous_id == next_id or discard_dirty:
            return
        if self.models.require(previous_id).dirty:
            raise ToolError(
                "UNSAVED_CHANGES",
                "the current model has unsaved changes.",
                "Save it first, or confirm discarding changes in the ifc-console terminal.",
            )

    def _set_active_model(self, model_id: str) -> ModelSession:
        """Switch focus while the caller holds the model lifecycle lock."""
        session = self.models.require(model_id)
        if self.models.active_id == model_id:
            return session
        self.models.set_active(model_id)
        self.audit.record("model_active", model_id=model_id, path=str(session.path))
        self.events.emit(
            "active_model_changed",
            model_id=model_id,
            name=session.name,
            schema=session.schema,
            path=str(session.path),
        )
        return session

    async def set_active_model(self, model_id: str) -> ModelSession:
        """Move the write focus. Allowed while another model is dirty: the
        dirty model stays resident and unsaved work is never dropped."""
        async with self._model_lifecycle:
            return self._set_active_model(model_id)

    async def detach_model(self, model_id: str) -> ModelSession:
        async with self._model_lifecycle:
            previous_active = self.models.active_id
            session = self.models.drop(model_id)
            self.audit.record("model_detach", model_id=model_id, path=str(session.path))
            self.events.emit("model_detached", model_id=model_id, name=session.name)
            promoted_id = self.models.active_id
            if promoted_id is not None and promoted_id != previous_active:
                promoted = self.models.sessions[promoted_id]
                self.audit.record("model_active", model_id=promoted_id, path=str(promoted.path))
                self.events.emit(
                    "active_model_changed",
                    model_id=promoted_id,
                    name=promoted.name,
                    schema=promoted.schema,
                    path=str(promoted.path),
                )
            return session

    @asynccontextmanager
    async def active_session(self) -> AsyncIterator[ModelSession]:
        """Pin the active model while one operation is in flight."""
        async with self._model_lifecycle:
            session = self.session
            session.require_loaded()
            yield session

    def active_model_operation(self, operation: Callable) -> Callable:
        """Decorate an async operation that must keep one active model."""

        @wraps(operation)
        async def guarded(*args, **kwargs):
            async with self.active_session():
                return await operation(*args, **kwargs)

        return guarded

    def model_lifecycle_operation(self, operation: Callable) -> Callable:
        """Decorate an operation that must not overlap model lifecycle changes."""

        @wraps(operation)
        async def guarded(*args, **kwargs):
            async with self._model_lifecycle:
                return await operation(*args, **kwargs)

        return guarded

    def resolve_session(self, model_id: str | None = None) -> ModelSession:
        """The session a read tool should use: a named resident model, else the
        active one."""
        if not model_id:
            self.session.require_loaded()
            return self.session
        session = self.models.require(model_id)
        session.require_loaded()
        return session

    # -- companion files (IDS, BCF, schedules) ----------------------------------
    async def attach_file(self, path: Path, alias: str | None = None) -> Attachment:
        """Register a non-model file so the LLM can see and use it."""
        path = self.require_path_allowed(path)
        if not path.is_file():
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{path} does not exist.",
                "Call find_files to see what the workspace holds.",
            )
        # sniffing and describing read from disk; keep them off the event loop
        kind_name = await asyncio.to_thread(detect_kind, path)
        if kind_name is None:
            raise ToolError(
                "INVALID_INPUT",
                f"{path.name} is not a file kind ifc-console recognizes.",
                "Supported companions: .ids, .bcf/.bcfzip, .csv. Models go "
                "through open_ifc_file or attach.",
            )
        for existing in self.models.attachments.values():
            if existing.path == path:
                return existing
        spec = kind(kind_name)
        detail = await asyncio.to_thread(describe_file, path, kind_name)
        attachment = Attachment(
            alias=self.models.unique_attachment_alias(path, alias),
            path=path,
            kind=kind_name,
            consumed_by=spec.consumed_by if spec else None,
            detail=detail,
        )
        self.models.attach_file(attachment)
        self.add_allowed_dir(path.parent)
        self.audit.record("file_attach", path=str(path), kind=kind_name, alias=attachment.alias)
        self.events.emit("file_attached", path=str(path), kind=kind_name, alias=attachment.alias)
        return attachment

    def resolve_attachment(self, ref: str, *, kind: str | None = None) -> Path:
        """Accept either a path or the alias of an attached companion file."""
        attachment = self.models.attachments.get(ref.strip().lower())
        if attachment is not None:
            if kind is not None and attachment.kind != kind:
                raise ToolError(
                    "INVALID_INPUT",
                    f"{attachment.path.name} does not look like a {kind.upper()} file.",
                    f"Pass a {kind.upper()} file; find_files(kinds=['{kind}']) lists them.",
                )
            if not attachment.path.is_file():
                raise ToolError(
                    "FILE_NOT_FOUND",
                    f"{attachment.path} (alias {attachment.alias!r}) no longer exists.",
                    "The file moved or was deleted. Call attach again with the "
                    "current path, or detach the stale alias.",
                )
            return attachment.path
        path = self.require_path_allowed(Path(ref))
        if not path.is_file():
            aliases = ", ".join(sorted(self.models.attachments)) or "(none attached)"
            raise ToolError(
                "FILE_NOT_FOUND",
                f"{path} does not exist.",
                f"Pass a file path, or the alias of an attached file: {aliases}. "
                "find_files searches the allowed folders.",
            )
        if kind is not None and detect_kind(path) != kind:
            raise ToolError(
                "INVALID_INPUT",
                f"{path.name} does not look like a {kind.upper()} file.",
                f"Pass a {kind.upper()} file; find_files(kinds=['{kind}']) lists them.",
            )
        return path

    def detach_file(self, alias: str) -> Attachment:
        attachment = self.models.detach_file(alias)
        self.audit.record("file_detach", alias=alias, path=str(attachment.path))
        self.events.emit("file_detached", alias=alias, kind=attachment.kind)
        return attachment

    def set_mode(self, new_mode: Mode, *, by: str) -> None:
        if new_mode is self.policy.mode:
            return
        self.policy.set_mode(new_mode, by=by)

    def set_ui_theme(self, name: str, *, persist: bool = False) -> str:
        """Record the theme choice and tell every surface (auto resolves dark).

        Returns the resolved value the viewer should render ("dark"/"light").
        """
        self.ui_theme = name
        if persist:
            self.store.set_user("tui.theme", name)
        resolved = "light" if name == "light" else "dark"
        self.events.emit("theme_changed", theme=resolved)
        return resolved

    # -- knowledge base -------------------------------------------------------
    def start_knowledge(self) -> None:
        """Build the reference index in the background if it is missing.

        It reads only the installed ifcopenshell, so there is nothing to fetch
        and nothing to wait for; searches before it lands just return empty.
        """
        s = self.settings.knowledge
        if not (s.enabled and s.autobuild) or self.knowledge.ready:
            return
        if self._knowledge_thread is not None and self._knowledge_thread.is_alive():
            return

        def job() -> None:
            try:
                info = self.knowledge.build()
                self.events.emit("knowledge_ready", **{"total": info.get("total", 0)})
            except Exception:
                logging.getLogger("ifc-console.knowledge").warning(
                    "knowledge index build failed", exc_info=True
                )

        self._knowledge_thread = threading.Thread(
            target=job, name="ifc-console-knowledge", daemon=True
        )
        self._knowledge_thread.start()

    def shutdown(self) -> None:
        self.plugins.close()
        self.workflows.close()
        self.batches.close()
        self.jobs.close()
        self.sandbox.close()
        self.models.close_all()
        self.knowledge.close()
        self.audit.end()

    async def ashutdown(self) -> None:
        """Await job cleanup so transaction rollback finishes before sessions close."""
        self.plugins.close()
        await self.workflows.aclose()
        await self.batches.aclose()
        await self.jobs.aclose()
        self.sandbox.close()
        self.models.close_all()
        self.knowledge.close()
        self.audit.end()

    # -- logging helper used by the tool wrapper ------------------------------------
    def tool_event(self, tool: str, *, ok: bool, duration_ms: int, detail: str = "") -> None:
        context = self.operation_service.workspace_context()
        self.audit.record(
            "tool_call",
            tool=tool,
            ok=ok,
            outcome="succeeded" if ok else "failed",
            duration_ms=duration_ms,
            detail=detail,
            result_model_id=context.active_model.model_id,
            result_revision_id=context.active_model.revision_id,
        )
        self.events.emit("tool_called", tool=tool, ok=ok, duration_ms=duration_ms, detail=detail)
