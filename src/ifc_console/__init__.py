"""ifc-console: a terminal interface to connect IFC files to LLMs."""

from typing import TYPE_CHECKING, Any

__version__ = "0.2.0"

# The SDK pulls in the whole backend, so it stays behind a lazy attribute:
# `ifc-console --help` must not pay for ifcopenshell.
__all__ = [
    "AsyncWorkbench",
    "ApprovalRecord",
    "ArtifactGCPlan",
    "ArtifactGCResult",
    "ArtifactRef",
    "AuditVerification",
    "BatchRecord",
    "Capability",
    "CapabilityDecision",
    "ChangeSetRecord",
    "CommitRecord",
    "IfcConsoleError",
    "JobRecord",
    "OperationDefinition",
    "QueryElementsData",
    "RestoreRecord",
    "TransactionJournal",
    "ValidationData",
    "WorkflowPlan",
    "WorkflowRecord",
    "WorkflowSpec",
    "Workbench",
    "WorkspaceContext",
    "__version__",
]

if TYPE_CHECKING:
    from ifc_console.sdk import (
        ApprovalRecord,
        ArtifactGCPlan,
        ArtifactGCResult,
        ArtifactRef,
        AsyncWorkbench,
        AuditVerification,
        BatchRecord,
        Capability,
        CapabilityDecision,
        ChangeSetRecord,
        CommitRecord,
        IfcConsoleError,
        JobRecord,
        OperationDefinition,
        QueryElementsData,
        RestoreRecord,
        TransactionJournal,
        ValidationData,
        Workbench,
        WorkflowPlan,
        WorkflowRecord,
        WorkflowSpec,
        WorkspaceContext,
    )


def __getattr__(name: str) -> Any:
    if name in __all__ and name != "__version__":
        from ifc_console import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
