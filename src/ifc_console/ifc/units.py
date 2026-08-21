"""Length-unit facts and conversions between file units and SI metres.

Stored properties and quantities live in the file's project units; meshes from
the geometry iterator are always SI metres. Every measurement result reports
both, so callers never have to guess which one they are looking at.
"""

from __future__ import annotations

from typing import Any

from ifc_console.ifc.info import _units


def unit_info(ifc: Any) -> dict[str, Any]:
    """The file's length unit and its factor to SI metres."""
    try:
        import ifcopenshell.util.unit as unit_util

        factor = float(unit_util.calculate_unit_scale(ifc))
    except Exception:
        factor = 1.0
    if factor <= 0:
        factor = 1.0
    return {
        "length_unit": _units(ifc).get("length"),
        "si_length_unit": "METRE",
        "to_si_factor": factor,
    }


def file_to_si(value: float, factor: float, power: int = 1) -> float:
    return value * (factor**power)


def si_to_file(value: float, factor: float, power: int = 1) -> float:
    scale = factor**power
    return value / scale if scale else value


__all__ = ["file_to_si", "si_to_file", "unit_info"]
