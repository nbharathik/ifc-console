"""The bundled agents end to end, offline, with scripted models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ifc_console import LocalRuntime
from ifc_console.agents.blocks import BLOCK_BY_NAME
from ifc_console.agents.builtin import builtin_packs
from ifc_console.agents.builtin.docs import PACK as DOCS_PACK
from ifc_console.agents.builtin.measure import BLOCKS as MEASURE_BLOCKS
from ifc_console.agents.builtin.measure import (
    MeasurementReport,
    build_agent,
    build_proposal_source,
    report_to_csv,
)
from ifc_console.agents.proposals import PROPOSAL_TOOLS
from ifc_console.agents.provenance import (
    MEASUREMENT_PSET,
    PROPERTY_PSET,
    PROVENANCE_PSET,
    provenance_property_name,
)
from ifc_console.agents.runner import resolve_model_file
from ifc_console.testing import ScriptedAgentModel, text_round, tool_call_round

RECIPE = """
applies_to: {class: IfcWall}
property: thickness
method: geometry_extent
params: {axis: local_y}
unit: mm
source: {document: "QS-Manual.md", page: 1}
"""


def build_project(root: Path) -> Path:
    """A project folder: one wall model, one recipe, one manual."""
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.project
    import ifcopenshell.api.root
    import ifcopenshell.api.unit

    ifc = ifcopenshell.api.project.create_file(version="IFC4")
    ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcProject", name="P")
    unit = ifcopenshell.api.unit.add_si_unit(ifc, unit_type="LENGTHUNIT", prefix="MILLI")
    ifcopenshell.api.unit.assign_unit(ifc, units=[unit])
    model = ifcopenshell.api.context.add_context(ifc, context_type="Model")
    body = ifcopenshell.api.context.add_context(
        ifc,
        context_type="Model",
        context_identifier="Body",
        target_view="MODEL_VIEW",
        parent=model,
    )
    wall = ifcopenshell.api.root.create_entity(ifc, ifc_class="IfcWall", name="Wall-1")
    representation = ifcopenshell.api.geometry.add_wall_representation(
        ifc, context=body, length=5.0, height=3.0, thickness=0.2
    )
    ifcopenshell.api.geometry.assign_representation(
        ifc, product=wall, representation=representation
    )
    root.mkdir(parents=True, exist_ok=True)
    ifc.write(str(root / "tower.ifc"))

    recipes = root / ".ifc-console" / "recipes"
    recipes.mkdir(parents=True)
    (recipes / "walls.yaml").write_text(RECIPE, encoding="utf-8")
    (root / "QS-Manual.md").write_text(
        "# QS Manual\n\n## Wall thickness\n\nMeasure the geometric extent.",
        encoding="utf-8",
    )
    return root / "tower.ifc"


FINAL_ANSWER = json.dumps(
    {
        "metric": "thickness",
        "scope": "IfcWall (1 element)",
        "method": "geometry_extent",
        "unit": "MILLIMETRE",
        "source": "QS-Manual.md p.1",
        "elements": [
            {
                "global_id": "placeholder",
                "name": "Wall-1",
                "value": 200.0,
                "unit": "MILLIMETRE",
                "method": "geometry_extent",
                "source": "QS-Manual.md p.1",
            }
        ],
        "notes": None,
    }
)


def test_the_general_agent_ships_first_and_holds_the_others_capabilities():
    """One agent does everything; the focused presets are narrower views of it."""
    packs = builtin_packs()
    names = [pack.info.name for pack in packs]
    assert names[0] == "general"
    assert {"measurement", "docs", "review"} <= set(names)
    general = packs[0].info
    for pack in packs[1:]:
        assert set(pack.info.blocks) <= set(general.blocks), pack.info.name
    assert "files" in DOCS_PACK.info.features


@pytest.mark.asyncio
async def test_measure_worked_turn_offline(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model_file = build_project(tmp_path / "project")

    scripted = ScriptedAgentModel(
        [
            tool_call_round({"name": "query_elements", "arguments": '{"query": "IfcWall"}'}),
            tool_call_round(
                {
                    "name": "get_measurement_recipe",
                    "arguments": '{"ifc_class": "IfcWall", "property_name": "thickness"}',
                }
            ),
            tool_call_round(
                {
                    "name": "measure_elements",
                    "arguments": json.dumps(
                        {
                            "selector": "IfcWall",
                            "method": "geometry_extent",
                            "metric": "thickness",
                            "axis": "local_y",
                        }
                    ),
                }
            ),
            text_round(FINAL_ANSWER),
        ]
    )

    runtime = await LocalRuntime.open(home=tmp_path / "home", project_dir=model_file.parent)
    async with runtime:
        await runtime.open_model(model_file)
        agent = await build_agent(runtime, model=scripted)
        result = await agent.run(
            "measure the thickness of all walls", response_model=MeasurementReport
        )

    by_name = {record.name: record for record in result.tool_calls}
    recipe_result = by_name["get_measurement_recipe"].result
    assert recipe_result["ok"] is True
    assert recipe_result["data"]["recipe"]["source"]["document"] == "QS-Manual.md"
    element = by_name["measure_elements"].result["data"]["elements"][0]
    assert element["value"] == pytest.approx(200.0, rel=0.02)
    assert isinstance(result.data, MeasurementReport)

    report_path = report_to_csv(result.data, tmp_path / "out" / "report.csv")
    text = report_path.read_text(encoding="utf-8")
    assert "Wall-1" in text and "geometry_extent" in text


@pytest.mark.asyncio
async def test_measure_toolset_is_scoped(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model_file = build_project(tmp_path / "project")
    runtime = await LocalRuntime.open(home=tmp_path / "home", project_dir=model_file.parent)
    async with runtime:
        await runtime.open_model(model_file)
        agent = await build_agent(runtime, model=ScriptedAgentModel([]))
        expected = {
            tool
            for name in MEASURE_BLOCKS
            if not BLOCK_BY_NAME[name].viewer_only
            for tool in BLOCK_BY_NAME[name].tools
        } | set(PROPOSAL_TOOLS)
        assert set(agent.tools.names) == expected
        assert "get_viewer_selection" not in agent.tools.names
        assert "- measure_elements:" in agent.instructions
        assert "IfcConsole_AI_" in agent.instructions


@pytest.mark.asyncio
async def test_measurement_proposals_are_marked_as_ai_assisted(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model_file = build_project(tmp_path / "project")
    async with await LocalRuntime.open(
        model_file, home=tmp_path / "home", project_dir=model_file.parent
    ) as runtime:
        wall = (await runtime.workbench.query("IfcWall"))[0]
        proposals: list[str] = []
        source = build_proposal_source(runtime, proposals)
        tools = await runtime.tools("measure__propose_measured_value", sources=(source,))
        result = await tools.call(
            "measure__propose_measured_value",
            {"global_ids": [wall["global_id"]], "metric": "thickness", "value": 200.0},
        )

    record = result["data"]["change_set"]
    changes = record["change_set"]["changes"]
    assert len(changes) == 2
    assert changes[0]["pset_name"] == MEASUREMENT_PSET
    assert changes[0]["property_name"] == "MeasuredThickness"
    assert changes[1]["pset_name"] == PROVENANCE_PSET
    assert changes[1]["property_name"] == provenance_property_name(
        MEASUREMENT_PSET, "MeasuredThickness"
    )
    marker = json.loads(changes[1]["after"])
    assert marker["property"] == f"{MEASUREMENT_PSET}.MeasuredThickness"
    assert marker["proposal_id"]
    assert result["data"]["provenance_change_set"] == record["change_set_id"]
    assert result["data"]["ai_marked"] is True
    assert proposals == [record["change_set_id"]]


@pytest.mark.asyncio
async def test_truncated_proposal_result_keeps_atomic_change_set_id(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model_file = build_project(tmp_path / "project")
    async with await LocalRuntime.open(
        model_file,
        home=tmp_path / "home",
        project_dir=model_file.parent,
        settings={"exec.output_char_limit": 1000},
    ) as runtime:
        wall = (await runtime.workbench.query("IfcWall"))[0]
        proposals: list[str] = []
        source = build_proposal_source(runtime, proposals)
        tools = await runtime.tools("measure__propose_measured_value", sources=(source,))
        result = await tools.call(
            "measure__propose_measured_value",
            {"global_ids": [wall["global_id"]], "metric": "thickness", "value": 200.0},
        )
        identifier = result["data"]["change_set"]["change_set_id"]
        stored = runtime.workbench.change_set(identifier)

    assert result["meta"]["truncated"] is True
    assert result["data"]["ai_marked"] is True
    assert result["data"]["provenance_change_set"] == identifier
    assert proposals == [identifier]
    assert len(stored.change_set.changes) == 2


@pytest.mark.asyncio
async def test_multiple_ai_properties_keep_provenance_when_one_is_updated(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model_file = build_project(tmp_path / "project")
    async with await LocalRuntime.open(
        model_file,
        mode="edit",
        home=tmp_path / "home",
        project_dir=model_file.parent,
    ) as runtime:
        wall = (await runtime.workbench.query("IfcWall"))[0]
        global_id = wall["global_id"]
        proposals: list[str] = []
        source = build_proposal_source(runtime, proposals)
        tools = await runtime.tools(*PROPOSAL_TOOLS, sources=(source,))

        async def propose_commit(tool_name: str, arguments: dict) -> str:
            result = await tools.call(tool_name, arguments)
            assert result["ok"] is True
            identifier = result["data"]["change_set"]["change_set_id"]
            approval = runtime.workbench.approve_change_set(identifier, approved_by="test")
            await runtime.workbench.commit_change_set(identifier, approval_id=approval.approval_id)
            return identifier

        first = await propose_commit(
            "measure__propose_measured_value",
            {"global_ids": [global_id], "metric": "thickness", "value": 200.0},
        )
        second = await propose_commit(
            "measure__propose_property_value",
            {
                "global_ids": [global_id],
                "property_name": "ReviewSource",
                "value": "schedule A",
                "method": "schedule_lookup",
            },
        )
        third = await propose_commit(
            "measure__propose_measured_value",
            {"global_ids": [global_id], "metric": "thickness", "value": 250.0},
        )
        inventory = await runtime.call("list_ai_authored_properties")

    assert proposals == [first, second, third]
    row = inventory["data"]["elements"][0]
    measured_key = f"{MEASUREMENT_PSET}.MeasuredThickness"
    property_key = f"{PROPERTY_PSET}.ReviewSource"
    assert row["properties"][measured_key] == 250.0
    assert row["properties"][property_key] == "schedule A"
    assert set(row["provenance_by_property"]) == {measured_key, property_key}
    assert row["provenance_by_property"][property_key]["method"] == "schedule_lookup"
    assert isinstance(row["provenance"], dict)


@pytest.mark.asyncio
async def test_docs_answers_from_the_ingested_corpus(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    project = tmp_path / "project"
    project.mkdir()
    (project / "handbook.md").write_text(
        "# Handbook\n\n## Submissions\n\nEvery submission needs an IDS check.",
        encoding="utf-8",
    )
    runtime = await LocalRuntime.open(home=tmp_path / "home", project_dir=project)
    async with runtime:
        await runtime.workbench.ingest_docs([project / "handbook.md"])
        scripted = ScriptedAgentModel(
            [
                tool_call_round(
                    {
                        "name": "search_ifc_knowledge",
                        "arguments": '{"query": "submission requirements", "corpus": "project"}',
                    }
                ),
                text_round("Every submission needs an IDS check (handbook.md, Submissions)."),
            ]
        )
        agent = await DOCS_PACK.build(runtime, model=scripted)
        result = await agent.run("what do submissions need?")

    hits = result.tool_calls[0].result["data"]["hits"]
    assert hits and hits[0]["meta"]["path"] == "handbook.md"
    assert "IDS check" in result.text


def test_resolve_model_file_prefers_the_only_model(tmp_path: Path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.ifc").write_text("ISO-10303-21;", encoding="utf-8")
    project_dir, model = resolve_model_file(project)
    assert project_dir == project
    assert model is not None and model.name == "a.ifc"
    (project / "b.ifc").write_text("ISO-10303-21;", encoding="utf-8")
    with pytest.raises(SystemExit):
        resolve_model_file(project)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert resolve_model_file(empty) == (empty, None)
