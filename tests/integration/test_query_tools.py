"""Query tools end-to-end through the in-memory MCP client (plan 03 §3.1-3.7)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.asyncio


async def test_session_status(ask_harness) -> None:
    out = await ask_harness.call("get_session_status")
    assert out["ok"] is True
    assert out["data"]["model"]["loaded"] is True
    assert out["data"]["mode"] == "ask"
    assert out["meta"]["fingerprint"]


async def test_project_info(ask_harness) -> None:
    out = await ask_harness.call("get_ifc_project_info")
    assert out["ok"] is True
    data = out["data"]
    assert data["schema"] == "IFC4"
    assert data["entity_counts"]["IfcWall"] == 3
    assert data["spatial"]["storeys"] == 2


async def test_spatial_structure(ask_harness) -> None:
    out = await ask_harness.call("get_spatial_structure")
    assert out["ok"] is True
    assert out["data"]["tree"]["class"] == "IfcProject"


async def test_query_elements_and_pagination(ask_harness) -> None:
    out = await ask_harness.call("query_elements", query="IfcWall")
    assert out["ok"] is True
    assert out["meta"]["total"] == 3
    assert len(out["data"]["rows"]) == 3
    assert out["data"]["rows"][0]["class"] == "IfcWall"

    page = await ask_harness.call("query_elements", query="IfcWall", limit=1, offset=1)
    assert page["meta"]["total"] == 3
    assert len(page["data"]["rows"]) == 1


async def test_search_elements_resolves_name_and_global_id(ask_harness) -> None:
    named = await ask_harness.call("search_elements", term="Wall-1")
    assert named["ok"] is True
    assert named["data"]["mode"] == "text"
    assert len(named["data"]["results"]) == 1

    global_id = named["data"]["results"][0]["global_id"]
    by_id = await ask_harness.call("search_elements", term=global_id)
    assert [row["global_id"] for row in by_id["data"]["results"]] == [global_id]


async def test_query_property_facet(ask_harness) -> None:
    out = await ask_harness.call(
        "query_elements", query="IfcWall, Pset_WallCommon.FireRating=F30"
    )
    assert out["ok"] is True
    assert out["meta"]["total"] == 1  # only one wall has FireRating F30


async def test_query_invalid_selector_gives_help(ask_harness) -> None:
    out = await ask_harness.call("query_elements", query="!!! not valid @@")
    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_QUERY"
    assert "syntax_help" in out["data"]


async def test_get_element_detail_and_missing(ask_harness) -> None:
    listing = await ask_harness.call("query_elements", query="IfcWall")
    gid = listing["data"]["rows"][0]["global_id"]
    out = await ask_harness.call(
        "get_element", global_ids=[gid, "notarealguid00000000000"]
    )
    assert out["ok"] is True
    assert len(out["data"]["elements"]) == 1
    assert out["data"]["missing"] == ["notarealguid00000000000"]
    assert "attributes" in out["data"]["elements"][0]


async def test_get_psets(ask_harness) -> None:
    listing = await ask_harness.call(
        "query_elements", query="IfcWall, Pset_WallCommon.FireRating=F30"
    )
    gid = listing["data"]["rows"][0]["global_id"]
    out = await ask_harness.call("get_psets", global_ids=[gid])
    assert out["ok"] is True
    result = out["data"]["results"][0]
    assert result["found"] is True
    assert result["psets"]["Pset_WallCommon"]["FireRating"] == "F30"


async def test_schema_docs(ask_harness) -> None:
    out = await ask_harness.call("get_schema_docs", entity="IfcWall")
    assert out["ok"] is True
    assert out["data"]["entity"] == "IfcWall"
    assert any(a["name"] == "Name" for a in out["data"]["attributes"])


async def test_no_model_error(harness_factory) -> None:
    h = await harness_factory(model=None)
    out = await h.call("get_ifc_project_info")
    assert out["ok"] is False
    assert out["error"]["code"] == "NO_MODEL_LOADED"
    assert out["error"]["hint"]
