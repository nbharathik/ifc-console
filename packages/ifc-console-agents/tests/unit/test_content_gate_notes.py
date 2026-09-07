"""The content gate says what it hid instead of returning a silently empty result."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ifc_console_agents.content import AgentContentGate

pytestmark = pytest.mark.asyncio


def _result(**data):
    return {"ok": True, "data": data, "meta": {"returned": 2}}


async def test_lookup_rows_report_hidden_rows_and_a_note():
    gate = AgentContentGate(("agents/references/demo/allowed.jsonl",))
    call = SimpleNamespace(name="lookup_table_rows", arguments={"table": "rows"})

    async def call_next(_call):
        return _result(
            rows=[
                {"path": "agents/references/demo/allowed.jsonl", "row": {"a": 1}},
                {"path": "agents/packs/other/data/rows.jsonl", "row": {"a": 2}},
            ],
            matched=2,
        )

    out = await gate(call, call_next)
    assert [row["row"]["a"] for row in out["data"]["rows"]] == [1]
    assert out["data"]["hidden"] == 1
    assert "content access" in out["data"]["access_note"]
    assert "Agent workspace > Content" in out["data"]["access_note"]


async def test_project_search_hits_carry_the_note_only_when_something_was_hidden():
    gate = AgentContentGate(("a.md",))

    async def call_next(_call):
        return _result(hits=[{"meta": {"path": "a.md"}}, {"meta": {"path": "b.md"}}])

    hidden = await gate(
        SimpleNamespace(name="search_ifc_knowledge", arguments={"corpus": "project"}), call_next
    )
    assert hidden["data"]["hidden"] == 1 and "1 hit(s) hidden" in hidden["data"]["access_note"]

    async def call_all(_call):
        return _result(hits=[{"meta": {"path": "a.md"}}])

    clean = await gate(
        SimpleNamespace(name="search_ifc_knowledge", arguments={"corpus": "project"}), call_all
    )
    assert "hidden" not in clean["data"] and "access_note" not in clean["data"]


async def test_temporary_grant_opens_the_hidden_files_for_one_run():
    gate = AgentContentGate(())
    call = SimpleNamespace(name="list_project_documents", arguments={})

    async def call_next(_call):
        return _result(files=[{"path": "agents/packs/p/knowledge/a.md"}])

    blocked = await gate(call, call_next)
    assert blocked["data"]["files"] == [] and blocked["data"]["hidden"] == 1
    with gate.temporary(["agents/packs/p/knowledge/a.md"]):
        allowed = await gate(call, call_next)
    assert len(allowed["data"]["files"]) == 1 and "hidden" not in allowed["data"]
