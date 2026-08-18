"""Tool registration surface: the core set, plus the optional viewer category
that appears and disappears with the viewer."""

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


async def test_core_tools_registered_viewer_tools_absent(ask_harness) -> None:
    """Without the viewer, the surface is the lean core set."""
    tools = set(await ask_harness.list_tools())
    assert CORE_TOOLS.issubset(tools), CORE_TOOLS - tools
    assert not tools & set(VIEWER_TOOLS)


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


async def test_missing_viewer_extra_cannot_enable_its_tools(tmp_path, monkeypatch) -> None:
    """A remembered setting or --viewer must not expose unusable viewer tools
    in a core-only installation."""
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
    assert not names & set(VIEWER_TOOLS)
    core.shutdown()


async def test_viewer_tools_toggle_live(harness_factory, work_model) -> None:
    """/viewer mid-session adds the category; /viewer off removes it again,
    without rebuilding the server."""
    h = await harness_factory(model=work_model)
    assert not set(await h.list_tools()) & set(VIEWER_TOOLS)

    h.core.enable_viewer()
    assert set(VIEWER_TOOLS).issubset(set(await h.list_tools()))

    h.core.disable_viewer()
    assert not set(await h.list_tools()) & set(VIEWER_TOOLS)

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
    """Enabled viewer, no browser tab: the tools exist and answer honestly."""
    h = await harness_factory(model=work_model)
    h.core.enable_viewer()
    out = await h.call("get_viewer_selection")
    assert out["ok"] is False
    assert out["error"]["code"] == "VIEWER_NOT_CONNECTED"


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
