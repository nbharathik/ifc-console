"""The skills store and its three tools."""

from __future__ import annotations

import pytest

from ifc_console.agents.skills import AgentSkillStore
from ifc_console.core.results import ToolError

BODY = "## When to use\nSheet piles.\n\n## Steps\n1. analyze_element_geometry\n"


class TestSkillStore:
    def test_save_read_and_list_round_trip(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        row = store.save(
            "sheet-pile-profile",
            BODY,
            description="Measure a sheet pile profile",
            applies_to="IfcMember sheet piles",
        )
        assert row["name"] == "sheet-pile-profile"
        assert row["path"].endswith("sheet-pile-profile.md")

        loaded = store.read("sheet-pile-profile")
        assert loaded["description"] == "Measure a sheet pile profile"
        assert loaded["applies_to"] == "IfcMember sheet piles"
        assert "analyze_element_geometry" in loaded["content"]
        assert "---" not in loaded["content"]

        entries = store.entries()
        assert [entry["name"] for entry in entries] == ["sheet-pile-profile"]
        assert "content" not in entries[0]

    def test_overwrite_is_explicit(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.save("a-skill", BODY, description="one")
        with pytest.raises(ToolError) as caught:
            store.save("a-skill", BODY, description="two")
        assert caught.value.code == "FILE_EXISTS"
        store.save("a-skill", BODY, description="two", overwrite=True)
        assert store.read("a-skill")["description"] == "two"

    def test_names_are_slugs_only(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        for bad in ("../escape", "UPPER", "a b", "", "x"):
            with pytest.raises(ToolError):
                store.save(bad, BODY, description="nope")

    def test_a_missing_skill_names_what_exists(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.save("known", BODY, description="here")
        with pytest.raises(ToolError) as caught:
            store.read("unknown")
        assert caught.value.code == "NOT_FOUND"
        assert "known" in caught.value.hint

    def test_import_takes_external_markdown_as_it_comes(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        with_header = (
            b"---\nname: pile-check\ndescription: Check a pile\napplies_to: IfcPile\n---\n\n"
            b"Steps here.\n"
        )
        row = store.import_file("Anything At All.md", with_header)
        assert row["name"] == "pile-check"
        assert store.read("pile-check")["applies_to"] == "IfcPile"

        bare = b"# Measure openings\n\n1. query_elements\n"
        row = store.import_file("Measure Openings (v2).md", bare)
        assert row["name"] == "measure-openings-v2"
        assert row["description"] == "Measure openings"

        again = store.import_file("Anything.md", with_header)
        assert again["name"] == "pile-check-2"

        with pytest.raises(ToolError) as caught:
            store.import_file("x" * 40 + ".md", b"a" * (64 * 1024 + 1))
        assert caught.value.code == "INVALID_INPUT"

    def test_hand_written_front_matter_is_parsed(self, tmp_path):
        store = AgentSkillStore(tmp_path)
        store.directory.mkdir(parents=True)
        (store.directory / "manual.md").write_text(
            "---\nname: manual\ndescription: written by hand\n---\n\nBody text.\n",
            encoding="utf-8",
        )
        entry = store.entries()[0]
        assert entry["name"] == "manual"
        assert entry["description"] == "written by hand"


class TestSkillTools:
    async def _ops(self, core):
        from ifc_console.application.operations import build_operations

        return build_operations(core)

    async def test_the_agent_can_record_and_reuse_a_skill(self, core):
        ops = await self._ops(core)
        saved = await ops.call(
            "save_agent_skill",
            {
                "name": "sheet-pile-profile",
                "description": "Measure a sheet pile",
                "content": BODY,
                "applies_to": "IfcMember",
            },
        )
        assert saved.ok is True
        assert saved.data["saved"] is True

        listed = await ops.call("list_agent_skills", {})
        assert listed.ok is True
        assert listed.data["count"] == 1
        assert listed.data["skills"][0]["name"] == "sheet-pile-profile"

        loaded = await ops.call("get_agent_skill", {"name": "sheet-pile-profile"})
        assert loaded.ok is True
        assert "analyze_element_geometry" in loaded.data["content"]

    async def test_an_empty_project_hints_at_recording(self, core):
        ops = await self._ops(core)
        listed = await ops.call("list_agent_skills", {})
        assert listed.data["count"] == 0
        assert "save_agent_skill" in listed.data["note"]

    async def test_a_tool_argument_named_name_survives_the_agent_toolset(self, core):
        """Regression: `name` used to collide with the workbench call's own
        first parameter and die as TOOL_SOURCE_FAILED before reaching the tool."""
        from ifc_console.agents.panel import panel_runtime

        await self._ops(core)
        toolset = await panel_runtime(core).toolset()
        result = await toolset.call("get_agent_skill", {"name": "does-not-exist"})
        assert result["ok"] is False
        assert result["error"]["code"] == "NOT_FOUND"

        saved = await toolset.call(
            "save_agent_skill",
            {"name": "via-panel", "description": "d", "content": BODY},
        )
        assert saved["ok"] is True

    async def test_saving_requires_approval_in_agent_surfaces(self, core):
        from ifc_console.agents.panel import panel_runtime

        await self._ops(core)
        toolset = await panel_runtime(core).toolset()
        by_name = {definition.name: definition for definition in toolset.definitions}
        assert by_name["save_agent_skill"].requires_approval is True
        assert by_name["get_agent_skill"].requires_approval is False
