"""The offline knowledge index: build once, then search it."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import ifc_console.knowledge as knowledge_module
from ifc_console.knowledge import KnowledgeBase, _collapse_schemas
from ifc_console.knowledge.records import Record, expand_terms
from ifc_console.knowledge.store import build as build_store
from ifc_console.knowledge.store import name_boost, query_tokens

# The full three-schema build takes seconds; one schema is enough to prove the
# pipeline and keeps the suite quick.
_SCHEMAS = ("IFC4",)


def _repair_records(_schemas: tuple[str, ...]):
    yield Record(
        kind="recipe",
        key="recipe:test:repair",
        name="repair-marker",
        summary="repaired knowledge index",
    )


@pytest.fixture(scope="module")
def kb(tmp_path_factory: pytest.TempPathFactory) -> KnowledgeBase:
    home = tmp_path_factory.mktemp("kb-home")
    base = KnowledgeBase(home, schemas=_SCHEMAS)
    base.build()
    yield base
    base.close()


def test_expand_terms_splits_identifiers():
    terms = expand_terms("IfcWallStandardCase").split()
    assert "wall" in terms and "standard" in terms and "ifcwallstandardcase" in terms
    assert "wallcommon" in expand_terms("Pset_WallCommon").split()


def test_query_tokens_drop_stopwords_and_expand_phrases():
    tokens = query_tokens("how do I add a property set to an element")
    assert "add" in tokens and "how" not in tokens
    assert "pset" in tokens, "the phrase 'property set' should reach the schema spelling"


def test_name_boost_prefers_exact_names():
    exact = name_boost(
        "material.assign_material", "material assign material", ["assign", "material"]
    )
    other = name_boost("material.assign_profile", "material assign profile", ["assign", "material"])
    assert exact > other


def test_build_indexes_every_corpus(kb: KnowledgeBase):
    stats = kb.stats()
    assert stats["ready"] is True
    counts = stats["counts"]
    for kind in ("entity", "pset", "property", "type", "api", "recipe"):
        assert counts.get(kind, 0) > 0, f"no {kind} records were indexed"
    assert counts["api"] > 300
    assert kb.path.exists()


def test_rebuild_is_skipped_when_the_index_exists(kb: KnowledgeBase):
    assert kb.build()["built"] is False


def test_search_finds_a_property_by_plain_words(kb: KnowledgeBase):
    hits = kb.search("fire rating of a wall", limit=5)
    names = [hit["name"] for hit in hits]
    assert "Pset_WallCommon.FireRating" in names


def test_search_finds_the_api_function_for_a_task(kb: KnowledgeBase):
    hits = kb.search("assign material", kind="api", limit=5)
    assert hits[0]["name"] == "material.assign_material"


def test_search_finds_a_recipe(kb: KnowledgeBase):
    hits = kb.search("rename elements", kind="recipe", limit=3)
    assert hits[0]["name"] == "rename-elements"
    assert hits[0]["meta"]["mode"] == "edit"


def test_search_respects_the_kind_filter(kb: KnowledgeBase):
    hits = kb.search("wall", kind=("entity",), limit=10)
    assert hits and all(hit["kind"] == "entity" for hit in hits)


def test_get_returns_the_full_body(kb: KnowledgeBase):
    record = kb.get("api:root.create_entity")
    assert record is not None
    assert "ifc_class" in record["body"]
    assert record["meta"]["signature"].startswith("create_entity(")


def test_get_is_none_for_an_unknown_key(kb: KnowledgeBase):
    assert kb.get("api:does.not.exist") is None


def test_lookup_by_exact_name(kb: KnowledgeBase):
    found = kb.lookup("IfcWall", kind="entity", schema="IFC4")
    assert found and found[0]["key"] == "entity:IFC4:IfcWall"


def test_entity_records_carry_predefined_types(kb: KnowledgeBase):
    record = kb.get("entity:IFC4:IfcWall")
    assert "SOLIDWALL" in record["meta"]["predefined_types"]


def test_pset_records_carry_applicability(kb: KnowledgeBase):
    record = kb.get("pset:IFC4:Pset_WallCommon")
    assert record["meta"]["applicable_to"] == "IfcWall"
    assert "FireRating" in record["meta"]["properties"]


def test_missing_index_searches_empty_instead_of_raising(tmp_path: Path):
    base = KnowledgeBase(tmp_path / "empty")
    assert base.ready is False
    assert base.search("anything") == []
    assert base.stats()["ready"] is False


def test_corrupt_index_is_not_ready_and_build_repairs_it(tmp_path: Path, monkeypatch):
    base = KnowledgeBase(tmp_path / "home", schemas=_SCHEMAS)
    base.path.parent.mkdir(parents=True)
    base.path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(knowledge_module, "_iter_records", _repair_records)

    assert base.ready is False
    assert base.last_error
    assert base.build()["built"] is True

    assert base.ready is True
    assert base.get("recipe:test:repair") is not None
    base.close()


def test_incompatible_metadata_is_not_ready_and_build_repairs_it(tmp_path: Path, monkeypatch):
    base = KnowledgeBase(tmp_path / "home", schemas=_SCHEMAS)
    build_store(
        base.path,
        _repair_records(_SCHEMAS),
        {"ifcopenshell": "different-version", "schemas": list(_SCHEMAS)},
    )
    monkeypatch.setattr(knowledge_module, "_iter_records", _repair_records)

    assert base.ready is False
    assert "metadata" in (base.last_error or "")
    assert base.build()["built"] is True

    assert base.ready is True
    assert base.get("recipe:test:repair") is not None
    base.close()


def test_collapse_schemas_keeps_one_row_per_name():
    rows = [
        {"kind": "pset", "name": "P", "schema": "IFC2X3", "score": 5.0},
        {"kind": "pset", "name": "P", "schema": "IFC4", "score": 5.0},
        {"kind": "pset", "name": "Q", "schema": "IFC4", "score": 4.0},
    ]
    collapsed = _collapse_schemas(rows, 10)
    assert [row["name"] for row in collapsed] == ["P", "Q"]
    assert collapsed[0]["schema"] == "IFC4", "IFC4 represents a name in several schemas"
    assert collapsed[0]["also_in"] == ["IFC2X3"]


def test_index_is_rebuilt_for_a_new_ifcopenshell_version(tmp_path: Path, monkeypatch):
    base = KnowledgeBase(tmp_path / "home", schemas=_SCHEMAS)
    first = base.path
    monkeypatch.setattr("ifc_console.knowledge._ifcopenshell_version", lambda: "9.9.9")
    assert base.path != first


def test_old_indexes_are_pruned(kb: KnowledgeBase, tmp_path: Path):
    stale = kb.path.parent / "kb-v1-ios0.0.1.sqlite"
    shutil.copy2(kb.path, stale)
    kb.build(force=True)
    assert not stale.exists()
    assert kb.path.exists()
