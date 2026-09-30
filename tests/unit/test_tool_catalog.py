"""find_tools ranks the live catalog; the lean core is a fixed, small list."""

from __future__ import annotations

import pytest

from ifc_console.application.operations import build_operations
from ifc_console.mcp import catalog


@pytest.fixture
def index(core) -> catalog.ToolIndex:
    return catalog.ToolIndex(build_operations(core).definitions())


def _names(index: catalog.ToolIndex, query: str, limit: int = 3) -> list[str]:
    return [card.name for card in index.search(query, limit=limit)]


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("take a screenshot of the model", "get_viewer_screenshot"),
        ("find collisions between pipes and walls", "detect_clashes"),
        ("check the model against an IDS file", "validate_ids"),
        ("export the walls to a spreadsheet", "export_csv"),
        ("what storeys does the building have", "get_spatial_structure"),
        ("how thick is this wall", "measure_local_thickness"),
        ("compare two revisions of the model", "compare_models"),
        ("which elements are missing properties", "audit_element_properties"),
        ("paint elements by fire rating", "apply_color_theme"),
        ("what is the signature of pset.add_pset", "get_api_docs"),
        ("write property values on several elements", "set_properties"),
        ("assign a value to a property", "set_properties"),
    ],
)
def test_a_plain_question_finds_the_right_tool_near_the_top(index, query, expected) -> None:
    assert expected in _names(index, query)


def test_an_unrelated_query_finds_nothing(index) -> None:
    assert index.search("zzzz qqqq") == []
    assert index.search("") == []


def test_excluded_tools_are_not_returned(index) -> None:
    hits = index.search("run python code", exclude=frozenset({"execute_ifc_code"}))

    assert "execute_ifc_code" not in [card.name for card in hits]


def test_the_limit_caps_the_hits(index) -> None:
    assert len(index.search("model", limit=2)) == 2


def test_a_card_says_what_kind_of_tool_it_is(index) -> None:
    save = index.cards["save_ifc_file"]
    orient = index.cards["orient"]

    assert save.destructive is True and save.read_only is False
    assert orient.read_only is True and orient.destructive is False


def test_the_summary_drops_the_tag_and_keeps_the_first_sentence() -> None:
    assert (
        catalog.summary("[QUERY] Count things. Then do more. And more.") == "Count things."
    )
    long = "[X] " + "word " * 100
    assert len(catalog.summary(long, limit=50)) <= 50


def test_the_compact_schema_lists_arguments_with_short_notes() -> None:
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "A selector. " * 30},
            "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "description": "Rows."},
            "mode": {"type": "string", "enum": ["a", "b"]},
        },
        "required": ["query"],
    }

    compact = catalog.compact_schema(schema)

    assert compact["required"] == ["query"]
    assert compact["args"]["limit"] == "integer: Rows."
    assert compact["args"]["mode"] == "a|b"
    assert len(compact["args"]["query"]) < 140


def test_every_lean_core_tool_exists_and_the_list_is_small(core) -> None:
    names = {definition.name for definition in build_operations(core).definitions()}

    assert not set(catalog.LEAN_CORE) - names
    assert len(catalog.LEAN_CORE) == 14
    assert set(catalog.LEAN_CORE) >= catalog.DISCOVERY_TOOLS


def test_words_fold_plurals_and_case() -> None:
    assert catalog.tokens("Walls and DoorTypes") == ["wall", "door", "type"]
