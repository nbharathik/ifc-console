"""Typed permissions shared by operations, policy, SDK, and workers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal


class Capability(StrEnum):
    MODEL_READ = "model:read"
    MODEL_PREVIEW = "model:preview"
    MODEL_MUTATE = "model:mutate"
    MODEL_APPROVE = "model:approve"
    MODEL_COMMIT = "model:commit"
    MODEL_RESTORE = "model:restore"
    FILE_READ = "file:read"
    FILE_WRITE = "file:write"
    ARTIFACT_READ = "artifact:read"
    ARTIFACT_WRITE = "artifact:write"
    AUDIT_READ = "audit:read"
    JOB_READ = "job:read"
    JOB_SUBMIT = "job:submit"
    JOB_CANCEL = "job:cancel"
    GENERATED_CODE = "code:execute"
    GEOMETRY = "geometry:compute"
    KNOWLEDGE_READ = "knowledge:read"
    SESSION_MANAGE = "session:manage"
    VIEWER_READ = "viewer:read"
    VIEWER_CONTROL = "viewer:control"
    NETWORK = "network:access"
    PROCESS = "process:execute"


Authority = Literal["tool", "caller", "worker"]


@dataclass(frozen=True)
class CapabilityDecision:
    """Serializable explanation of one policy decision."""

    allowed: bool
    profile: str
    authority: Authority
    required: tuple[Capability, ...]
    granted: tuple[Capability, ...]
    missing: tuple[Capability, ...]
    rule: str
    version: Literal["1"] = "1"


# Ask/edit remain ergonomic profiles. The permission vocabulary is independent
# so a later workspace or enterprise policy can grant a narrower set directly.
ASK_CAPABILITIES = frozenset(
    {
        Capability.MODEL_READ,
        Capability.MODEL_PREVIEW,
        Capability.FILE_READ,
        Capability.FILE_WRITE,
        Capability.ARTIFACT_READ,
        Capability.ARTIFACT_WRITE,
        Capability.AUDIT_READ,
        Capability.JOB_READ,
        Capability.JOB_SUBMIT,
        Capability.JOB_CANCEL,
        Capability.GENERATED_CODE,
        Capability.GEOMETRY,
        Capability.KNOWLEDGE_READ,
        Capability.SESSION_MANAGE,
        Capability.VIEWER_READ,
        Capability.VIEWER_CONTROL,
    }
)

EDIT_CAPABILITIES = ASK_CAPABILITIES | frozenset(
    {
        Capability.MODEL_MUTATE,
        Capability.MODEL_COMMIT,
        Capability.MODEL_RESTORE,
    }
)

CALLER_ONLY_CAPABILITIES = frozenset({Capability.MODEL_APPROVE})
SYSTEM_CAPABILITIES = frozenset({Capability.NETWORK, Capability.PROCESS})


def normalize_capabilities(
    values: tuple[Capability | str, ...] | list[Capability | str],
) -> tuple[Capability, ...]:
    """Canonicalize a capability collection for stable schemas and audit."""
    return tuple(sorted({Capability(value) for value in values}, key=lambda item: item.value))
