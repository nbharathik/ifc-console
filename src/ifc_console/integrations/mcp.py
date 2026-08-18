"""Use any MCP server as a namespaced source in an IFC agent toolset."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ifc_console.toolsets import ToolDefinition

if TYPE_CHECKING:
    from mcp import ClientSession

_MAX_TOOL_PAGES = 100
_MAX_TOOLS = 10_000


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


class McpToolSource:
    """A persistent MCP client session projected as an agent ``ToolSource``."""

    def __init__(
        self,
        session: ClientSession,
        *,
        stack: AsyncExitStack | None = None,
        namespace: str = "mcp",
        source_id: str | None = None,
    ) -> None:
        self.session = session
        self._stack = stack
        self.namespace = namespace
        self.source_id = source_id or (f"mcp:{namespace}" if namespace else "mcp")
        self._closed = False

    @classmethod
    async def connect_http(
        cls,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        namespace: str = "mcp",
        source_id: str | None = None,
    ) -> McpToolSource:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(httpx.AsyncClient(headers=dict(headers or {})))
            read, write, _session_id = await stack.enter_async_context(
                streamable_http_client(url, http_client=client)
            )
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return cls(
                session,
                stack=stack,
                namespace=namespace,
                source_id=source_id,
            )
        except Exception:
            await stack.aclose()
            raise

    @classmethod
    async def connect_stdio(
        cls,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        namespace: str = "mcp",
        source_id: str | None = None,
    ) -> McpToolSource:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        stack = AsyncExitStack()
        try:
            parameters = StdioServerParameters(
                command=command,
                args=list(args),
                env=dict(env) if env is not None else None,
                cwd=cwd,
            )
            read, write = await stack.enter_async_context(stdio_client(parameters))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return cls(
                session,
                stack=stack,
                namespace=namespace,
                source_id=source_id,
            )
        except Exception:
            await stack.aclose()
            raise

    async def __aenter__(self) -> McpToolSource:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def list_tools(self) -> Sequence[ToolDefinition]:
        rows: list[Any] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0
        while True:
            if pages >= _MAX_TOOL_PAGES:
                raise RuntimeError(f"MCP tool listing exceeded {_MAX_TOOL_PAGES} pages")
            result = await self.session.list_tools(cursor=cursor)
            pages += 1
            if len(rows) + len(result.tools) > _MAX_TOOLS:
                raise RuntimeError(f"MCP tool listing exceeded {_MAX_TOOLS} tools")
            rows.extend(result.tools)
            next_cursor = getattr(result, "nextCursor", None)
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("MCP tool listing repeated a pagination cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

        definitions: list[ToolDefinition] = []
        for tool in rows:
            annotations = _json_value(tool.annotations) if tool.annotations is not None else {}
            meta = dict(tool.meta or {})
            tags = {"mcp"}
            raw_tags = meta.get("tags")
            if isinstance(raw_tags, list):
                tags.update(str(tag) for tag in raw_tags)
            definitions.append(
                ToolDefinition(
                    name=tool.name,
                    native_name=tool.name,
                    description=tool.description or tool.title or "",
                    input_schema=dict(tool.inputSchema or {}),
                    output_schema=dict(tool.outputSchema) if tool.outputSchema else None,
                    annotations=dict(annotations or {}),
                    tags=frozenset(tags),
                    requires_approval=not (
                        (annotations or {}).get("readOnlyHint") is True
                        and (annotations or {}).get("destructiveHint") is not True
                    ),
                    source=self.source_id,
                )
            )
        return definitions

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = await self.session.call_tool(name, dict(arguments))
        structured = result.structuredContent
        if isinstance(structured, Mapping) and "ok" in structured:
            return dict(structured)
        if result.isError:
            text = "\n".join(
                str(getattr(item, "text", ""))
                for item in result.content
                if getattr(item, "text", None)
            )
            return {
                "ok": False,
                "error": {
                    "code": "MCP_TOOL_ERROR",
                    "message": text[:600] or f"MCP tool {name!r} failed",
                    "hint": "Inspect the MCP server and call arguments.",
                },
                "meta": {"tool_source": self.source_id},
            }
        data: Any
        if structured is not None:
            data = _json_value(structured)
        else:
            data = {"content": [_json_value(item) for item in result.content]}
        return {
            "ok": True,
            "data": data,
            "meta": {"tool_source": self.source_id},
        }

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._stack is not None:
            await self._stack.aclose()


__all__ = ["McpToolSource"]
