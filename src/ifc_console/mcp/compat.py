"""MCP SDK compatibility layer.

Imports the server API by its SDK v2 names on both SDK lines, so the rest of
the codebase is written against v2 (MCPServer) and runs unchanged on 1.28+.
The decorator surface (tool/resource/prompt, remove_tool, icons, title,
structured_output) is identical on both lines; only import paths moved.
"""

from __future__ import annotations

try:
    from mcp.server import MCPServer
    from mcp.server.mcpserver import Image

    MCP_SDK_V2 = True
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.utilities.types import Image

    MCP_SDK_V2 = False

from collections.abc import Callable

from mcp import types
from mcp.types import ToolAnnotations


def install_listing(mcp: MCPServer, select: Callable[[list[types.Tool]], list[types.Tool]]) -> None:
    """Answer `tools/list` with `select(all tools)`, leaving every tool callable.

    The SDK validates a call against the tool it last listed. Listing a subset
    would leave the others unvalidated (and logged), so the cache keeps the full
    set while the wire shows the selection. This is the only place that touches
    SDK internals for listings.
    """
    server = getattr(mcp, "_mcp_server", None)
    original = getattr(mcp, "list_tools", None)
    if server is None or original is None or not hasattr(server, "list_tools"):
        return

    @server.list_tools()
    async def list_tools() -> types.ListToolsResult:
        everything = list(await original())
        cache = getattr(server, "_tool_cache", None)
        if isinstance(cache, dict):
            cache.update({tool.name: tool for tool in everything})
        return types.ListToolsResult(tools=select(everything))


__all__ = ["MCP_SDK_V2", "Image", "MCPServer", "ToolAnnotations", "install_listing"]
