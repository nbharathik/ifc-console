"""Rotation-independent geometry signatures and explainable comparisons."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

SIGNATURE_VERSION = "1.0"

_CLASS_FAMILIES = {
    "IfcBeam": "linear_member",
    "IfcColumn": "linear_member",
    "IfcMember": "linear_member",
    "IfcPile": "linear_member",
    "IfcWall": "wall",
    "IfcWallStandardCase": "wall",
    "IfcSlab": "plate",
    "IfcPlate": "plate",
    "IfcFooting": "foundation",
    "IfcDoor": "opening_element",
    "IfcWindow": "opening_element",
}


def _measurement_map(record: dict[str, Any]) -> dict[str, Any]:
    return {
        item.get("id"): item
        for item in record.get("measurements") or ()
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _measurement_value(items: dict[str, Any], name: str) -> float | None:
    value = (items.get(name) or {}).get("value_si")
    if isinstance(value, (int, float)) and np.isfinite(value):
        return float(value)
    return None


def _profile_family(record: dict[str, Any]) -> str | None:
    for solid in record.get("swept_solids") or ():
        profile = solid.get("profile") or {}
        family = profile.get("family")
        if family:
            return str(family)
        cls = profile.get("class")
        if cls:
            return str(cls)
    for material in record.get("material_profiles") or ():
        family = (material.get("profile") or {}).get("family")
        if family:
            return str(family)
    return None


def _material_key(material: Any) -> str | None:
    if material is None:
        return None
    if isinstance(material, str):
        return material.casefold()
    if isinstance(material, dict):
        values = []
        for key in ("name", "Name", "material", "type", "class"):
            value = material.get(key)
            if value:
                values.append(str(value).casefold())
        return ":".join(values) or None
    return str(material).casefold()


def _normalized(values: list[float | None]) -> list[float] | None:
    if any(value is None or value <= 0 for value in values):
        return None
    numeric = [float(value) for value in values if value is not None]
    scale = max(numeric)
    return [round(value / scale, 6) for value in numeric]


def _canonical_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_geometry_signature(record: dict[str, Any]) -> dict[str, Any]:
    """Build a compact intrinsic signature from one v2 analysis record."""
    measurements = _measurement_map(record)
    extents = [
        _measurement_value(measurements, "envelope.overall_length"),
        _measurement_value(measurements, "envelope.overall_width"),
        _measurement_value(measurements, "envelope.overall_height"),
    ]
    if any(value is None for value in extents):
        legacy = record.get("dimensions") or {}
        extents = [
            (legacy.get("length") or {}).get("si"),
            (legacy.get("width") or {}).get("si"),
            (legacy.get("height") or {}).get("si"),
        ]
    section_analysis = record.get("section_analysis") or {}
    dominant = (section_analysis.get("representative_sections") or {}).get("dominant") or {}
    descriptor = dominant.get("descriptor") or {}
    section_shape = _normalized([descriptor.get("width_si"), descriptor.get("height_si")])
    topology = record.get("topology") or {}
    object_info = record.get("object") or record
    element_type = object_info.get("type") or record.get("type")
    if isinstance(element_type, dict):
        type_key = (
            ":".join(
                str(value).casefold()
                for value in (element_type.get("class"), element_type.get("name"))
                if value
            )
            or None
        )
    else:
        type_key = str(element_type).casefold() if element_type else None
    ifc_class = object_info.get("class") or record.get("class")
    payload: dict[str, Any] = {
        "version": SIGNATURE_VERSION,
        "class_family": _CLASS_FAMILIES.get(str(ifc_class), str(ifc_class)),
        "ifc_class": ifc_class,
        "type_key": type_key,
        "geometry_family": record.get("geometry_family"),
        "profile_family": _profile_family(record),
        "material_key": _material_key(record.get("material")),
        "normalized_extents": _normalized(extents),
        "normalized_section_bounds": section_shape,
        "section_loops": descriptor.get("loop_count"),
        "section_holes": descriptor.get("hole_count"),
        "components": topology.get("connected_components"),
        "closed_shells": topology.get("closed_shells"),
        "through_holes": topology.get("through_holes"),
        "section_variation": section_analysis.get("variation"),
    }
    payload["fingerprint"] = _canonical_hash(payload)
    return payload


def _vector_similarity(left: Any, right: Any) -> float | None:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return None
    try:
        a, b = np.asarray(left, dtype=np.float64), np.asarray(right, dtype=np.float64)
    except (TypeError, ValueError):
        return None
    if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        return None
    distance = float(np.linalg.norm(a - b) / max(np.sqrt(len(a)), 1.0))
    return max(0.0, 1.0 - distance)


def compare_geometry_signatures(
    exemplar: dict[str, Any],
    candidate: dict[str, Any],
    *,
    threshold: float = 0.85,
) -> dict[str, Any]:
    """Compare signatures with weighted, explainable signals."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between zero and one")
    if "version" not in exemplar or "version" not in candidate:
        raise ValueError("both signatures must include a version")
    signals: list[dict[str, Any]] = []

    def categorical(name: str, weight: float, label: str) -> None:
        left, right = exemplar.get(name), candidate.get(name)
        if left is None or right is None:
            return
        signals.append(
            {
                "signal": name,
                "label": label,
                "weight": weight,
                "similarity": 1.0 if left == right else 0.0,
                "exemplar": left,
                "candidate": right,
            }
        )

    categorical("class_family", 0.1, "physical class family")
    categorical("type_key", 0.1, "IFC type")
    categorical("geometry_family", 0.15, "representation family")
    categorical("profile_family", 0.18, "profile family")
    categorical("material_key", 0.07, "material")
    categorical("components", 0.08, "component count")
    categorical("closed_shells", 0.04, "closed shell count")
    categorical("through_holes", 0.04, "through-hole topology")
    categorical("section_variation", 0.04, "longitudinal section variation")
    for name, weight, label in (
        ("normalized_extents", 0.15, "normalized proportions"),
        ("normalized_section_bounds", 0.05, "normalized section bounds"),
    ):
        similarity = _vector_similarity(exemplar.get(name), candidate.get(name))
        if similarity is not None:
            signals.append(
                {
                    "signal": name,
                    "label": label,
                    "weight": weight,
                    "similarity": round(similarity, 6),
                    "exemplar": exemplar.get(name),
                    "candidate": candidate.get(name),
                }
            )

    total_weight = sum(signal["weight"] for signal in signals)
    score = (
        sum(signal["weight"] * signal["similarity"] for signal in signals) / total_weight
        if total_weight
        else 0.0
    )
    reasons = [signal["label"] for signal in signals if signal["similarity"] >= 0.9]
    mismatches = [signal["label"] for signal in signals if signal["similarity"] < 0.75]
    hard_compatible = True
    if exemplar.get("class_family") and candidate.get("class_family"):
        hard_compatible = exemplar["class_family"] == candidate["class_family"]
    if exemplar.get("geometry_family") and candidate.get("geometry_family"):
        hard_compatible = hard_compatible and (
            exemplar["geometry_family"] == candidate["geometry_family"]
        )
    matched = bool(hard_compatible and score >= threshold)
    if not hard_compatible:
        mismatches.insert(0, "hard representation or class-family filter")
    return {
        "score": round(score, 6),
        "matched": matched,
        "threshold": threshold,
        "hard_filters_passed": hard_compatible,
        "reasons": list(dict.fromkeys(reasons)),
        "mismatches": list(dict.fromkeys(mismatches)),
        "signals": signals,
    }


__all__ = [
    "SIGNATURE_VERSION",
    "build_geometry_signature",
    "compare_geometry_signatures",
]
