"""Query tools end-to-end through the in-memory MCP client (plan 03 §3.1-3.7)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio


@pytest.fixture
def classified_model(tmp_path: Path, minimal_ifc4_path: Path) -> Path:
    """The fixture model plus one direct and one type-inherited reference."""
    import ifcopenshell
    import ifcopenshell.api.classification as classification_api
    import ifcopenshell.api.root
    import ifcopenshell.api.type

    ifc = ifcopenshell.open(str(minimal_ifc4_path))
    walls = {w.Name: w for w in ifc.by_type("IfcWall")}
    system = classification_api.add_classification(ifc, classification="Uniclass 2015")
    reference = classification_api.add_reference(
        ifc,
        products=[walls["Wall-1"]],
        identification="EF_25_10",
        name="Walls",
        classification=system,
    )
    reference.Location = "https://uniclass.thenbs.com/EF_25_10"

    wall_type = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWallType", name="WT-1")
    ifcopenshell.api.type.assign_type(
        ifc, related_objects=[walls["Wall-2"]], relating_type=wall_type
    )
    classification_api.add_reference(
        ifc,
        products=[wall_type],
        identification="EF_25_10_25",
        name="Framed walls",
        classification=system,
    )
    path = tmp_path / "classified.ifc"
    ifc.write(str(path))
    return path


async def test_session_status(ask_harness) -> None:
    out = await ask_harness.call("get_session_status")
    assert out["ok"] is True
    assert out["data"]["model"]["loaded"] is True
    assert out["data"]["mode"] == "ask"
    assert out["meta"]["fingerprint"]


async def test_session_status_does_not_serialize_the_viewer_token(ask_harness) -> None:
    assert ask_harness.core.enable_viewer() is True

    result = await ask_harness.session.call_tool("get_session_status", {})
    out = result.structuredContent

    assert out is not None
    assert out["data"]["viewer"]["url"] == ask_harness.core.viewer_public_url
    serialized = json.dumps(out) + "".join(
        block.text for block in result.content if getattr(block, "type", None) == "text"
    )
    assert ask_harness.core.token not in serialized
    assert "#t=" not in out["data"]["viewer"]["url"]
    assert f"#t={ask_harness.core.token}" in ask_harness.core.viewer.url


async def test_project_info(ask_harness) -> None:
    out = await ask_harness.call("get_ifc_project_info")
    assert out["ok"] is True
    data = out["data"]
    assert data["schema"] == "IFC4"
    assert data["entity_counts"]["IfcWall"] == 3
    assert data["spatial"]["storeys"] == 2


async def test_model_read_pins_its_session_during_lifecycle_changes(
    ask_harness, monkeypatch
) -> None:
    core = ask_harness.core
    session = core.session
    model_id = core.models.active_id
    original_run = session.run
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_run(job, timeout=None):
        entered.set()
        await release.wait()
        return await original_run(job, timeout=timeout)

    monkeypatch.setattr(session, "run", blocked_run)
    read = asyncio.create_task(ask_harness.call("search_elements", term="Wall"))
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert model_id is not None
    detach = asyncio.create_task(core.detach_model(model_id))
    await asyncio.sleep(0)

    assert not detach.done()
    release.set()
    result = await read
    await detach

    assert result["ok"] is True


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


async def test_query_elements_pages_instead_of_dropping_rows(ask_harness) -> None:
    """An oversized page used to come back as ok=True with the rows gone."""
    full = await ask_harness.call("query_elements", query="IfcWall")
    baseline = json.dumps({"ok": True, "data": full["data"], "meta": full["meta"]}, indent=2)
    ask_harness.core.settings.exec.output_char_limit = len(baseline) - 100

    out = await ask_harness.call("query_elements", query="IfcWall")

    assert out["ok"] is True
    assert out["meta"]["truncated"] is True
    assert "preview" not in out["data"]
    assert isinstance(out["data"]["rows"], list)
    cut = out["data"]["truncation"]
    assert cut["kept"] == len(out["data"]["rows"]) < 3
    assert cut["of"] == 3
    assert cut["next_offset"] == cut["kept"]


async def test_query_elements_projects_dotted_properties(ask_harness) -> None:
    out = await ask_harness.call(
        "query_elements",
        query="IfcWall",
        fields=[],
        properties=["Pset_WallCommon.FireRating"],
    )

    assert out["ok"] is True
    rows = out["data"]["rows"]
    # fields=[] means the minimal row, not the three default columns
    assert set(rows[0]) == {"global_id", "class", "Pset_WallCommon.FireRating"}
    assert "F30" in [row["Pset_WallCommon.FireRating"] for row in rows]


async def test_query_elements_rejects_an_undotted_property(ask_harness) -> None:
    out = await ask_harness.call("query_elements", query="IfcWall", properties=["FireRating"])

    assert out["ok"] is False
    assert out["error"]["code"] == "INVALID_INPUT"
    assert "get_element_details" not in out["error"]["hint"]


async def test_get_element_include_empty_returns_the_minimal_record(ask_harness) -> None:
    listing = await ask_harness.call("query_elements", query="IfcWall", limit=1)
    gid = listing["data"]["rows"][0]["global_id"]

    out = await ask_harness.call("get_element", global_ids=[gid], include=[])

    assert out["ok"] is True
    assert set(out["data"]["elements"][0]) == {"global_id", "class"}


async def test_search_elements_paginates_with_offset(ask_harness) -> None:
    first = await ask_harness.call("search_elements", term="IfcWall", limit=1)
    second = await ask_harness.call("search_elements", term="IfcWall", limit=1, offset=1)

    assert first["meta"]["total"] == second["meta"]["total"] == 3
    assert second["meta"]["offset"] == 1
    assert first["data"]["results"][0]["global_id"] != second["data"]["results"][0]["global_id"]


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


async def test_get_element_reports_classification_references(
    harness_factory, classified_model: Path
) -> None:
    h = await harness_factory(model=classified_model)

    async def global_id(name: str) -> str:
        listing = await h.call("query_elements", query=f"IfcWall, Name={name}")
        return listing["data"]["rows"][0]["global_id"]

    direct, from_type = await global_id("Wall-1"), await global_id("Wall-2")

    out = await h.call("get_element", global_ids=[direct, from_type], include=["classification"])

    assert out["ok"] is True
    rows = {e["global_id"]: e["classification"] for e in out["data"]["elements"]}
    assert rows[direct] == [
        {
            "system": "Uniclass 2015",
            "identification": "EF_25_10",
            "name": "Walls",
            "location": "https://uniclass.thenbs.com/EF_25_10",
            "inherited": False,
        }
    ]
    assert rows[from_type][0]["identification"] == "EF_25_10_25"
    assert rows[from_type][0]["inherited"] is True


async def test_project_info_reports_classification_coverage(
    harness_factory, classified_model: Path
) -> None:
    h = await harness_factory(model=classified_model)

    out = await h.call("get_ifc_project_info")

    data = out["data"]
    coverage = data["classification_coverage"]
    assert coverage["systems"] == [{"name": "Uniclass 2015", "elements": 2}]
    # the directly assigned wall plus the one that inherits from its type
    assert coverage["classified"] == 2
    assert coverage["total"] == data["entity_counts"]["total_products"]
    assert coverage["coverage"] == pytest.approx(2 / coverage["total"], abs=0.001)


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


# -- read and mesh caches on AppCore --------------------------------------------
async def test_element_meshes_tessellate_each_element_once(ask_harness, monkeypatch) -> None:
    import numpy as np

    core = ask_harness.core
    calls: list[list[int]] = []

    def fake_tessellate(ifc, elements):
        calls.append(sorted(e.id() for e in elements))
        return {e.id(): (np.zeros((3, 3)), np.zeros((1, 3), dtype=np.int64)) for e in elements}

    monkeypatch.setattr("ifc_console.ifc.geometry.element_meshes", fake_tessellate)
    walls = core.session.ifc.by_type("IfcWall")[:2]

    first = core.element_meshes(walls[:1])
    second = core.element_meshes(walls)

    assert set(first) == {walls[0].id()}
    assert set(second) == {walls[0].id(), walls[1].id()}
    assert calls == [[walls[0].id()], [walls[1].id()]]

    core.session.mark_dirty()  # a mutation must never serve a stale mesh
    core.element_meshes(walls[:1])
    assert calls[-1] == [walls[0].id()]


async def test_element_mesh_cache_separates_profiles_and_budgets(
    ask_harness, monkeypatch
) -> None:
    import numpy as np

    core = ask_harness.core
    calls: list[tuple[str, int | None]] = []

    def fake_tessellate(ifc, elements, *, profile="standard", max_triangles=None):
        calls.append((profile, max_triangles))
        return {e.id(): (np.zeros((3, 3)), np.zeros((1, 3), dtype=np.int64)) for e in elements}

    monkeypatch.setattr("ifc_console.ifc.geometry.element_meshes", fake_tessellate)
    wall = core.session.ifc.by_type("IfcWall")[0]

    core.element_meshes([wall])
    core.element_meshes([wall], profile="analysis")
    core.element_meshes([wall], profile="analysis")
    core.element_meshes([wall], profile="analysis", max_triangles=123_456)

    assert calls == [
        ("standard", None),
        ("analysis", None),
        ("analysis", 123_456),
    ]


async def test_cached_read_joins_an_in_flight_computation(ask_harness) -> None:
    import threading

    core = ask_harness.core
    gate = threading.Event()
    calls = 0

    def build() -> int:
        nonlocal calls
        calls += 1
        gate.wait(10)
        return calls

    first = asyncio.create_task(core.cached_read("probe", build))
    await asyncio.sleep(0.05)  # let the first call reach the model worker
    second = asyncio.create_task(core.cached_read("probe", build))
    await asyncio.sleep(0.05)
    gate.set()
    (a, _), (b, joined) = await asyncio.gather(first, second)

    assert calls == 1
    assert (a, b) == (1, 1)
    assert joined is True


async def test_no_model_error(harness_factory) -> None:
    h = await harness_factory(model=None)
    out = await h.call("get_ifc_project_info")
    assert out["ok"] is False
    assert out["error"]["code"] == "NO_MODEL_LOADED"
    assert out["error"]["hint"]
