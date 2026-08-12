"""AI-facing operations can preview changes but cannot approve or commit them."""

from __future__ import annotations


async def test_preview_and_get_changeset_are_projected_to_mcp(ask_harness) -> None:
    rows = await ask_harness.call(
        "query_elements", query="IfcWall, Pset_WallCommon.FireRating=F30", limit=1
    )
    global_id = rows["data"]["rows"][0]["global_id"]
    preview = await ask_harness.call(
        "preview_property_change",
        global_ids=[global_id],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value="F60",
    )
    assert preview["ok"] is True
    record = preview["data"]["change_set"]
    assert record["change_set"]["changes"][0]["before"] == "F30"
    assert record["change_set"]["changes"][0]["after"] == "F60"
    assert ask_harness.core.session.dirty is False

    restored = await ask_harness.call("get_change_set", change_set_id=record["change_set_id"])
    assert restored["ok"] is True
    assert restored["data"]["change_set"]["change_set_id"] == record["change_set_id"]

    classification = await ask_harness.call(
        "preview_classification_assignment",
        global_ids=[global_id],
        classification_name="Company Classification",
        identification="WALL-EXT",
        reference_name="External wall",
    )
    assert classification["ok"] is True
    assert (
        classification["data"]["change_set"]["change_set"]["operation"]
        == "classification.assign"
    )


async def test_ai_tool_surface_excludes_approval_commit_and_restore(ask_harness) -> None:
    tools = set(await ask_harness.list_tools())
    assert {
        "preview_property_change",
        "preview_classification_assignment",
        "get_change_set",
    } <= tools
    assert {"approve_change_set", "commit_change_set", "restore_commit"}.isdisjoint(tools)
