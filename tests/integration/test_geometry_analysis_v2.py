"""End-to-end contract checks for semantic geometry analysis."""

from __future__ import annotations

import numpy as np


def _measurement_fields(item: dict) -> set[str]:
    return {
        "id",
        "label",
        "quantity_kind",
        "value_si",
        "si_unit",
        "source",
        "method",
        "frame",
        "confidence",
        "flags",
    } - set(item)


def _coverage_ids(items: list) -> set[str]:
    return {
        item.get("id") if isinstance(item, dict) else item
        for item in items
        if (isinstance(item, str) and item) or (isinstance(item, dict) and item.get("id"))
    }


async def test_high_level_analysis_returns_versioned_semantic_inventory(
    harness_factory, work_model
):
    h = await harness_factory(model=work_model)
    out = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="compact",
        frame="semantic",
        station_strategy="auto",
    )

    assert out["ok"] is True
    data = out["data"]
    assert data["analysis_version"] == "2.0"
    assert data["model_revision"] == {
        "model_id": h.core.session.model_id,
        "fingerprint": h.core.session.fingerprint,
        "revision": h.core.session.revision,
    }
    record = data["elements"][0]
    assert record["dimensions"]
    assert record["measurements"]
    assert record["coverage"]["extracted"]
    assert record["geometry_signature"]["version"]

    semantic = record["frames"]["semantic"]
    axes = np.asarray([semantic["longitudinal"], semantic["transverse"], semantic["vertical"]])
    np.testing.assert_allclose(np.linalg.norm(axes, axis=1), np.ones(3), atol=1e-6)
    np.testing.assert_allclose(axes @ axes.T, np.eye(3), atol=1e-6)

    for measurement in record["measurements"]:
        assert not _measurement_fields(measurement), measurement
        assert "." in measurement["id"]
        assert measurement["frame"] in {"semantic", "placement", "principal", "world", None}
        assert measurement["confidence"] in {"high", "medium", "low"}


async def test_requested_measurement_coverage_never_silently_omits_unknown_ids(
    harness_factory, work_model
):
    h = await harness_factory(model=work_model)
    requested = ["envelope.overall_height", "custom.unsupported_measurement"]
    out = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="compact",
        measurement_ids=requested,
    )

    assert out["ok"] is True
    coverage = out["data"]["elements"][0]["coverage"]
    assert coverage["requested"] == requested
    accounted_for = set().union(
        *(
            _coverage_ids(coverage[name] or [])
            for name in ("extracted", "unavailable", "ambiguous", "conflicting")
        )
    )
    assert set(requested) <= accounted_for
    assert "custom.unsupported_measurement" in _coverage_ids(coverage["unavailable"])


async def test_analysis_options_are_cache_keyed_and_sections_can_be_skipped(
    harness_factory, work_model
):
    h = await harness_factory(model=work_model)

    without_sections = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="compact",
        station_strategy="none",
        include_sections=False,
    )
    assert without_sections["ok"] is True
    assert without_sections["meta"].get("cached") is not True

    with_sections = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="full",
        station_strategy="auto",
        include_sections=True,
        include_outline=True,
    )
    assert with_sections["ok"] is True
    assert with_sections["meta"].get("cached") is not True
    record = with_sections["data"]["elements"][0]
    assert record["section_analysis"]["strategy"] == "auto"
    assert record["section_analysis"]["stations_evaluated"] <= 17
    assert record["representation_inventory"]

    warm = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="full",
        station_strategy="auto",
        include_sections=True,
        include_outline=True,
    )
    assert warm["meta"]["cached"] is True


async def test_compact_then_full_analysis_reuses_revision_mesh(
    harness_factory, work_model, monkeypatch
):
    from ifc_console.ifc import geometry

    h = await harness_factory(model=work_model)
    original = geometry._tessellate
    tessellation_calls: list[list[int]] = []

    def counted_tessellation(ifc, elements, *, profile="standard", max_triangles=None):
        tessellation_calls.append([element.id() for element in elements])
        return original(
            ifc,
            elements,
            profile=profile,
            max_triangles=max_triangles,
        )

    monkeypatch.setattr(geometry, "_tessellate", counted_tessellation)
    compact = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="compact",
        station_strategy="none",
        include_sections=False,
    )
    detailed = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="full",
        station_strategy="auto",
        include_sections=True,
    )

    assert compact["ok"] is True
    assert detailed["ok"] is True
    assert len(tessellation_calls) == 1
    first_stats = compact["data"]["performance"]["mesh_cache"]
    second_stats = detailed["data"]["performance"]["mesh_cache"]
    assert first_stats["cache_misses"] == 1
    assert first_stats["tessellation_batches"] == 1
    assert second_stats["cache_hits"] == 1
    assert second_stats["cache_misses"] == 0
    assert second_stats["tessellation_batches"] == 0
    assert second_stats["tessellation_ms"] == 0.0
    assert detailed["data"]["performance"]["timing_ms"]["analysis_call"] >= 0.0

    warm = await h.call(
        "analyze_element_geometry",
        selector="IfcWall, Name=Wall-1",
        detail="full",
        station_strategy="auto",
        include_sections=True,
    )
    assert warm["meta"]["cached"] is True
    assert warm["data"]["performance"]["mesh_cache"]["skipped_due_to_read_cache"] is True
    assert len(tessellation_calls) == 1
