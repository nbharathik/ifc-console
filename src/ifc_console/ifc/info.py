"""get_ifc_project_info builder: schema, units, counts, materials, header."""

from __future__ import annotations

import contextlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMMON_CLASSES = [
    "IfcWall", "IfcSlab", "IfcColumn", "IfcBeam", "IfcDoor", "IfcWindow",
    "IfcStair", "IfcRamp", "IfcRoof", "IfcSpace", "IfcCovering", "IfcRailing",
    "IfcCurtainWall", "IfcPlate", "IfcMember", "IfcFooting", "IfcPile",
    "IfcFurnishingElement", "IfcFlowSegment", "IfcFlowTerminal",
]

_UNIT_KEYS = {"LENGTHUNIT": "length", "AREAUNIT": "area", "VOLUMEUNIT": "volume"}


def _units(ifc: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        assignment = (ifc.by_type("IfcUnitAssignment") or [None])[0]
        if assignment is None:
            return out
        for unit in assignment.Units or ():
            unit_type = getattr(unit, "UnitType", None)
            if unit_type not in _UNIT_KEYS:
                continue
            if unit.is_a("IfcSIUnit"):
                prefix = getattr(unit, "Prefix", None) or ""
                out[_UNIT_KEYS[unit_type]] = f"{prefix}{unit.Name}"
            else:
                name = getattr(unit, "Name", None)
                if name:
                    out[_UNIT_KEYS[unit_type]] = str(name)
    except Exception:
        pass
    return out


def _header(ifc: Any) -> dict[str, Any]:
    try:
        fn = ifc.header.file_name
        return {
            "originating_system": getattr(fn, "originating_system", None),
            "preprocessor_version": getattr(fn, "preprocessor_version", None),
            "timestamp": getattr(fn, "time_stamp", None),
            "authors": list(getattr(fn, "author", ()) or ()),
            "organizations": list(getattr(fn, "organization", ()) or ()),
        }
    except Exception:
        return {}


def _materials(ifc: Any, top: int = 20) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        for mat in ifc.by_type("IfcMaterial"):
            try:
                used_by = len(ifc.get_inverse(mat))
            except Exception:
                used_by = 0
            rows.append({"name": getattr(mat, "Name", None), "used_by": used_by})
    except Exception:
        return []
    rows.sort(key=lambda r: -r["used_by"])
    return rows[:top]


def build_project_info(ifc: Any, path: Path | None) -> dict[str, Any]:
    file_block: dict[str, Any] = {}
    if path is not None and path.exists():
        stat = path.stat()
        file_block = {
            "name": path.name,
            "path": str(path),
            "size_bytes": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        }

    project = (ifc.by_type("IfcProject") or [None])[0]
    project_block = None
    if project is not None:
        project_block = {
            "global_id": getattr(project, "GlobalId", None),
            "name": getattr(project, "Name", None),
            "description": getattr(project, "Description", None),
            "phase": getattr(project, "Phase", None),
        }

    entity_counts: dict[str, int] = {}
    for cls in COMMON_CLASSES:
        try:
            n = len(ifc.by_type(cls))
        except Exception:
            n = 0
        if n:
            entity_counts[cls] = n
    with contextlib.suppress(Exception):
        entity_counts["total_products"] = len(ifc.by_type("IfcProduct"))

    def _count(cls: str) -> int:
        try:
            return len(ifc.by_type(cls))
        except Exception:
            return 0

    classifications = []
    with contextlib.suppress(Exception):
        classifications = [
            getattr(c, "Name", None) for c in ifc.by_type("IfcClassification")
        ]

    return {
        "file": file_block,
        "schema": getattr(ifc, "schema", None),
        "project": project_block,
        "units": _units(ifc),
        "spatial": {
            "sites": _count("IfcSite"),
            "buildings": _count("IfcBuilding"),
            "storeys": _count("IfcBuildingStorey"),
            "spaces": _count("IfcSpace"),
        },
        "entity_counts": entity_counts,
        "materials": _materials(ifc),
        "classifications": classifications,
        "header": _header(ifc),
    }
