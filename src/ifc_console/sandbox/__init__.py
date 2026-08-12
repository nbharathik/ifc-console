"""Process-level sandbox for LLM-authored code.

The in-process guards in `ifc_console.policy.guards` shape what generated code
can reach. This package provides the stronger path for eligible read-only code:
a separate process with no network, no subprocesses, no credential environment,
a memory cap, and a read-only view of the model directories. Auto mode can
report and use guarded in-process fallback; strict mode refuses it.

Nothing here imports ifcopenshell at module level; the worker pays that cost
in its own process.
"""

from ifc_console.sandbox.client import SandboxError, SandboxNotReady, SandboxTimeout
from ifc_console.sandbox.policy import SandboxPolicy
from ifc_console.sandbox.runner import Decision, SandboxResult, SandboxRunner

__all__ = [
    "Decision",
    "SandboxError",
    "SandboxNotReady",
    "SandboxPolicy",
    "SandboxResult",
    "SandboxRunner",
    "SandboxTimeout",
]
