"""Auto mode approves console operations, never an engine's own actions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ifc_console.agents.models import ApprovalDecision, ApprovalRequest
from ifc_console.agents.panel import AutoApprovalHandler, standing_allowed


class RecordingAudit:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def record(self, event: str, **fields) -> None:
        self.events.append((event, fields))


class Asker:
    def __init__(self, approved: bool) -> None:
        self.approved = approved
        self.requests: list[ApprovalRequest] = []

    async def request(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(approved=self.approved, decided_by="person")


def _request(name: str, capabilities: tuple[str, ...] = ()) -> ApprovalRequest:
    return ApprovalRequest(
        request_id="r",
        run_id="run",
        thread_id="thread",
        tool_call_id="call",
        tool_name=name,
        arguments={},
        required_capabilities=capabilities,
    )


@pytest.fixture
def core(monkeypatch) -> SimpleNamespace:
    monkeypatch.setattr(
        "ifc_console.application.operations.build_operations", lambda core: None
    )
    return SimpleNamespace(audit=RecordingAudit(), operations={"get_session_status": object()})


async def test_a_console_operation_is_approved_without_asking(core) -> None:
    asker = Asker(approved=False)
    handler = AutoApprovalHandler(core, ask=asker)

    decision = await handler.request(_request("execute_ifc_code", ("model:mutate",)))

    assert decision.approved is True
    assert decision.decided_by == "session-autonomy"
    assert asker.requests == []
    assert core.audit.events[-1][0] == "agent_approval_auto"


async def test_a_console_operation_with_no_declared_capabilities_is_still_covered(core) -> None:
    decision = await AutoApprovalHandler(core).request(_request("get_session_status"))

    assert decision.approved is True


async def test_an_engine_native_action_goes_to_a_person(core) -> None:
    asker = Asker(approved=True)
    handler = AutoApprovalHandler(core, ask=asker)

    decision = await handler.request(_request("bash"))

    assert decision.decided_by == "person"
    assert [request.tool_name for request in asker.requests] == ["bash"]
    assert core.audit.events[-1][0] == "agent_approval_native"


async def test_an_engine_native_action_is_denied_when_nobody_can_be_asked(core) -> None:
    decision = await AutoApprovalHandler(core).request(_request("edit"))

    assert decision.approved is False
    assert "by hand" in decision.reason


@pytest.mark.parametrize(
    ("tool", "capabilities", "allowed"),
    [
        ("measure__propose_measured_value", ("artifact:write", "model:preview"), True),
        ("query_elements", ("model:read",), True),
        ("execute_ifc_code", ("code:execute", "model:mutate"), False),
        ("save_ifc_file", ("model:commit",), False),
        ("bash", (), False),
    ],
)
def test_only_low_risk_tools_may_be_allowed_for_a_conversation(
    tool: str, capabilities: tuple[str, ...], allowed: bool
) -> None:
    assert standing_allowed(_request(tool, capabilities)) is allowed


async def test_a_standing_decision_answers_the_same_call_without_asking() -> None:
    import asyncio

    from ifc_console.agents.panel import AgentPanelState, PanelApprovalHandler, standing_key

    state = AgentPanelState()
    request = _request("measure__propose_measured_value", ("artifact:write", "model:preview"))
    state.standing.add(standing_key(request))
    handler = PanelApprovalHandler(state)

    decision = await asyncio.wait_for(handler.request(request), timeout=1)

    assert decision.approved is True
    assert decision.decided_by == "standing-decision"
    assert state.pending_approvals == {}
    # another conversation, or a wider capability set, still stops and asks
    other = request.model_copy(update={"thread_id": "another"})
    wider = request.model_copy(update={"required_capabilities": ("model:mutate",)})
    assert standing_key(other) not in state.standing
    assert standing_key(wider) not in state.standing
    state.forget("thread")
    assert state.standing == set()
