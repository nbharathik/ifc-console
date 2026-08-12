"""Compatibility coverage for every supported Python version."""

from __future__ import annotations

import json

from ifc_console.core.batches import BatchState
from ifc_console.core.capabilities import Capability
from ifc_console.core.jobs import JobState
from ifc_console.core.transaction_journal import TransactionKind
from ifc_console.core.workflows import WorkflowState


def test_string_enums_have_stdlib_str_enum_behavior() -> None:
    values = (
        BatchState.QUEUED,
        Capability.MODEL_READ,
        JobState.QUEUED,
        TransactionKind.COMMIT,
        WorkflowState.QUEUED,
    )

    for value in values:
        assert str(value) == value.value
        assert json.dumps(value) == json.dumps(value.value)
