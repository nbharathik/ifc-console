"""Process-level sandbox for LLM-authored code.

The in-process guards in `ifc_console.policy.guards` shape what generated
code can reach. This package decides what it can *do*: the code runs in a
separate process with no network, no subprocesses, no credentials in its
environment, a memory cap, and a read-only view of the model directories.

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
