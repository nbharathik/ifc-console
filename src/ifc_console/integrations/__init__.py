"""Optional adapters for composing IFC Console with external systems."""

from ifc_console.integrations.langchain import to_langchain_tools
from ifc_console.integrations.mcp import McpToolSource

__all__ = ["McpToolSource", "to_langchain_tools"]
