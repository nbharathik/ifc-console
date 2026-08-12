"""Backend-neutral runtime for embedding IFC Console in agent applications."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ifc_console.sdk import AsyncWorkbench, IfcConsoleError
from ifc_console.toolsets import ToolDefinition, Toolset, ToolSource, definition_from_operation


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
    ) -> Toolset:
        return await Toolset.build(
            self.backend,
            *additional_sources,
            permitted_only=permitted_only,
        )

    async def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        value = await self.backend.call_tool(name, arguments)
        if isinstance(value, Mapping):
            return dict(value)
        return {"ok": True, "data": {"result": value}, "meta": {}}

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

    @classmethod
    async def open(
        cls,
        path: str | Path | None = None,
        *,
        mode: str = "ask",
        home: str | Path | None = None,
        allowed_dirs: tuple[str | Path, ...] = (),
        settings: dict[str, Any] | None = None,
    ) -> LocalRuntime:
        workbench = await AsyncWorkbench.create(
            path,
            mode=mode,
            home=home,
            allowed_dirs=allowed_dirs,
            settings=settings,
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
    "IfcRuntime",
    "LocalOperationBackend",
    "LocalRuntime",
    "OperationBackend",
    "WorkspaceClient",
]
