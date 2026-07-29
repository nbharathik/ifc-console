"""The cached flag surfaced by the heavy read tools."""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


async def test_project_info_reports_cache_state(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    first = await h.call("get_ifc_project_info")
    assert first["ok"] is True
    assert first["meta"]["cached"] is False
    second = await h.call("get_ifc_project_info")
    assert second["meta"]["cached"] is True
    assert second["data"] == first["data"]


async def test_spatial_tree_cache_invalidates_on_mutation(harness_factory, work_model: Path):
    from ifc_console.policy.modes import Mode

    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    first = await h.call("get_spatial_structure")
    assert first["meta"]["cached"] is False
    warm = await h.call("get_spatial_structure")
    assert warm["meta"]["cached"] is True

    out = await h.call(
        "execute_ifc_code",
        code="ifc.by_type('IfcProject')[0].Name = 'Renamed'",
        description="rename project",
    )
    assert out["ok"] is True
    cold = await h.call("get_spatial_structure")
    assert cold["meta"]["cached"] is False


async def test_spatial_tree_key_includes_arguments(harness_factory, work_model: Path):
    h = await harness_factory(model=work_model)
    await h.call("get_spatial_structure")
    shallow = await h.call("get_spatial_structure", depth=1)
    assert shallow["meta"]["cached"] is False
