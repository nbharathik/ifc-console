"""ifc-console: a terminal interface to connect IFC files to LLMs."""

from importlib.util import find_spec
from typing import TYPE_CHECKING, Any

__version__ = "0.1.4"

# The SDK pulls in the whole backend, so it stays behind a lazy attribute:
# `ifc-console --help` must not pay for ifcopenshell.
__all__ = [
    "Agent",
    "AgentEvent",
    "AgentEventType",
    "AgentImage",
    "AgentLimits",
    "AgentMessage",
    "AgentModel",
    "AgentRole",
    "AgentRunError",
    "AgentRunResult",
    "AgentToolCallRecord",
    "AgentToolSource",
    "AgentUsage",
    "AsyncWorkbench",
    "Approval",
    "ApprovalRecord",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalRequest",
    "Authority",
    "ArtifactGCPlan",
    "ArtifactGCResult",
    "ArtifactRef",
    "AuditVerification",
    "BatchChildRecord",
    "BatchRecord",
    "BatchSpec",
    "BatchState",
    "Capability",
    "CapabilityDecision",
    "ChangeSet",
    "ChangeSetRecord",
    "ClassificationAssignmentChange",
    "CommitJobSpec",
    "CommitRecord",
    "CommitResult",
    "ConsoleRuntime",
    "CallbackApprovalHandler",
    "DenyAllApprovals",
    "EmbeddedWebApp",
    "Envelope",
    "ErrorInfo",
    "IfcConsoleError",
    "IfcRuntime",
    "IfcToolProfile",
    "IfcScalar",
    "JobEvent",
    "JobFailure",
    "JobRecord",
    "JobState",
    "FunctionToolSource",
    "InMemoryThreadStore",
    "JsonThreadStore",
    "LocalOperationBackend",
    "LocalRuntime",
    "McpToolSource",
    "ModelContext",
    "OperationAnnotations",
    "OperationContext",
    "OperationDefinition",
    "OperationPlugin",
    "OperationBackend",
    "PluginAPI",
    "PluginManifest",
    "PluginRecord",
    "PropertyCreateChange",
    "PropertyValueChange",
    "ProviderModel",
    "QueryBatchOperation",
    "QueryElementsData",
    "QueryJobSpec",
    "RestoreJobSpec",
    "RestoreRecord",
    "RestoreResult",
    "RevisionRef",
    "RuntimeSettings",
    "SearchElementsData",
    "SourceFileRef",
    "TransactionJournal",
    "TransactionKind",
    "TransactionPhase",
    "ThreadStore",
    "ThreadStoreError",
    "ToolCall",
    "ToolDefinition",
    "ToolMiddleware",
    "ToolSource",
    "Toolset",
    "ValidationBatchOperation",
    "ValidationData",
    "ValidationIssue",
    "ValidationJobSpec",
    "WorkflowInputSpec",
    "WorkflowPlan",
    "WorkflowQueryOperation",
    "WorkflowRecord",
    "WorkflowSpec",
    "WorkflowState",
    "WorkflowStepPlan",
    "WorkflowStepRecord",
    "WorkflowStepSpec",
    "WorkflowStepState",
    "WorkflowValidationOperation",
    "Workbench",
    "WorkspaceContext",
    "WorkspaceClient",
    "__version__",
]

