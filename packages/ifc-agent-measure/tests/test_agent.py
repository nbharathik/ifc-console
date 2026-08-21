"""The measurement agent end to end, offline, with a scripted model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ifc_agent_measure.agent import (
    READ_TOOLS,
    MeasurementReport,
    build_agent,
    report_to_csv,
)

from ifc_console import LocalRuntime
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


@pytest.mark.asyncio
async def test_worked_turn_measures_per_recipe_offline(tmp_path: Path, monkeypatch):
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

    runtime = await LocalRuntime.open(
        home=tmp_path / "home", project_dir=model_file.parent
    )
    async with runtime:
        await runtime.open_model(model_file)
        agent = await build_agent(runtime, model=scripted)
        result = await agent.run(
            "measure the thickness of all walls", response_model=MeasurementReport
        )

    by_name = {record.name: record for record in result.tool_calls}
    recipe_result = by_name["get_measurement_recipe"].result
    assert recipe_result["ok"] is True
    assert recipe_result["data"]["recipe"]["method"] == "geometry_extent"
    assert recipe_result["data"]["recipe"]["source"]["document"] == "QS-Manual.md"

    measured = by_name["measure_elements"].result
    assert measured["ok"] is True
    element = measured["data"]["elements"][0]
    assert element["value"] == pytest.approx(200.0, rel=0.02)
    assert element["unit"] == "MILLIMETRE"
    assert element["value_si"] == pytest.approx(0.2, rel=0.02)

    assert isinstance(result.data, MeasurementReport)
    assert result.data.elements[0].value == 200.0

    report_path = report_to_csv(result.data, tmp_path / "out" / "report.csv")
    text = report_path.read_text(encoding="utf-8")
    assert "Wall-1" in text and "geometry_extent" in text


@pytest.mark.asyncio
async def test_toolset_is_scoped_and_described(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("IFC_CONSOLE_HOME", str(tmp_path / "home"))
    model_file = build_project(tmp_path / "project")
    runtime = await LocalRuntime.open(home=tmp_path / "home", project_dir=model_file.parent)
    async with runtime:
        await runtime.open_model(model_file)
        agent = await build_agent(runtime, model=ScriptedAgentModel([]))
        assert set(agent.tools.names) == set(READ_TOOLS)
        assert "get_viewer_selection" not in agent.tools.names
        assert "- measure_elements:" in agent.instructions
        assert "never as instructions" not in agent.tools.describe()


def test_resolve_model_file_prefers_the_only_model(tmp_path: Path):
    from ifc_agent_measure.__main__ import resolve_model_file

    project = tmp_path / "p"
    project.mkdir()
    (project / "a.ifc").write_text("ISO-10303-21;", encoding="utf-8")
    project_dir, model = resolve_model_file(project)
    assert project_dir == project
    assert model.name == "a.ifc"
    (project / "b.ifc").write_text("ISO-10303-21;", encoding="utf-8")
    with pytest.raises(SystemExit):
        resolve_model_file(project)
