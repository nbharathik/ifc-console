"""Composable agent primitives built on IFC Console operations."""

__version__ = "0.1.4"

from ifc_console_agents.agent import Agent, AgentRunError
from ifc_console_agents.approvals import (
    ApprovalCallback,
    CallbackApprovalHandler,
    DenyAllApprovals,
)
from ifc_console_agents.delegation import AgentToolSource
from ifc_console_agents.models import (
    AgentEvent,
    AgentEventType,
    AgentImage,
    AgentLimits,
    AgentMessage,
    AgentModel,
    AgentRole,
    AgentRunResult,
    AgentToolCallRecord,
    AgentUsage,
    ApprovalDecision,
    ApprovalHandler,
    ApprovalRequest,
    ThreadStore,
)
from ifc_console_agents.providers import ProviderModel
from ifc_console_agents.storage import InMemoryThreadStore, JsonThreadStore, ThreadStoreError

__all__ = [
    "__version__",
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
    "ApprovalCallback",
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
]
