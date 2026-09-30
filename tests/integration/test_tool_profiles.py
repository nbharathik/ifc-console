"""The lean profile lists a small core; every tool stays callable; find_tools and call_tool reach the rest."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from ifc_console.mcp import catalog
from ifc_console.policy.modes import Mode

pytestmark = pytest.mark.asyncio


async def test_the_full_profile_lists_every_tool_but_the_discovery_pair(ask_harness) -> None:
    names = set(await ask_harness.list_tools())

    assert {"orient", "execute_ifc_code", "set_properties", "measure_elements"} <= names
    assert not names & catalog.DISCOVERY_TOOLS


async def test_the_lean_profile_lists_the_core_in_order(ask_harness) -> None:
    ask_harness.core.settings.mcp.tool_profile = "lean"

    names = await ask_harness.list_tools()

    assert names == list(catalog.LEAN_CORE)


async def test_a_tool_the_lean_profile_hides_is_still_callable(ask_harness) -> None:
    ask_harness.core.settings.mcp.tool_profile = "lean"
    await ask_harness.list_tools()

    out = await ask_harness.call("get_ifc_project_info")

    assert out["ok"] is True
    assert "get_ifc_project_info" not in await ask_harness.list_tools()


async def test_find_tools_returns_matches_with_their_argument_shapes(ask_harness) -> None:
    out = await ask_harness.call("find_tools", query="take a screenshot of the model")

    assert out["ok"] is True
    tools = out["data"]["tools"]
    assert tools[0]["name"] == "get_viewer_screenshot"
    assert "args" in tools[0] and tools[0]["summary"]
    assert "call_tool" in out["data"]["use"]
    assert not {tool["name"] for tool in tools} & catalog.DISCOVERY_TOOLS


async def test_find_tools_flags_edits_and_direct_only_tools(ask_harness) -> None:
    edits = await ask_harness.call("find_tools", query="set property values on elements")
    saves = await ask_harness.call("find_tools", query="save the model to a file")

    assert {t["name"]: t for t in edits["data"]["tools"]}["set_properties"]["edits"] is True
    assert {t["name"]: t for t in saves["data"]["tools"]}["save_ifc_file"]["direct_only"] is True


async def test_call_tool_runs_a_tool_and_returns_its_result(ask_harness) -> None:
    direct = await ask_harness.call("get_ifc_project_info")

    via = await ask_harness.call("call_tool", name="get_ifc_project_info", arguments={})

    assert via["ok"] is True
    assert via["data"] == direct["data"]


async def test_call_tool_passes_arguments_through(ask_harness) -> None:
    via = await ask_harness.call(
        "call_tool", name="query_elements", arguments={"query": "IfcWall", "limit": 1}
    )

    assert via["ok"] is True
    assert len(via["data"]["rows"]) == 1


async def test_call_tool_refuses_tools_that_delete_or_overwrite(harness_factory, work_model) -> None:
    h = await harness_factory(model=work_model, mode=Mode.EDIT, allow_ai_save=True)

    out = await h.call("call_tool", name="save_ifc_file", arguments={})

    assert out["ok"] is False
    assert out["error"]["code"] == "CAPABILITY_DENIED"
    assert "/tools profile full" in out["error"]["hint"]


async def test_call_tool_does_not_bypass_ask_mode(ask_harness) -> None:
    out = await ask_harness.call(
        "call_tool",
        name="set_properties",
        arguments={"changes": [{"global_ids": ["x"], "pset": "P", "property": "p", "value": 1}]},
    )

    assert out["ok"] is False
    assert out["error"]["code"] == "ASK_MODE_BLOCKED"


async def test_call_tool_reports_an_unknown_name(ask_harness) -> None:
    out = await ask_harness.call("call_tool", name="no_such_tool")

    assert out["ok"] is False
    assert out["error"]["code"] == "NOT_FOUND"
    assert "find_tools" in out["error"]["hint"]


async def test_call_tool_does_not_call_itself(ask_harness) -> None:
    out = await ask_harness.call("call_tool", name="call_tool", arguments={})

    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"


async def test_results_are_one_line_of_json(ask_harness) -> None:
    result = await ask_harness.session.call_tool("get_ifc_project_info", {})

    text = result.content[0].text
    assert "\n" not in text
    assert json.loads(text)["ok"] is True


def _rpc(client: TestClient, path: str, headers: dict | None = None) -> list[str]:
    response = client.post(
        path,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        },
    )
    assert response.status_code == 200, response.text
    body = response.text
    if body.startswith("event:"):
        body = next(line[6:] for line in body.splitlines() if line.startswith("data: "))
    return [tool["name"] for tool in json.loads(body)["result"]["tools"]]


async def test_a_path_or_a_header_picks_the_profile_over_http(core, work_model) -> None:
    from ifc_console.mcp.server import build_http_app, build_mcp

    await core.open_model(work_model)
    auth = {"Authorization": f"Bearer {core.token}"}
    app = build_http_app(core, build_mcp(core))

    with TestClient(app, base_url="http://127.0.0.1:8383") as client:
        default = _rpc(client, "/mcp", auth)
        by_path = _rpc(client, "/mcp/lean", auth)
        by_header = _rpc(client, "/mcp", {**auth, "X-IFC-Console-Tools": "lean"})

    assert "measure_elements" in default and "find_tools" not in default
    assert by_path == list(catalog.LEAN_CORE)
    assert by_header == list(catalog.LEAN_CORE)
