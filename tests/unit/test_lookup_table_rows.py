"""Table row lookup: tolerant name matching, columns in the answer, nearest ranking."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ifc_console.mcp.tools_knowledge import _field_equals, _loose

pytestmark = pytest.mark.asyncio


def test_loose_matching_ignores_spaces_and_case():
    assert _loose(" GU 6N ") == "gu6n"
    assert _field_equals("GU 6N", "gu6n")
    assert _field_equals("AZ 26-700N", "AZ26-700N")
    assert _field_equals(700, "700")
    assert _field_equals(12.2, 12.2)
    assert not _field_equals("GU 6N", "GU 7N")
    assert not _field_equals("x", 1)


async def test_lookup_finds_rows_by_loose_name_and_lists_columns(core, tmp_path: Path):
    table = tmp_path / "u-sections.jsonl"
    rows = [
        {
            "id": "gu-6n:per_m_wall",
            "designation": "GU 6N",
            "basis": "per_m_wall",
            "width_b_mm": 600,
            "height_h_mm": 309,
            "source_page": 18,
            "verified": True,
        },
        {
            "id": "gu-7n:per_m_wall",
            "designation": "GU 7N",
            "basis": "per_m_wall",
            "width_b_mm": 600,
            "height_h_mm": 310,
            "source_page": 18,
            "verified": True,
        },
    ]
    table.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    core.project_knowledge.ingest([table])
    from ifc_console.application.operations import build_operations

    ops = build_operations(core)

    async def call(name, **arguments):
        return (await ops.call(name, arguments)).model_dump()

    found = await call("lookup_table_rows", table="u-sections", where={"designation": "gu6n"})
    assert found["ok"], found
    assert [hit["row"]["designation"] for hit in found["data"]["rows"]] == ["GU 6N"]
    assert "width_b_mm" in found["data"]["columns"]

    nearest = await call(
        "lookup_table_rows", table="u-sections", nearest={"width_b_mm": 600, "height_h_mm": 309.6}
    )
    ranked = [(hit["row"]["designation"], hit["residual_sum"]) for hit in nearest["data"]["rows"]]
    assert ranked[0][0] == "GU 7N" and ranked[1][0] == "GU 6N"

    empty = await call("lookup_table_rows", table="u-sections", where={"designation": "nope"})
    assert empty["data"]["rows"] == [] and "columns of this table" in empty["data"]["hint"]


async def test_lookup_fields_star_and_note_keep_results_small(core, tmp_path: Path):
    table = tmp_path / "z-sections.jsonl"
    rows = [
        {
            "id": f"az-{n}:per_m_wall",
            "designation": f"AZ {n}",
            "basis": "per_m_wall",
            "width_b_mm": 700 + n,
            "source_page": 20,
            "verified": True,
        }
        for n in range(1, 5)
    ]
    table.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    core.project_knowledge.ingest([table])
    from ifc_console.application.operations import build_operations

    ops = build_operations(core)

    async def call(name, **arguments):
        return (await ops.call(name, arguments)).model_dump()

    picked = await call(
        "lookup_table_rows",
        table="z-sections",
        where={"designation": "AZ 2"},
        fields=["designation", "width_b_mm"],
    )
    hit = picked["data"]["rows"][0]
    assert hit["row"] == {"designation": "AZ 2", "width_b_mm": 702}
    assert hit["table"] == "z-sections" and "corpus" not in hit

    everything = await call("lookup_table_rows", table="*", limit=2)
    assert everything["data"]["matched"] >= 4 and len(everything["data"]["rows"]) == 2
    assert "more rows match" in everything["data"]["note"]
    assert "z-sections" in everything["data"]["known_tables"]
