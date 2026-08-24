"""The measurement preset, plus the report shape a measurement run produces.

The agent itself is data now: see :mod:`ifc_console.agents.presets`. This
module keeps the measurement-specific value types and the CSV writer, and
re-exports the preset so existing imports keep working.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ifc_console.agents.agent import Agent
from ifc_console.agents.models import AgentLimits
from ifc_console.agents.presets import MEASUREMENT, PresetPack
from ifc_console.agents.proposals import MeasurementMetric, build_proposal_source

BLOCKS = MEASUREMENT.blocks
ROLE = MEASUREMENT.role
INSTRUCTIONS = ROLE


class MeasuredElement(BaseModel):
    global_id: str
    name: str | None = None
    value: float | None = None
    unit: str | None = None
    method: str
    source: str | None = None
    flags: list[str] = Field(default_factory=list)


class MeasurementReport(BaseModel):
    """The structured final answer a measurement run produces."""

    metric: str
    scope: str
    method: str
    unit: str | None = None
    source: str | None = None
    elements: list[MeasuredElement]
    notes: str | None = None


PACK = PresetPack(MEASUREMENT)


async def build_agent(
    runtime: Any,
    *,
    model: Any,
    viewer: bool = False,
    proposal_source: Any = None,
    thread_store: Any = None,
    limits: AgentLimits | None = None,
    approval_handler: Any = None,
    instructions: str = "",
    model_label: str = "",
) -> Agent:
    """The measurement agent over a LocalRuntime or ConsoleRuntime."""
    del proposal_source  # the proposal block owns those tools now
    return await PACK.build(
        runtime,
        model=model,
        viewer=viewer,
        instructions=instructions,
        model_label=model_label,
        thread_store=thread_store,
        approval_handler=approval_handler,
        limits=limits,
    )


def report_to_csv(report: MeasurementReport, path: str | Path) -> Path:
    """Write the per-element rows of a report as a CSV file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["global_id", "name", "value", "unit", "method", "source", "flags"])
        for element in report.elements:
            writer.writerow(
                [
                    element.global_id,
                    element.name or "",
                    element.value if element.value is not None else "",
                    element.unit or report.unit or "",
                    element.method,
                    element.source or report.source or "",
                    ";".join(element.flags),
                ]
            )
    return target


__all__ = [
    "BLOCKS",
    "INSTRUCTIONS",
    "PACK",
    "ROLE",
    "MeasuredElement",
    "MeasurementMetric",
    "MeasurementReport",
    "build_agent",
    "build_proposal_source",
    "report_to_csv",
]
