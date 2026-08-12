"""Typed capability profiles and operation enforcement."""

from __future__ import annotations

from pathlib import Path

from ifc_console.app import AppCore
from ifc_console.application.operations import OperationService
from ifc_console.core.capabilities import Capability
from ifc_console.core.operations import OperationRegistry
from ifc_console.core.results import Envelope, ToolError
from ifc_console.policy.modes import Mode, PolicyEngine
from ifc_console.settings import SettingsStore


def test_ask_and_edit_are_explicit_compatibility_profiles() -> None:
    policy = PolicyEngine(Mode.ASK, allow_system_access=False)

    ask = policy.evaluate([Capability.MODEL_READ, Capability.MODEL_PREVIEW])
    assert ask.allowed is True
    assert ask.profile == "ask:tool"
    assert policy.evaluate([Capability.MODEL_COMMIT]).allowed is False
    assert policy.evaluate([Capability.MODEL_APPROVE]).allowed is False

    policy.mode = Mode.EDIT
    assert policy.evaluate([Capability.MODEL_MUTATE]).allowed is True
    assert policy.evaluate([Capability.MODEL_COMMIT]).allowed is False
    assert policy.evaluate([Capability.MODEL_COMMIT], authority="caller").allowed is True
    policy.allow_ai_save = True
    assert policy.evaluate([Capability.MODEL_COMMIT]).allowed is True
    assert policy.evaluate([Capability.MODEL_APPROVE]).allowed is False
    assert policy.evaluate([Capability.MODEL_APPROVE], authority="caller").allowed is True


def test_system_capabilities_need_both_edit_mode_and_explicit_setting() -> None:
    policy = PolicyEngine(Mode.EDIT, allow_system_access=False)
    assert policy.evaluate([Capability.NETWORK]).allowed is False

    policy.allow_system_access = True
    decision = policy.evaluate([Capability.NETWORK, Capability.PROCESS])
    assert decision.allowed is False

    policy.allow_ai_save = True
    decision = policy.evaluate([Capability.NETWORK, Capability.PROCESS])
    assert decision.allowed is True

    policy.mode = Mode.ASK
    assert policy.evaluate([Capability.NETWORK]).allowed is False


def test_ai_save_denial_has_a_specific_actionable_error() -> None:
    policy = PolicyEngine(Mode.EDIT, allow_system_access=False)

    try:
        policy.require([Capability.MODEL_COMMIT], action="save_ifc_file")
    except ToolError as exc:
        assert exc.code == "AI_SAVE_DISABLED"
        assert "/save" in exc.hint
    else:
        raise AssertionError("AI model commit should be denied by default")


async def test_operation_service_enforces_declared_capabilities(tmp_path: Path) -> None:
    core = AppCore(SettingsStore(home=tmp_path, project_dir=tmp_path, env={}), mode=Mode.ASK)
    registry = OperationRegistry()

    @registry.tool(required_capabilities=(Capability.MODEL_MUTATE,))
    async def guarded() -> Envelope:
        raise AssertionError("a denied handler must not execute")

    core.start_audit()
    try:
        result = await OperationService(core, registry).call("guarded", {})
        assert result.ok is False
        assert result.error is not None
        assert result.error.code == "ASK_MODE_BLOCKED"
        assert result.meta["correlation_id"].startswith("corr-")

        policy_event = next(
            event for event in core.audit.tail(10) if event["ev"] == "policy_decision"
        )
        assert policy_event["allowed"] is False
        assert policy_event["required"] == ["model:mutate"]
        assert policy_event["correlation_id"] == result.meta["correlation_id"]
    finally:
        core.shutdown()


def test_builtin_operation_definitions_are_self_describing(tmp_path: Path) -> None:
    from ifc_console.application.operations import build_operations

    core = AppCore(SettingsStore(home=tmp_path, project_dir=tmp_path, env={}))
    try:
        definitions = build_operations(core).definitions()
        assert definitions
        assert all(definition.required_capabilities for definition in definitions)
        by_name = {definition.name: definition for definition in definitions}
        assert by_name["execute_ifc_code"].required_capabilities == (
            Capability.GENERATED_CODE,
        )
        assert Capability.MODEL_COMMIT in by_name["save_ifc_file"].required_capabilities
        assert Capability.MODEL_APPROVE not in {
            capability
            for definition in definitions
            for capability in definition.required_capabilities
        }
    finally:
        core.shutdown()
