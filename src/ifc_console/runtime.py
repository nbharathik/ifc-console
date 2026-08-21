"""Backend-neutral runtime for embedding IFC Console in agent applications."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ifc_console.sdk import AsyncWorkbench, IfcConsoleError
from ifc_console.toolsets import (
    IfcToolProfile,
    ToolDefinition,
    Toolset,
    ToolSource,
    definition_from_operation,
)

if TYPE_CHECKING:
    from ifc_console.agents.agent import Agent
    from ifc_console.agents.models import (
        AgentLimits,
        AgentModel,
        ApprovalHandler,
        ThreadStore,
    )
    from ifc_console.toolsets import ToolMiddleware


@dataclass(frozen=True)
class EmbeddedWebApp:
    """The loopback web surface an embedding application can serve."""

    app: Any
    origin: str
    token: str = field(repr=False)
    viewer_url: str | None = None
    chat_url: str | None = None

    def browser_url(self, path: str) -> str:
        """Build a tokenized URL for a browser page mounted on this app."""

        clean_path = "/" + path.lstrip("/")
        return f"{self.origin}{clean_path}#t={self.token}"


@runtime_checkable
class OperationBackend(ToolSource, Protocol):
    """The small boundary shared by embedded and remote console runtimes."""


class LocalOperationBackend:
    """Call the existing transport-neutral service in this Python process."""

    namespace = ""
    source_id = "ifc-console:local"

    def __init__(self, workbench: AsyncWorkbench, *, owns_workbench: bool = True) -> None:
        self.workbench = workbench
        self.owns_workbench = owns_workbench

    async def list_tools(self) -> Sequence[ToolDefinition]:
        return [
            definition_from_operation(payload, source=self.source_id)
            for payload in await self.workbench.tools(permitted_only=False)
        ]

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        return await self.workbench.call(name, **dict(arguments))

    async def aclose(self) -> None:
        if self.owns_workbench:
            await self.workbench.aclose()


class IfcRuntime:
    """A composable IFC execution runtime backed locally or by a console."""

    def __init__(self, backend: OperationBackend) -> None:
        self.backend = backend
        self.workspace = WorkspaceClient(self)
        self._closed = False

    async def __aenter__(self) -> IfcRuntime:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def toolset(
        self,
        *additional_sources: ToolSource,
        permitted_only: bool = True,
        profile: IfcToolProfile | str = IfcToolProfile.FULL,
    ) -> Toolset:
        base = await Toolset.build(self.backend, permitted_only=permitted_only)
        scoped = base.for_profile(profile)
        if not additional_sources:
            return scoped
        additions = await Toolset.build(*additional_sources, permitted_only=permitted_only)
        return scoped + additions

    async def tools(
        self,
        *names: str,
        sources: Sequence[ToolSource] = (),
        permitted_only: bool = True,
        profile: IfcToolProfile | str = IfcToolProfile.FULL,
    ) -> Toolset:
        """Build a toolset containing only the named operations.

        Names may be exact names or glob patterns. Application and MCP sources
        remain explicit so each agent can expose a deliberately small surface.
        """

        available = await self.toolset(
            *sources,
            permitted_only=permitted_only,
            profile=profile,
        )
        if not names:
            return available
        selected = available.include(*names)
        missing = [
            name
            for name in names
            if not any(char in name for char in "*?[") and name not in selected
        ]
        if missing:
            choices = ", ".join(available.names)
            raise KeyError(
                f"unknown or unavailable tools: {', '.join(missing)}; available: {choices}"
            )
        return selected

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        value = await self.backend.call_tool(name, arguments)
        if isinstance(value, Mapping):
            return dict(value)
        return {"ok": True, "data": {"result": value}, "meta": {}}

    async def create_agent(
        self,
        *,
        name: str,
        model: AgentModel,
        instructions: str,
        tool_sources: Sequence[ToolSource] = (),
        tool_profile: IfcToolProfile | str = IfcToolProfile.FULL,
        permitted_only: bool = True,
        thread_store: ThreadStore | None = None,
        approval_handler: ApprovalHandler | None = None,
        limits: AgentLimits | None = None,
        middleware: Sequence[ToolMiddleware] = (),
        model_options: Mapping[str, Any] | None = None,
    ) -> Agent:
        """Build the bundled provider-neutral agent over this runtime."""

        from ifc_console.agents import Agent

        tools = await self.toolset(
            *tool_sources,
            permitted_only=permitted_only,
            profile=tool_profile,
        )
        return Agent(
            name=name,
            model=model,
            tools=tools,
            instructions=instructions,
            thread_store=thread_store,
            approval_handler=approval_handler,
            limits=limits,
            middleware=middleware,
            model_options=model_options,
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self.backend.aclose()


class LocalRuntime(IfcRuntime):
    """An embedded runtime that opens IFC models in the current process."""

    def __init__(self, backend: LocalOperationBackend) -> None:
        super().__init__(backend)
        self._local_backend = backend
        self.settings = RuntimeSettings(backend.workbench)

    @classmethod
    async def open(
        cls,
        path: str | Path | None = None,
        *,
        mode: str = "ask",
        home: str | Path | None = None,
        allowed_dirs: tuple[str | Path, ...] = (),
        settings: dict[str, Any] | None = None,
        project_dir: str | Path | None = None,
    ) -> LocalRuntime:
        workbench = await AsyncWorkbench.create(
            path,
            mode=mode,
            home=home,
            allowed_dirs=allowed_dirs,
            settings=settings,
            project_dir=project_dir,
        )
        return cls(LocalOperationBackend(workbench))

    @classmethod
    def from_workbench(
        cls,
        workbench: AsyncWorkbench,
        *,
        owns_workbench: bool = False,
    ) -> LocalRuntime:
        return cls(LocalOperationBackend(workbench, owns_workbench=owns_workbench))

    @property
    def workbench(self) -> AsyncWorkbench:
        return self._local_backend.workbench

    @property
    def context(self):
        return self.workbench.context

    @property
    def mode(self) -> str:
        return self.workbench.mode

    def set_mode(self, mode: str) -> str:
        """Caller-owned compatibility switch; never advertise it as a tool."""

        return self.workbench.set_mode(mode)

    async def open_model(self, path: str | Path, **kwargs: Any) -> dict[str, Any]:
        """Open an IFC model and allow its parent directory for this runtime."""

        return await self.workbench.open_model(path, **kwargs)

    def enable_viewer(self) -> str:
        """Enable viewer operations and return its tokenized loopback URL."""

        if not self.workbench.core.enable_viewer():
            raise IfcConsoleError(
                "VIEWER_NOT_INSTALLED",
                "the optional viewer assets are unavailable",
                'Install "ifc-console[viewer]".',
            )
        return self.workbench.core.viewer_url

    def build_web_app(
        self,
        *,
        viewer: bool = False,
        extra_routes: Sequence[Any] = (),
    ) -> EmbeddedWebApp:
        """Build the authenticated MCP/viewer app for custom panels and APIs.

        The caller owns the ASGI server lifecycle and should bind it to loopback.
        Build or enable the viewer before creating a toolset when the agent needs
        viewer-selection and highlighting tools.
        """

        from ifc_console.mcp.server import build_http_app, build_mcp

        core = self.workbench.core
        if viewer:
            self.enable_viewer()
        app = build_http_app(core, build_mcp(core), extra_routes=list(extra_routes))
        origin = f"http://127.0.0.1:{core.port}"
        return EmbeddedWebApp(
            app=app,
            origin=origin,
            token=core.token,
            viewer_url=core.viewer_url if core.viewer.enabled else None,
            chat_url=core.chat_solo_url if core.chat.enabled else None,
        )


class ConsoleRuntime(IfcRuntime):
    """A runtime connected to an existing IFC Console through MCP."""

    @classmethod
    async def connect_http(
        cls,
        url: str,
        *,
        token: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> ConsoleRuntime:
        from ifc_console.integrations.mcp import McpToolSource

        combined_headers = dict(headers or {})
        if token:
            combined_headers["Authorization"] = f"Bearer {token}"
        source = await McpToolSource.connect_http(
            url,
            headers=combined_headers,
            namespace="",
            source_id="ifc-console:remote",
        )
        return cls(source)

    @classmethod
    async def connect_stdio(
        cls,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
    ) -> ConsoleRuntime:
        from ifc_console.integrations.mcp import McpToolSource

        source = await McpToolSource.connect_stdio(
            command,
            args,
            env=env,
            cwd=cwd,
            namespace="",
            source_id="ifc-console:remote",
        )
        return cls(source)


class RuntimeSettings:
    """Validated, session-only settings for an embedded runtime.

    This component never edits ``~/.ifc-console/settings.json``. Keep it in
    host code; settings and mode changes should not be model-callable tools.
    """

    def __init__(self, workbench: AsyncWorkbench) -> None:
        self._workbench = workbench

    def all(self) -> dict[str, Any]:
        return self._workbench.settings

    def get(self, key: str) -> Any:
        return self._workbench.get_setting(key)

    def set(self, key: str, value: Any) -> Any:
        return self.update({key: value})[key]

    def update(self, overrides: Mapping[str, Any]) -> dict[str, Any]:
        return self._workbench.configure(overrides)


class WorkspaceClient:
    """Model/workspace operations available through either runtime backend."""

    def __init__(self, runtime: IfcRuntime) -> None:
        self.runtime = runtime

    @staticmethod
    def _data(payload: Mapping[str, Any]) -> dict[str, Any]:
        if payload.get("ok"):
            data = payload.get("data")
            return dict(data) if isinstance(data, Mapping) else {"result": data}
        error = payload.get("error") or {}
        raise IfcConsoleError(
            str(error.get("code") or "OPERATION_FAILED"),
            str(error.get("message") or "operation failed"),
            str(error.get("hint") or ""),
        )

    async def status(self) -> dict[str, Any]:
        return self._data(await self.runtime.call("get_session_status"))

    async def orient(self) -> dict[str, Any]:
        return self._data(await self.runtime.call("orient"))

    async def info(self, *, model: str | None = None) -> dict[str, Any]:
        arguments = {"model": model} if model is not None else {}
        return self._data(await self.runtime.call("get_ifc_project_info", **arguments))

    async def search(
        self,
        term: str,
        *,
        limit: int = 50,
        model: str | None = None,
    ) -> list[dict[str, Any]]:
        arguments: dict[str, Any] = {"term": term, "limit": limit}
        if model is not None:
            arguments["model"] = model
        data = self._data(await self.runtime.call("search_elements", **arguments))
        return list(data.get("results") or ())

    async def selection(self) -> list[dict[str, Any]]:
        """Return elements the user selected in the connected 3D viewer."""

        data = self._data(await self.runtime.call("get_viewer_selection"))
        return list(data.get("elements") or ())

    async def open(self, path: str | Path) -> dict[str, Any]:
        return self._data(await self.runtime.call("open_ifc_file", path=str(path)))

    async def models(self) -> dict[str, Any]:
        return self._data(await self.runtime.call("list_models"))

    async def attach(self, path: str | Path, *, alias: str | None = None) -> dict[str, Any]:
        return self._data(await self.runtime.call("attach", path=str(path), alias=alias))

    async def detach(self, identifier: str) -> dict[str, Any]:
        return self._data(await self.runtime.call("detach", id=identifier))

    async def use(self, model_id: str) -> dict[str, Any]:
        return self._data(await self.runtime.call("set_active_model", model_id=model_id))


__all__ = [
    "ConsoleRuntime",
    "EmbeddedWebApp",
    "IfcRuntime",
    "LocalOperationBackend",
    "LocalRuntime",
    "OperationBackend",
    "RuntimeSettings",
    "WorkspaceClient",
]
