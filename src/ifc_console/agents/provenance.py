"""Where AI-assisted values go, and how they stay identifiable forever.

A model that cannot tell which values a language model produced is a model
nobody can trust again. Every value an agent proposes lands in a property set
under one reserved prefix, and carries a machine-readable provenance record
naming the agent, the model, the method, and the document it came from. A
reviewer, a downstream tool, or a later run can find and remove every one of
them with a prefix match, so the AI-assisted layer is always separable from
the authored model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

# One reserved namespace. Everything an agent writes begins with this.
PREFIX = "IfcConsole_AI_"
MEASUREMENT_PSET = f"{PREFIX}Measurements"
PROPERTY_PSET = f"{PREFIX}Properties"
PROVENANCE_PSET = f"{PREFIX}Provenance"
# Kept for models written by releases that stored one marker per element.
PROVENANCE_PROPERTY = "AI_Provenance"
PROVENANCE_PROPERTY_PREFIX = f"{PROVENANCE_PROPERTY}_"
MARKER_PROPERTY = "AI_Generated"

# Property names an agent may create. Deliberately conservative: no colons, no
# dots, nothing that could be read as a path or a selector.
PROPERTY_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,62}$")

ALLOWED_PSETS = (MEASUREMENT_PSET, PROPERTY_PSET)

# metric -> (property name, IFC nominal type)
MEASUREMENT_PROPERTIES: dict[str, tuple[str, str]] = {
    "length": ("MeasuredLength", "IfcLengthMeasure"),
    "width": ("MeasuredWidth", "IfcLengthMeasure"),
    "height": ("MeasuredHeight", "IfcLengthMeasure"),
    "thickness": ("MeasuredThickness", "IfcLengthMeasure"),
    "depth": ("MeasuredDepth", "IfcLengthMeasure"),
    "perimeter": ("MeasuredPerimeter", "IfcLengthMeasure"),
    "area": ("MeasuredArea", "IfcAreaMeasure"),
    "volume": ("MeasuredVolume", "IfcVolumeMeasure"),
    "distance": ("MeasuredDistance", "IfcLengthMeasure"),
    "count": ("MeasuredCount", "IfcCountMeasure"),
}

NOMINAL_TYPES = ("IfcText", "IfcLabel", "IfcReal", "IfcInteger", "IfcBoolean")


def is_ai_authored(pset_name: str) -> bool:
    """True for any property set this project writes on an agent's behalf."""
    return str(pset_name).startswith(PREFIX)


def validate_property_name(name: str) -> str:
    cleaned = str(name).strip()
    if not PROPERTY_NAME.match(cleaned):
        raise ValueError(
            "property names must start with a letter and use only letters, digits, "
            "and underscores (max 63 characters)"
        )
    return cleaned


def validate_pset(name: str) -> str:
    cleaned = str(name).strip()
    if cleaned not in ALLOWED_PSETS:
        raise ValueError(
            f"agents may only write into {' or '.join(ALLOWED_PSETS)}; "
            "authored property sets stay under human control"
        )
    return cleaned


def provenance_property_name(pset_name: str, property_name: str) -> str:
    """Stable marker name for one AI-authored target property."""
    clean_pset = validate_pset(pset_name)
    clean_property = validate_property_name(property_name)
    scope = "M" if clean_pset == MEASUREMENT_PSET else "P"
    return f"{PROVENANCE_PROPERTY_PREFIX}{scope}_{clean_property}"


def is_provenance_property(name: str) -> bool:
    """True for legacy and per-property provenance markers."""
    clean = str(name)
    return clean == PROVENANCE_PROPERTY or clean.startswith(PROVENANCE_PROPERTY_PREFIX)