if TYPE_CHECKING:
    from ifc_console_agents import (
        Agent,
        AgentEvent,
        AgentEventType,
        AgentImage,
        AgentLimits,
        AgentMessage,
        AgentModel,
        AgentRole,
        AgentRunError,
        AgentRunResult,
        AgentToolCallRecord,
        AgentToolSource,
        AgentUsage,
        ApprovalDecision,
        ApprovalHandler,
        ApprovalRequest,
        CallbackApprovalHandler,
        DenyAllApprovals,
        InMemoryThreadStore,
        JsonThreadStore,
        ProviderModel,
        ThreadStore,
        ThreadStoreError,
    )

    from ifc_console.integrations import McpToolSource
    from ifc_console.runtime import (
        ConsoleRuntime,
        EmbeddedWebApp,
        IfcRuntime,
        LocalOperationBackend,
        LocalRuntime,
        OperationBackend,
        RuntimeSettings,
        WorkspaceClient,
    )
    from ifc_console.sdk import (
        Approval,
        ApprovalRecord,
        ArtifactGCPlan,
        ArtifactGCResult,
        ArtifactRef,
        AsyncWorkbench,
        AuditVerification,
        Authority,
        BatchChildRecord,
        BatchRecord,
        BatchSpec,
        BatchState,
        Capability,
        CapabilityDecision,
        ChangeSet,
        ChangeSetRecord,
        ClassificationAssignmentChange,
        CommitJobSpec,
        CommitRecord,
        CommitResult,
        Envelope,
        ErrorInfo,
        IfcConsoleError,
        IfcScalar,
        JobEvent,
        JobFailure,
        JobRecord,
        JobState,
        ModelContext,
        OperationAnnotations,
        OperationContext,
        OperationDefinition,
        OperationPlugin,
        PluginAPI,
        PluginManifest,
        PluginRecord,
        PropertyCreateChange,
        PropertyValueChange,
        QueryBatchOperation,
        QueryElementsData,
        QueryJobSpec,
        RestoreJobSpec,
        RestoreRecord,
        RestoreResult,
        RevisionRef,
        SearchElementsData,
        SourceFileRef,
        TransactionJournal,
        TransactionKind,
        TransactionPhase,
        ValidationBatchOperation,
        ValidationData,
        ValidationIssue,
        ValidationJobSpec,
        Workbench,
        WorkflowInputSpec,
        WorkflowPlan,
        WorkflowQueryOperation,
        WorkflowRecord,
        WorkflowSpec,
        WorkflowState,
        WorkflowStepPlan,
        WorkflowStepRecord,
        WorkflowStepSpec,
        WorkflowStepState,
        WorkflowValidationOperation,
        WorkspaceContext,
    )
    from ifc_console.toolsets import (
        FunctionToolSource,
        IfcToolProfile,
        ToolCall,
        ToolDefinition,
        ToolMiddleware,
        Toolset,
        ToolSource,
    )


_AGENT_EXPORTS = {
    "Agent",
    "AgentEvent",
    "AgentEventType",
    "AgentImage",
    "AgentLimits",
    "AgentMessage",
    "AgentModel",
    "AgentRole",
    "AgentRunError",
    "AgentRunResult",
    "AgentToolCallRecord",
    "AgentToolSource",
    "AgentUsage",
    "ApprovalDecision",
    "ApprovalHandler",
    "ApprovalRequest",
    "CallbackApprovalHandler",
    "DenyAllApprovals",
    "InMemoryThreadStore",
    "JsonThreadStore",
    "ProviderModel",
    "ThreadStore",
    "ThreadStoreError",
}

# Keep explicit one-release imports such as ``from ifc_console import Agent``
# working when the companion distribution is installed, without making a
# core-only ``from ifc_console import *`` try to resolve unavailable symbols.
try:
    _AGENTS_AVAILABLE = find_spec("ifc_console_agents") is not None
except (ImportError, ValueError):
    _AGENTS_AVAILABLE = False
if not _AGENTS_AVAILABLE:
    __all__ = [name for name in __all__ if name not in _AGENT_EXPORTS]

_RUNTIME_EXPORTS = {
    "ConsoleRuntime",
    "EmbeddedWebApp",
    "IfcRuntime",
    "LocalOperationBackend",
    "LocalRuntime",
    "OperationBackend",
    "RuntimeSettings",
    "WorkspaceClient",
}
_TOOLSET_EXPORTS = {
    "FunctionToolSource",
    "IfcToolProfile",
    "ToolCall",
    "ToolDefinition",
    "ToolMiddleware",
    "ToolSource",
    "Toolset",
}


def __getattr__(name: str) -> Any:
    if name in _AGENT_EXPORTS:
        try:
            import ifc_console_agents
        except ModuleNotFoundError as exc:
            if exc.name != "ifc_console_agents":
                raise
            raise AttributeError(
                f"{name} is provided by the optional ifc-console-agents package"
            ) from exc
        return getattr(ifc_console_agents, name)
    if name in _RUNTIME_EXPORTS:
        from ifc_console import runtime

        return getattr(runtime, name)
    if name in _TOOLSET_EXPORTS:
        from ifc_console import toolsets

        return getattr(toolsets, name)
    if name == "McpToolSource":
        from ifc_console.integrations import McpToolSource

        return McpToolSource
    if name in __all__ and name != "__version__":
        from ifc_console import sdk

        return getattr(sdk, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
