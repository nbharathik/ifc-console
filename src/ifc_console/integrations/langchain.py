"""Optional direct LangChain/LangGraph bindings for an IFC ``Toolset``.

The adapter is intentionally thin: IFC Console owns IFC execution and policy;
LangChain owns model orchestration, persistence, middleware, and streaming.
Nothing here starts an MCP server or serializes a model into prompt context.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ifc_console.toolsets import Toolset


def to_langchain_tools(
    toolset: Toolset,
    *,
    structured_artifacts: bool = True,
) -> list[Any]:
    """Convert a selected IFC toolset to LangChain ``StructuredTool`` objects.

    Use the returned tools with ``langchain.agents.create_agent`` or a LangGraph
    ``ToolNode``. Calls are asynchronous; invoke the graph with ``ainvoke`` or
    ``astream`` so the IFC runtime remains on its owning event loop.

    When ``structured_artifacts`` is true, each LangChain ``ToolMessage`` keeps
    the complete IFC envelope as its artifact while the model receives the same
    bounded envelope as JSON text.
    """

    try:
        from langchain_core.tools import StructuredTool
    except ImportError:
        raise ImportError(
            "LangChain is not an ifc-console dependency. Install langchain in "
            "your agent application's environment before using this adapter."
        ) from None

    tools: list[Any] = []
    for definition in toolset.definitions:

        async def call_tool(
            _tool_name: str = definition.name,
            **arguments: Any,
        ) -> Any:
            payload = await toolset.call(_tool_name, arguments)
            content = json.dumps(payload, default=str, ensure_ascii=False)
            if structured_artifacts:
                return content, payload
            return content

        tools.append(
            StructuredTool.from_function(
                coroutine=call_tool,
                name=definition.name,
                description=definition.description,
                args_schema=definition.input_schema,
                infer_schema=False,
                response_format=("content_and_artifact" if structured_artifacts else "content"),
                tags=sorted(definition.tags),
                metadata={
                    "source": definition.source,
                    "required_capabilities": list(definition.required_capabilities),
                    "requires_approval": definition.requires_approval,
                },
            )
        )
    return tools


__all__ = ["to_langchain_tools"]
