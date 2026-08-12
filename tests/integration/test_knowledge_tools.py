"""The knowledge tools over the wire, plus the extended get_schema_docs."""

from __future__ import annotations

import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def kb_harness(harness_factory, work_model):
    harness = await harness_factory(model=work_model)
    core = harness.core
    core.store.settings.knowledge.schemas = ["IFC4"]
    from ifc_console.knowledge import KnowledgeBase

    core.knowledge = KnowledgeBase(core.store.home, schemas=("IFC4",))
    await __import__("asyncio").to_thread(core.knowledge.build)
    return harness


@pytest.mark.asyncio
async def test_search_finds_the_property_set_for_fire_rating(kb_harness):
    payload = await kb_harness.call("search_ifc_knowledge", query="wall fire rating", limit=5)
    assert payload["ok"] is True
    names = [hit["name"] for hit in payload["data"]["hits"]]
    assert "Pset_WallCommon.FireRating" in names


@pytest.mark.asyncio
async def test_search_honours_the_kind_filter(kb_harness):
    payload = await kb_harness.call(
        "search_ifc_knowledge", query="create a wall", kind=["api"], limit=5
    )
    assert payload["ok"] is True
    hits = payload["data"]["hits"]
    assert hits and all(hit["kind"] == "api" for hit in hits)


@pytest.mark.asyncio
async def test_get_api_docs_returns_a_signature(kb_harness):
    payload = await kb_harness.call("get_api_docs", function="root.create_entity")
    assert payload["ok"] is True
    assert payload["data"]["meta"]["signature"].startswith("create_entity(")
    assert "ifc_api.root.create_entity" in payload["data"]["usage"]


@pytest.mark.asyncio
async def test_get_api_docs_suggests_close_names(kb_harness):
    payload = await kb_harness.call("get_api_docs", function="pset.add_property_set")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"
    assert "add_pset" in payload["error"]["hint"]


@pytest.mark.asyncio
async def test_get_knowledge_record_returns_the_body(kb_harness):
    payload = await kb_harness.call("get_knowledge_record", key="recipe:rename-elements")
    assert payload["ok"] is True
    assert "edit_attributes" in payload["data"]["body"]


@pytest.mark.asyncio
async def test_knowledge_tools_report_when_the_index_is_missing(ask_harness):
    payload = await ask_harness.call("search_ifc_knowledge", query="wall")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "KNOWLEDGE_NOT_READY"


@pytest.mark.asyncio
async def test_schema_docs_lists_applicable_psets(ask_harness):
    payload = await ask_harness.call("get_schema_docs", entity="IfcWall")
    assert payload["ok"] is True
    assert "Pset_WallCommon" in payload["data"]["applicable_psets"]


@pytest.mark.asyncio
async def test_schema_docs_explains_a_property_set(ask_harness):
    payload = await ask_harness.call("get_schema_docs", pset="Pset_WallCommon")
    assert payload["ok"] is True
    block = payload["data"]["property_set"]
    assert block["applicable_to"] == "IfcWall"
    names = {prop["name"]: prop for prop in block["properties"]}
    assert names["FireRating"]["selector"] == "Pset_WallCommon.FireRating"
    assert names["IsExternal"]["data_type"] == "IfcBoolean"


@pytest.mark.asyncio
async def test_schema_docs_reverse_looks_up_a_property(ask_harness):
    payload = await ask_harness.call("get_schema_docs", property="FireRating")
    assert payload["ok"] is True
    lookup = payload["data"]["property_lookup"]
    assert lookup["found"] > 1
    assert "Pset_WallCommon.FireRating" in [row["selector"] for row in lookup["defined_in"]]


@pytest.mark.asyncio
async def test_schema_docs_needs_one_of_the_three_arguments(ask_harness):
    payload = await ask_harness.call("get_schema_docs")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_schema_docs_rejects_an_unknown_pset(ask_harness):
    payload = await ask_harness.call("get_schema_docs", pset="Pset_NotAThing")
    assert payload["ok"] is False
    assert payload["error"]["code"] == "NOT_FOUND"
