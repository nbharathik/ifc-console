"""Host-owned approval policies for protected agent tool calls."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from ifc_console_agents.models import ApprovalDecision, ApprovalRequest


class DenyAllApprovals:
    """Safe default: protected tools remain unavailable without a host policy."""

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        return ApprovalDecision(
            approved=False,
            decided_by="default-policy",
            reason=f"no approval handler is configured for {request.tool_name}",
        )


ApprovalCallback = Callable[
    [ApprovalRequest],
    ApprovalDecision | bool | Awaitable[ApprovalDecision | bool],
]


class CallbackApprovalHandler:
    """Adapt an application callback, RBAC check, or UI decision service."""

    def __init__(self, callback: ApprovalCallback) -> None:
        self.callback = callback

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        value = self.callback(request)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, ApprovalDecision):
            return value
        return ApprovalDecision(approved=bool(value), decided_by="callback")


__all__ = ["ApprovalCallback", "CallbackApprovalHandler", "DenyAllApprovals"]
