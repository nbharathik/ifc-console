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

from mcp.types import ToolAnnotations

__all__ = ["MCP_SDK_V2", "Image", "MCPServer", "ToolAnnotations"]
