"""Tool registration surface, including the stable optional viewer catalog."""

from __future__ import annotations

import threading

import pytest

from ifc_console.mcp.tools_viewer import TOOL_NAMES as VIEWER_TOOLS
from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio

CORE_TOOLS = {
    "get_session_status",
    "orient",
    "describe_capabilities",
    "get_ifc_project_info",
    "get_spatial_structure",
    "query_elements",
    "get_element",
    "get_psets",
    "get_schema_docs",
    "validate_model",
    "validate_ids",
    "compute_quantities",
    "get_georeferencing",
    "export_csv",
    "execute_ifc_code",
    "list_ifc_files",
    "open_ifc_file",
    "save_ifc_file",
}


async def test_core_and_viewer_tools_are_registered_before_viewer_start(ask_harness) -> None:
    """A cached MCP catalog can open and then drive the viewer."""
    tools = set(await ask_harness.list_tools())
    assert CORE_TOOLS.issubset(tools), CORE_TOOLS - tools
    assert set(VIEWER_TOOLS).issubset(tools)


async def test_viewer_tools_appear_when_enabled_at_start(tmp_path) -> None:
    """--viewer at launch: build_mcp registers the category from the start."""
    from ifc_console.app import AppCore
    from ifc_console.mcp.server import build_mcp
    from ifc_console.settings import SettingsStore

    store = SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={})
    core = AppCore(store, viewer=True)
    mcp = build_mcp(core)
    names = {tool.name for tool in await mcp.list_tools()}
    assert set(VIEWER_TOOLS).issubset(names)
    assert CORE_TOOLS.issubset(names)
    core.shutdown()


async def test_missing_viewer_extra_keeps_catalog_but_launcher_explains(tmp_path, monkeypatch) -> None:
    """Discovery stays stable and execution reports the missing optional extra."""
    from ifc_console.app import AppCore
    from ifc_console.mcp.server import build_mcp
    from ifc_console.settings import SettingsStore
    from ifc_console.viewer import assets

    monkeypatch.setattr(assets, "available", lambda: False)
    store = SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={})
    core = AppCore(store, viewer=True, chat=True)
    mcp = build_mcp(core)
    names = {tool.name for tool in await mcp.list_tools()}
    assert core.viewer.enabled is False
    assert core.chat.enabled is False
    assert set(VIEWER_TOOLS).issubset(names)
    result = await core.operation_service.call("open_viewer", {"wait_for_connection_s": 0})
    assert result.ok is False
    assert result.error.code == "EXTRA_NOT_INSTALLED"
    core.shutdown()


async def test_viewer_tool_catalog_is_stable_across_toggle(harness_factory, work_model) -> None:
    """/viewer changes readiness, never names cached by an MCP client."""
    h = await harness_factory(model=work_model)
    assert set(VIEWER_TOOLS).issubset(set(await h.list_tools()))

    h.core.enable_viewer()
    assert set(VIEWER_TOOLS).issubset(set(await h.list_tools()))

    h.core.disable_viewer()
    assert set(VIEWER_TOOLS).issubset(set(await h.list_tools()))

    # and back on: registration must be repeatable
    h.core.enable_viewer()
    assert set(VIEWER_TOOLS).issubset(set(await h.list_tools()))


async def test_viewer_tools_survive_server_rebuild(tmp_path) -> None:
    """/port rebuilds the FastMCP instance; an enabled viewer must re-register
    its category on the new one."""
    from ifc_console.app import AppCore
    from ifc_console.mcp.server import build_mcp
    from ifc_console.settings import SettingsStore

    store = SettingsStore(home=tmp_path / "home", project_dir=tmp_path, env={})
    core = AppCore(store)
    build_mcp(core)
    core.enable_viewer()
    rebuilt = build_mcp(core)  # what restart_server does
    names = {tool.name for tool in await rebuilt.list_tools()}
    assert set(VIEWER_TOOLS).issubset(names)
    core.shutdown()