@dataclass(frozen=True)
class Provenance:
    """The record stored beside every AI-assisted value."""

    agent: str
    property_name: str
    pset: str
    method: str
    model: str = ""
    source: str = ""
    unit: str = ""
    confidence: str = ""
    instructions: str = ""
    change_set: str = ""
    written_at: str = ""
    proposal_id: str = ""

    def with_change_set(self, change_set_id: str) -> Provenance:
        return Provenance(**{**self.__dict__, "change_set": change_set_id})

    def with_proposal(self, proposal_id: str) -> Provenance:
        """Bind the record to one preview without creating a hash cycle."""
        return Provenance(**{**self.__dict__, "proposal_id": proposal_id})

    def to_json(self) -> str:
        """Compact, stable, and short enough for an IfcText property."""
        payload = {
            "v": 1,
            "ai_generated": True,
            "agent": self.agent,
            "property": f"{self.pset}.{self.property_name}",
            "method": self.method,
            "model": self.model,
            "source": self.source,
            "unit": self.unit,
            "confidence": self.confidence,
            "instructions": self.instructions[:240],
            "change_set": self.change_set,
            "written_at": self.written_at or stamp(),
            "proposal_id": self.proposal_id,
            "tool": "ifc-console",
        }
        return json.dumps({k: v for k, v in payload.items() if v not in ("", None)})


def stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def measurement_property(metric: str) -> tuple[str, str]:
    try:
        return MEASUREMENT_PROPERTIES[metric]
    except KeyError:
        raise ValueError(
            f"unknown metric {metric!r}; choose one of {', '.join(sorted(MEASUREMENT_PROPERTIES))}"
        ) from None


def read_ai_properties(ifc: Any, *, limit: int = 500) -> dict[str, Any]:
    """Every element in an open model carrying AI-authored property sets.

    Pure read. The report is what a reviewer needs to accept or strip the
    AI-assisted layer: which elements, which properties, and the provenance
    record for each.
    """
    import ifcopenshell.util.element as element_util

    rows: list[dict[str, Any]] = []
    psets_seen: dict[str, int] = {}
    truncated = False
    for element in ifc.by_type("IfcObject"):
        try:
            sets = element_util.get_psets(element, psets_only=True)
        except Exception:
            continue
        marked = {name: values for name, values in sets.items() if is_ai_authored(name)}
        if not marked:
            continue
        if len(rows) >= limit:
            truncated = True
            break
        provenance: dict[str, Any] | None = None
        provenance_records: list[tuple[str, dict[str, Any]]] = []
        provenance_by_property: dict[str, dict[str, Any]] = {}
        provenance_ranks: dict[str, tuple[str, bool, int]] = {}
        properties: dict[str, Any] = {}
        for name, values in marked.items():
            psets_seen[name] = psets_seen.get(name, 0) + 1
            for key, value in values.items():
                if key == "id":
                    continue
                if name == PROVENANCE_PSET and is_provenance_property(key):
                    try:
                        parsed = json.loads(str(value))
                        record = parsed if isinstance(parsed, dict) else {"raw": str(value)[:400]}
                    except (ValueError, TypeError):
                        record = {"raw": str(value)[:400]}
                    target = record.get("property")
                    target_key = target if isinstance(target, str) and target else key
                    rank = (
                        str(record.get("written_at", "")),
                        key != PROVENANCE_PROPERTY,
                        len(provenance_records),
                    )
                    if rank > provenance_ranks.get(target_key, ("", False, -1)):
                        provenance_by_property[target_key] = record
                        provenance_ranks[target_key] = rank
                    provenance_records.append((key, record))
                    continue
                properties[f"{name}.{key}"] = value
        if provenance_records:
            provenance = max(
                enumerate(provenance_records),
                key=lambda item: (
                    str(item[1][1].get("written_at", "")),
                    item[1][0] != PROVENANCE_PROPERTY,
                    item[0],
                ),
            )[1][1]
        rows.append(
            {
                "global_id": getattr(element, "GlobalId", None),
                "class": element.is_a(),
                "name": getattr(element, "Name", None),
                "properties": properties,
                "provenance": provenance,
                "provenance_by_property": provenance_by_property,
            }
        )
    return {
        "prefix": PREFIX,
        "elements": rows,
        "property_sets": psets_seen,
        "truncated": truncated,
    }


__all__ = [
    "ALLOWED_PSETS",
    "MARKER_PROPERTY",
    "MEASUREMENT_PROPERTIES",
    "MEASUREMENT_PSET",
    "NOMINAL_TYPES",
    "PREFIX",
    "PROPERTY_PSET",
    "PROVENANCE_PROPERTY",
    "PROVENANCE_PROPERTY_PREFIX",
    "PROVENANCE_PSET",
    "Provenance",
    "is_ai_authored",
    "is_provenance_property",
    "measurement_property",
    "provenance_property_name",
    "read_ai_properties",
    "stamp",
    "validate_property_name",
    "validate_pset",
]
