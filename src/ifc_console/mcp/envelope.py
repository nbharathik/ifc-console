"""Compatibility exports for the transport-neutral result contracts."""

from ifc_console.core.results import (
    ERROR_CODES,
    Envelope,
    ErrorInfo,
    ToolError,
    dump,
    err,
    from_tool_error,
    ok,
)

__all__ = [
    "ERROR_CODES",
    "Envelope",
    "ErrorInfo",
    "ToolError",
    "dump",
    "err",
    "from_tool_error",
    "ok",
]