async def test_viewer_tools_report_not_connected(harness_factory, work_model) -> None:
    """No browser tab: the stable tools answer with the recovery call."""
    h = await harness_factory(model=work_model)
    out = await h.call("get_viewer_selection")
    assert out["ok"] is False
    assert out["error"]["code"] == "VIEWER_NOT_CONNECTED"
    assert "open_viewer" in out["error"]["hint"]


async def test_mcp_tools_publish_interoperability_metadata(ask_harness) -> None:
    result = await ask_harness.session.list_tools()
    by_name = {tool.name: tool for tool in result.tools}

    control = by_name["control_viewer"]
    assert "viewer" in control.meta["tags"]
    assert control.meta["ifcConsole"]["requiredCapabilities"] == ["viewer:control"]
    assert control.annotations.readOnlyHint is False

    query = by_name["query_elements"]
    assert "read" in query.meta["tags"]
    assert query.meta["ifcConsole"]["sharedOperation"] is True


async def test_capability_report_explains_viewer_readiness(ask_harness) -> None:
    out = await ask_harness.call("describe_capabilities")
    assert out["ok"] is True
    assert out["data"]["tool_surface"]["shared"] is True
    assert out["data"]["tool_surface"]["stable_viewer_catalog"] is True
    tools = {row["name"]: row for row in out["data"]["tools"]}
    assert tools["control_viewer"]["required_capabilities"] == ["viewer:control"]
    assert tools["control_viewer"]["availability"] == "call_open_viewer"


async def test_list_ifc_files_finds_model(ask_harness, work_model) -> None:
    out = await ask_harness.call("list_ifc_files")
    assert out["ok"] is True
    names = [f["path"] for f in out["data"]["files"]]
    assert any(work_model.name in n for n in names)


async def test_list_ifc_files_scans_off_the_event_loop(ask_harness, monkeypatch) -> None:
    event_loop_thread = threading.get_ident()
    scanned_on: list[int] = []

    def scan(_roots, _recursive, _recent_paths):
        scanned_on.append(threading.get_ident())
        return []

    monkeypatch.setattr("ifc_console.mcp.tools_files._scan_ifc_files", scan)

    out = await ask_harness.call("list_ifc_files")

    assert out["ok"] is True
    assert scanned_on and scanned_on[0] != event_loop_thread


async def test_open_ifc_file_works_in_ask_mode(
    harness_factory, work_model, tmp_path
) -> None:
    """Opening is reading, not editing: ask mode allows it (the terminal shows it)."""
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    # a second copy in an allowed dir
    import shutil

    other = tmp_path / "other.ifc"
    shutil.copy2(work_model, other)
    h.core.add_allowed_dir(tmp_path)
    out = await h.call("open_ifc_file", path=str(other))
    assert out["ok"] is True
    assert out["data"]["name"] == "other.ifc"


async def test_open_ifc_file_refuses_unsaved_changes(
    harness_factory, work_model, tmp_path
) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT)
    out = await h.call(
        "execute_ifc_code",
        code="ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')",
    )
    assert out["ok"] is True  # the model is now dirty
    import shutil

    other = tmp_path / "other.ifc"
    shutil.copy2(work_model, other)
    h.core.add_allowed_dir(tmp_path)
    out = await h.call("open_ifc_file", path=str(other))
    assert out["ok"] is False
    assert out["error"]["code"] == "UNSAVED_CHANGES"


async def test_open_ifc_file_rejects_outside_allowed(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.ASK)
    out = await h.call("open_ifc_file", path="/etc/passwd")
    assert out["ok"] is False
    assert out["error"]["code"] in ("PATH_NOT_ALLOWED", "INVALID_INPUT")


async def test_get_schema_docs_without_model(harness_factory) -> None:
    h = await harness_factory(model=None)
    out = await h.call("get_schema_docs", entity="IfcWall")
    assert out["ok"] is True  # works with no model (defaults to IFC4)
    assert out["data"]["schema"] == "IFC4"
