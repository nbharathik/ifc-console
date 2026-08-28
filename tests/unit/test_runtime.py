from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.runtime import LocalRuntime
from ifc_console.toolsets import FunctionToolSource, IfcToolProfile


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


@pytest.mark.asyncio
async def test_local_runtime_supports_a_sequential_configure_and_select_workflow(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    company = FunctionToolSource(namespace="company")

    @company.tool()
    async def property_rule() -> dict:
        return {"pset": "Company_ElementData", "property": "Thickness"}

    async with await LocalRuntime.open(home=tmp_path / "home") as runtime:
        model = await runtime.open_model(minimal_ifc4_path)
        runtime.set_mode("ask")
        configured = runtime.settings.update(
            {
                "exec.output_char_limit": 12_000,
                "workspace.scan_depth": 2,
            }
        )
        tools = await runtime.tools(
            "get_ifc_project_info",
            "search_elements",
            "company__property_rule",
            sources=(company,),
        )

        assert model["loaded"] is True
        assert configured == {
            "exec.output_char_limit": 12_000,
            "workspace.scan_depth": 2,
        }
        assert runtime.settings.get("exec.output_char_limit") == 12_000
        assert runtime.workbench.core.workspace.depth == 2
        assert tools.names == (
            "company__property_rule",
            "get_ifc_project_info",
            "search_elements",
        )


@pytest.mark.asyncio
async def test_runtime_exact_tool_selection_reports_unavailable_names(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    async with await LocalRuntime.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
    ) as runtime:
        with pytest.raises(KeyError, match="not_a_real_tool"):
            await runtime.tools("not_a_real_tool")


@pytest.mark.asyncio
async def test_agent_profiles_scope_ifc_tools_but_keep_company_tools(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    company = FunctionToolSource(namespace="company")

    @company.tool()
    async def requirements() -> dict:
        return {"property": "Thickness"}

    async with await LocalRuntime.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
    ) as runtime:
        inspect = await runtime.toolset(company, profile=IfcToolProfile.INSPECT)
        editing = await runtime.toolset(profile=IfcToolProfile.PROPERTY_EDIT)

        assert "search_elements" in inspect
        assert "open_viewer" in inspect
        assert "control_viewer" in inspect
        assert "company__requirements" in inspect
        assert "execute_ifc_code" not in inspect
        assert "preview_property_change" not in inspect
        assert "preview_property_change" in editing
        assert "save_ifc_file" not in editing


@pytest.mark.asyncio
async def test_workspace_search_uses_the_same_runtime_boundary(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    async with await LocalRuntime.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
    ) as runtime:
        results = await runtime.workspace.search("Wall-2")

        assert len(results) == 1
        assert results[0]["name"] == "Wall-2"


@pytest.mark.asyncio
async def test_runtime_builds_embeddable_viewer_surface(
    tmp_path: Path,
    minimal_ifc4_path: Path,
):
    async with await LocalRuntime.open(
        minimal_ifc4_path,
        home=tmp_path / "home",
        settings={"server.port": 8877},
    ) as runtime:
        viewer_url = runtime.enable_viewer()
        surface = runtime.build_web_app()

        assert surface.app is not None
        assert surface.viewer_url == viewer_url
        assert surface.browser_url("custom-chat").startswith("http://127.0.0.1:8877/custom-chat#t=")
        assert "token=" not in repr(surface)
