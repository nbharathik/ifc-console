"""Built-in agent definitions and explicit host registration."""

from __future__ import annotations

import pytest

from ifc_console.agents.packs import AgentPackInfo, AgentPackRegistry
from ifc_console.testing import ScriptedAgentModel, text_round


class FakePack:
    def __init__(self, name: str = "fake", features: tuple[str, ...] = ()) -> None:
        self.info = AgentPackInfo(
            name=name,
            title=name.title(),
            description="a test pack",
            features=features,
        )

    async def build(self, runtime, *, model, viewer: bool = False):
        raise NotImplementedError


class TestRegistry:
    def test_host_app_can_register_a_trusted_pack_explicitly(self):
        registry = AgentPackRegistry()
        registry.register(FakePack())
        assert "fake" in [info.name for info in registry.installed()]
        assert registry.get("fake") is not None
        assert registry.is_builtin("fake") is False

    def test_bad_info_is_rejected(self):
        registry = AgentPackRegistry()

        class Broken:
            info = {"name": "broken"}

        with pytest.raises(TypeError):
            registry.register(Broken())

    def test_builtins_ship_active_with_no_allow_step(self):
        """The basic agents come with ifc-console itself and just appear."""
        registry = AgentPackRegistry()
        names = [info.name for info in registry.active()]
        assert "measurement" in names
        assert "docs" in names
        assert registry.is_builtin("measurement")
        docs = next(info for info in registry.active() if info.name == "docs")
        assert "files" in docs.features
        measure = next(info for info in registry.active() if info.name == "measurement")
        assert "files" in measure.features
        assert registry.problems == []


class TestPanelRuntime:
    async def test_bundled_pack_builds_over_the_running_core(self, core, work_model):
        from ifc_console.agents.panel import panel_runtime
        from ifc_console.application.operations import build_operations

        build_operations(core)
        await core.open_model(work_model)
        pack = core.agent_packs.get("measurement")
        assert pack is not None
        agent = await pack.build(panel_runtime(core), model=ScriptedAgentModel([]))
        assert "measure_elements" in agent.tools.names
        assert "measure__propose_measured_value" in agent.tools.names

    async def test_panel_runtime_is_built_once(self, core):
        from ifc_console.agents.panel import panel_runtime

        assert panel_runtime(core) is panel_runtime(core)

    async def test_pack_agent_answers_with_real_tools(self, core, work_model):
        from ifc_console.agents.panel import panel_runtime
        from ifc_console.application.operations import build_operations
        from ifc_console.testing import tool_call_round

        build_operations(core)
        await core.open_model(work_model)
        pack = core.agent_packs.get("measurement")
        scripted = ScriptedAgentModel(
            [
                tool_call_round({"name": "query_elements", "arguments": '{"query": "IfcWall"}'}),
                text_round("three walls"),
            ]
        )
        agent = await pack.build(panel_runtime(core), model=scripted)
        result = await agent.run("how many walls?")
        assert result.tool_calls[0].result["ok"] is True
        assert result.text == "three walls"


class TestBlueprints:
    def test_project_blueprint_is_atomic_and_appears_in_the_registry(self, tmp_path):
        from ifc_console.agents.blueprints import AgentBlueprint, AgentBlueprintStore

        blueprint = AgentBlueprint(
            name="custom-envelope-review",
            title="Envelope review",
            description="Review envelope evidence and measurements.",
            instructions="Use the project manual and report every source.",
            blocks=("ifc-context", "documents", "measurements"),
            starters=("Review the selected walls",),
        )
        store = AgentBlueprintStore(tmp_path)
        target = store.save(blueprint)
        assert target.is_file()
        assert not list(target.parent.glob("*.tmp"))

        registry = AgentPackRegistry(tmp_path)
        info = next(item for item in registry.active() if item.name == blueprint.name)
        assert info.kind == "custom"
        assert info.blocks == blueprint.blocks
        assert {"files", "vision"}.issubset(info.features)

    async def test_blueprint_builds_only_selected_block_tools(self, tmp_path):
        from ifc_console import LocalRuntime
        from ifc_console.agents.blueprints import AgentBlueprint, BlueprintPack

        blueprint = AgentBlueprint(
            name="custom-doc-reader",
            title="Doc reader",
            description="Read project references.",
            instructions="Answer with citations.",
            blocks=("documents",),
        )
        async with await LocalRuntime.open(
            home=tmp_path / "home", project_dir=tmp_path
        ) as runtime:
            agent = await BlueprintPack(blueprint).build(
                runtime, model=ScriptedAgentModel([]), viewer=False
            )

        assert "get_project_document_page" in agent.tools.names
        assert "measure_elements" not in agent.tools.names
        assert "preview_property_change" not in agent.tools.names
