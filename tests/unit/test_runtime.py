from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.runtime import LocalRuntime


@pytest.mark.asyncio
async def test_local_runtime_reuses_workbench_operations(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    async with await LocalRuntime.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
    ) as runtime:
        status = await runtime.workspace.status()
        tools = await runtime.toolset()
        result = await tools.call("query_elements", {"query": "IfcWall", "limit": 2})

        assert status["model"]["loaded"] is True
        assert "query_elements" in tools
        assert result["ok"] is True
        assert len(result["data"]["rows"]) == 2
        assert runtime.context.active_model.model_id
