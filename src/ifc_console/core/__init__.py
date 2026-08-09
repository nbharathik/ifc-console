"""Transport-neutral contracts with lazy public exports.

Importing one lightweight contract must not initialize every Pydantic model in
the package. This matters to the CLI, which imports the mode enum for argument
parsing and has a strict cold-start budget.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORTS = {
    "Approval": "changes",
    "ApprovalRecord": "changes",
    "ArtifactData": "operation_data",
    "ArtifactGCPlan": "artifacts",
    "ArtifactGCResult": "artifacts",
    "ArtifactListData": "operation_data",
    "ArtifactRef": "artifacts",
    "Authority": "capabilities",
    "BatchChildRecord": "batches",
    "BatchRecord": "batches",
    "BatchSpec": "batches",
    "BatchState": "batches",
    "Capability": "capabilities",
    "CapabilityDecision": "capabilities",
    "ChangeSet": "changes",
    "ChangeSetData": "operation_data",
    "ChangeSetRecord": "changes",
    "ClassificationAssignmentChange": "changes",
    "CommitRecord": "changes",
    "CommitResult": "changes",
    "CommitJobSpec": "jobs",
    "Envelope": "results",
    "ErrorInfo": "results",
    "IfcScalar": "changes",
    "JobData": "operation_data",
    "JobEvent": "jobs",
    "JobFailure": "jobs",
    "JobListData": "operation_data",
    "JobRecord": "jobs",
    "JobState": "jobs",
    "ModelContext": "context",
    "OperationAnnotations": "operations",
    "OperationContext": "context",
    "OperationDefinition": "operations",
    "OperationImage": "operations",
    "OperationRegistry": "operations",
    "OperationSpec": "operations",
    "PropertyValueChange": "changes",
    "PropertyCreateChange": "changes",
    "QueryElementsData": "operation_data",
    "QueryBatchOperation": "batches",
    "QueryJobSpec": "jobs",
    "RevisionId": "revisions",
    "RevisionRef": "revisions",
    "RestoreRecord": "changes",
    "RestoreResult": "changes",
    "RestoreJobSpec": "jobs",
    "SessionStatusData": "operation_data",
    "SourceFileRef": "jobs",
    "ToolError": "results",
    "TransactionJournal": "transaction_journal",
    "TransactionKind": "transaction_journal",
    "TransactionPhase": "transaction_journal",
    "ValidationData": "operation_data",
    "ValidationBatchOperation": "batches",
    "ValidationIssue": "operation_data",
    "ValidationJobSpec": "jobs",
    "WorkflowInputSpec": "workflows",
    "WorkflowPlan": "workflows",
    "WorkflowQueryOperation": "workflows",
    "WorkflowRecord": "workflows",
    "WorkflowSpec": "workflows",
    "WorkflowState": "workflows",
    "WorkflowStepPlan": "workflows",
    "WorkflowStepRecord": "workflows",
    "WorkflowStepSpec": "workflows",
    "WorkflowStepState": "workflows",
    "WorkflowValidationOperation": "workflows",
    "WorkspaceContext": "context",
}

__all__ = [
    "Approval",
    "ApprovalRecord",
    "ArtifactData",
    "ArtifactGCPlan",
    "ArtifactGCResult",
    "ArtifactListData",
    "ArtifactRef",
    "Authority",
    "BatchChildRecord",
    "BatchRecord",
    "BatchSpec",
    "BatchState",
    "Capability",
    "CapabilityDecision",
    "ChangeSet",
    "ChangeSetData",
    "ChangeSetRecord",
    "ClassificationAssignmentChange",
    "CommitRecord",
    "CommitResult",
    "CommitJobSpec",
    "Envelope",
    "ErrorInfo",
    "IfcScalar",
    "JobData",
    "JobEvent",
    "JobFailure",
    "JobListData",
    "JobRecord",
    "JobState",
    "ModelContext",
    "OperationAnnotations",
    "OperationContext",
    "OperationDefinition",
    "OperationImage",
    "OperationRegistry",
    "OperationSpec",
    "PropertyValueChange",
    "PropertyCreateChange",
    "QueryElementsData",
    "QueryBatchOperation",
    "QueryJobSpec",
    "RevisionId",
    "RevisionRef",
    "RestoreRecord",
    "RestoreResult",
    "RestoreJobSpec",
    "SessionStatusData",
    "SourceFileRef",
    "ToolError",
    "TransactionJournal",
    "TransactionKind",
    "TransactionPhase",
    "ValidationData",
    "ValidationBatchOperation",
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
    "WorkspaceContext",
]


if TYPE_CHECKING:
    from ifc_console.core.artifacts import ArtifactGCPlan, ArtifactGCResult, ArtifactRef
    from ifc_console.core.batches import (
        BatchChildRecord,
        BatchRecord,
        BatchSpec,
        BatchState,
        QueryBatchOperation,
        ValidationBatchOperation,
    )
    from ifc_console.core.capabilities import Authority, Capability, CapabilityDecision
    from ifc_console.core.changes import (
        Approval,
        ApprovalRecord,
        ChangeSet,
        ChangeSetRecord,
        ClassificationAssignmentChange,
        CommitRecord,
        CommitResult,
        IfcScalar,
        PropertyCreateChange,
        PropertyValueChange,
        RestoreRecord,
        RestoreResult,
    )
    from ifc_console.core.context import ModelContext, OperationContext, WorkspaceContext
    from ifc_console.core.jobs import (
        CommitJobSpec,
        JobEvent,
        JobFailure,
        JobRecord,
        JobState,
        QueryJobSpec,
        RestoreJobSpec,
        SourceFileRef,
        ValidationJobSpec,
    )
    from ifc_console.core.operation_data import (
        ArtifactData,
        ArtifactListData,
        ChangeSetData,
        JobData,
        JobListData,
        QueryElementsData,
        SessionStatusData,
        ValidationData,
        ValidationIssue,
    )
    from ifc_console.core.operations import (
        OperationAnnotations,
        OperationDefinition,
        OperationImage,
        OperationRegistry,
        OperationSpec,
    )
    from ifc_console.core.results import Envelope, ErrorInfo, ToolError
    from ifc_console.core.revisions import RevisionId, RevisionRef
    from ifc_console.core.transaction_journal import (
        TransactionJournal,
        TransactionKind,
        TransactionPhase,
    )
    from ifc_console.core.workflows import (
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
    )


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f"{__name__}.{module_name}"), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
