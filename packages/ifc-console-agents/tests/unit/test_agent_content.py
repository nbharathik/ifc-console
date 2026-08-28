"""Shared agent content access settings and tool filtering."""

from __future__ import annotations

import json

from ifc_console.toolsets import ToolCall

from ifc_console_agents.content import AgentContentAccessStore, AgentContentGate


def test_content_store_keeps_legacy_access_all_and_persists_empty_selection(tmp_path):
    store = AgentContentAccessStore(tmp_path)

    assert store.get("docs") is None
    store.set("docs", [])
    assert store.get("docs") == ()

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload == {"version": 1, "agents": {"docs": []}}
    assert not list(store.path.parent.glob("*.tmp"))

    store.set("docs", None)
    assert store.get("docs") is None

    store.path.write_text("not json", encoding="utf-8")
    assert store.get("docs") == ()


async def test_content_gate_filters_search_and_document_listing():
    allowed = ".ifc-console/agents/references/allowed.md"
    denied = ".ifc-console/agents/references/private.md"
    gate = AgentContentGate((allowed,))

    async def call_next(_call):
        return {
            "ok": True,
            "data": {
                "files": [{"path": allowed}, {"path": denied}],
                "hits": [
                    {"key": "doc:allowed#1", "meta": {"path": allowed}},
                    {"key": "doc:private#1", "meta": {"path": denied}},
                    {"key": "recipe:project", "meta": {}},
                ],
            },
            "meta": {"returned": 3},
        }

    listed = await gate(
        ToolCall(id="1", name="list_project_documents", arguments={}), call_next
    )
    assert listed["data"]["files"] == [{"path": allowed}]
    assert listed["meta"]["returned"] == 1

    searched = await gate(
        ToolCall(
            id="2",
            name="search_ifc_knowledge",
            arguments={"query": "wall", "corpus": "project"},
        ),
        call_next,
    )
    assert [row["key"] for row in searched["data"]["hits"]] == [
        "doc:allowed#1",
        "recipe:project",
    ]


async def test_content_gate_denies_turn_reads_even_in_all_mode_then_allows_attachment():
    attached = ".ifc-console/agents/references/.turns/attached.png"
    gate = AgentContentGate(None)
    calls = 0

    async def call_next(_call):
        nonlocal calls
        calls += 1
        return {"ok": True, "data": {"images": 1}, "meta": {}}

    denied = await gate(
        ToolCall(
            id="1",
            name="get_project_reference_image",
            arguments={"path": attached},
        ),
        call_next,
    )
    assert denied["error"]["code"] == "CONTENT_ACCESS_DENIED"
    assert calls == 0

    with gate.temporary((attached,)):
        accepted = await gate(
            ToolCall(
                id="2",
                name="get_project_reference_image",
                arguments={"path": attached},
            ),
            call_next,
        )
    assert accepted["ok"] is True
    assert calls == 1


async def test_content_gate_hides_turn_files_from_all_mode_listings():
    standing = ".ifc-console/agents/references/manual.md"
    attached = ".ifc-console/agents/references/.turns/question.md"
    gate = AgentContentGate(None)

    async def call_next(_call):
        return {
            "ok": True,
            "data": {"files": [{"path": standing}, {"path": attached}]},
            "meta": {"returned": 2},
        }

    result = await gate(
        ToolCall(id="1", name="list_project_documents", arguments={}), call_next
    )

    assert result["data"]["files"] == [{"path": standing}]
    assert result["meta"]["returned"] == 1
