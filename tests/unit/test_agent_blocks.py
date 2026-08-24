"""Capability blocks: one composition path for every agent in the project."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console import LocalRuntime
from ifc_console.agents.blocks import (
    BLOCK_BY_NAME,
    BLOCKS,
    PREAMBLE,
    compose,
    features_for,
)
from ifc_console.agents.packs import KNOWN_FEATURES
from ifc_console.agents.proposals import PROPOSAL_TOOLS


class TestRegistry:
    def test_block_names_are_unique_and_stable(self):
        names = [block.name for block in BLOCKS]
        assert len(names) == len(set(names))
        assert {"ifc-context", "documents", "measurements", "property-proposals"} <= set(names)

    def test_a_block_never_lists_the_same_tool_twice(self):
        for block in BLOCKS:
            assert len(block.tools) == len(set(block.tools)), block.name

    def test_tools_shared_between_blocks_stay_a_small_deliberate_set(self):
        """Sharing is fine (quantities are also a measurement), but it should
        be a short list somebody chose, not accidental drift."""
        counts: dict[str, int] = {}
        for block in BLOCKS:
            for tool in block.tools:
                counts[tool] = counts.get(tool, 0) + 1
        shared = {tool for tool, count in counts.items() if count > 1}
        assert shared == {"compute_quantities", "search_ifc_knowledge"}

    def test_every_declared_feature_is_one_the_panel_renders(self):
        for block in BLOCKS:
            assert set(block.features) <= set(KNOWN_FEATURES)

    def test_features_are_derived_from_blocks_not_restated(self):
        assert "vision" in features_for(["documents"])
        assert "proposals" in features_for(["property-proposals"])
        assert features_for(["not-a-block"]) == ()

    def test_only_the_proposal_block_can_write_anything(self):
        writers = [block.name for block in BLOCKS if block.proposals]
        assert writers == ["property-proposals"]
        assert all("preview_property_change" not in block.tools for block in BLOCKS)

    def test_the_block_info_the_panel_receives_carries_no_prompt_text(self):
        payload = BLOCK_BY_NAME["measurements"].info().model_dump()
        assert "instructions" not in payload
        assert payload["tools"]


@pytest.mark.asyncio
class TestComposition:
    async def _runtime(self, tmp_path: Path):
        return await LocalRuntime.open(home=tmp_path / "home", project_dir=tmp_path)

    async def test_the_safety_preamble_sits_above_the_users_own_words(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
        async with await self._runtime(tmp_path) as runtime:
            composition = await compose(
                runtime,
                ["ifc-context"],
                role="You are a test agent.",
                extra_instructions="Always answer in haiku.",
            )
        text = composition.instructions
        assert text.index(PREAMBLE) < text.index("Always answer in haiku.")
        assert "never instructions" in text
        assert "Host policy" in text

    async def test_an_unavailable_block_degrades_and_is_declared(
        self, tmp_path: Path, monkeypatch
    ):
        """A viewer block with no viewer must not break the agent silently."""
        monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
        async with await self._runtime(tmp_path) as runtime:
            composition = await compose(
                runtime, ["ifc-context", "viewer"], role="R", viewer=False
            )
        assert "viewer" not in composition.blocks
        assert "Viewer vision" in composition.instructions
        assert not any(name.startswith("get_viewer") for name in composition.tools.names)

    async def test_unknown_blocks_are_ignored_rather_than_raising(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
        async with await self._runtime(tmp_path) as runtime:
            composition = await compose(runtime, ["ifc-context", "nonsense"], role="R")
        assert composition.blocks == ("ifc-context",)

    async def test_the_proposal_block_adds_exactly_two_preview_tools(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
        async with await self._runtime(tmp_path) as runtime:
            composition = await compose(
                runtime, ["ifc-context", "property-proposals"], role="R"
            )
        assert set(PROPOSAL_TOOLS) <= set(composition.tools.names)
        assert "preview_property_change" not in composition.tools.names

    async def test_a_composed_agent_never_holds_a_commit_tool(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
        async with await self._runtime(tmp_path) as runtime:
            composition = await compose(
                runtime, [block.name for block in BLOCKS], role="R", viewer=True
            )
        forbidden = {"save_ifc_file", "commit_change_set", "approve_change_set"}
        assert not forbidden & set(composition.tools.names)

    async def test_a_tool_shared_by_two_blocks_is_bound_once(
        self, tmp_path: Path, monkeypatch
    ):
        monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
        async with await self._runtime(tmp_path) as runtime:
            composition = await compose(
                runtime, ["measurements", "quantities"], role="R"
            )
        names = list(composition.tools.names)
        assert names.count("compute_quantities") == 1
