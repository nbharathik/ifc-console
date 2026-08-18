"""Typed data contracts for the first stable SDK operations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ifc_console.core.artifacts import ArtifactRef
from ifc_console.core.changes import ChangeSetRecord
from ifc_console.core.jobs import JobRecord


class OperationData(BaseModel):
    """Base for additive operation results during the v2 migration."""

    model_config = ConfigDict(extra="allow", frozen=True)


class SessionStatusData(OperationData):
    server: dict[str, Any]
    model: dict[str, Any]
    mode: str
    dirty: bool
    viewer: dict[str, Any]


class JobData(OperationData):
    job: JobRecord


class JobListData(OperationData):
    jobs: list[JobRecord]


class ArtifactData(OperationData):
    artifact: ArtifactRef


class ArtifactListData(OperationData):
    artifacts: list[ArtifactRef]


class ChangeSetData(OperationData):
    change_set: ChangeSetRecord


class QueryElementsData(OperationData):
    rows: list[dict[str, Any]] = Field(default_factory=list)
    unknown_classes: list[str] | None = None
    schema_name: str | None = Field(default=None, alias="schema")
    note: str | None = None


class SearchElementsData(OperationData):
    query: str
    mode: str
    results: list[dict[str, Any]] = Field(default_factory=list)


class ValidationIssue(OperationData):
    severity: str
    message: str
    attribute: str | None = None
    class_name: str | None = Field(default=None, alias="class")
    id: int | None = None
    global_id: str | None = None
    instance: str | None = None


class ValidationData(OperationData):
    valid: bool
    issue_count: int = Field(ge=0)
    returned: int = Field(ge=0)
    express_rules: bool
    by_class: dict[str, int]
    by_severity: dict[str, int]
    issues: list[ValidationIssue]
