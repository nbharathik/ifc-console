"""Measurement recipes: loading, most-specific lookup, and indexing."""

from __future__ import annotations

from pathlib import Path

import pytest

from ifc_console.knowledge.project_recipes import find_recipe, load_recipes, recipes_dir
from ifc_console.mcp.envelope import ToolError

WALL_RECIPES = """
- applies_to: {class: IfcWall}
  property: thickness
  method: geometry_extent
  params: {axis: local_y}
  unit: mm
  notes: default for walls without layer data

- applies_to: {class: IfcWall, type_name: "Basic Wall: Interior*"}
  property: thickness
  method: layer_sum
  params: {exclude_layers: ["*Finish*", "*Render*"]}
  unit: mm
  tolerance: 2
  source: {document: "QS-Manual.pdf", page: 12}
  notes: structural layers only, per section 4.2
"""


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    directory = recipes_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / "walls.yaml").write_text(WALL_RECIPES, encoding="utf-8")
    return tmp_path


class TestLoading:
    def test_loads_both_recipes(self, project_dir: Path):
        recipes, problems = load_recipes(project_dir)
        assert len(recipes) == 2
        assert problems == []

    def test_no_recipes_dir_is_empty_not_an_error(self, tmp_path: Path):
        recipes, problems = load_recipes(tmp_path)
        assert recipes == [] and problems == []

    def test_invalid_entries_become_problems_not_crashes(self, project_dir: Path):
        bad = recipes_dir(project_dir) / "broken.yaml"
        bad.write_text(
            "applies_to: {class: IfcSlab}\nproperty: depth\nmethod: telepathy\n",
            encoding="utf-8",
        )
        recipes, problems = load_recipes(project_dir)
        assert len(recipes) == 2
        assert problems and "broken.yaml" in problems[0]


class TestLookup:
    def test_type_match_beats_class_match(self, project_dir: Path):
        result = find_recipe(
            project_dir,
            ifc_class="IfcWall",
            property_name="thickness",
            type_name="Basic Wall: Interior - 138mm",
        )
        assert result["matched"]["specificity"] == "type"
        assert result["recipe"]["method"] == "layer_sum"
        assert result["recipe"]["source"]["page"] == 12
        args = result["suggested_arguments"]
        assert args["method"] == "layer_sum"
        assert args["metric"] == "thickness"
        assert args["exclude_layers"] == ["*Finish*", "*Render*"]

    def test_class_recipe_answers_when_no_type_matches(self, project_dir: Path):
        result = find_recipe(
            project_dir,
            ifc_class="IfcWall",
            property_name="thickness",
            type_name="Curtain Wall System",
        )
        assert result["matched"]["specificity"] == "class"
        assert result["suggested_arguments"]["axis"] == "local_y"

    def test_property_match_is_case_insensitive(self, project_dir: Path):
        result = find_recipe(project_dir, ifc_class="ifcwall", property_name="Thickness")
        assert result["recipe"]["property"] == "thickness"

    def test_miss_points_at_project_search(self, project_dir: Path):
        with pytest.raises(ToolError) as excinfo:
            find_recipe(project_dir, ifc_class="IfcSlab", property_name="depth")
        assert excinfo.value.code == "NOT_FOUND"
        assert "corpus='project'" in excinfo.value.hint
        assert "thickness" in excinfo.value.hint

    def test_empty_project_names_the_recipes_directory(self, tmp_path: Path):
        with pytest.raises(ToolError) as excinfo:
            find_recipe(tmp_path, ifc_class="IfcWall", property_name="thickness")
        assert "recipes" in excinfo.value.hint


class TestIndexing:
    def test_recipes_are_searchable_beside_documents(self, project_dir: Path):
        from ifc_console.knowledge.project import ProjectKnowledge

        manual = project_dir / "qs-manual.md"
        manual.write_text("# QS Manual\n\n## Walls\n\nlayer rules", encoding="utf-8")
        project = ProjectKnowledge(project_dir)
        report = project.ingest([manual])
        assert report["recipes"] == 2

        hits = project.search("wall thickness recipe", kind="recipe")
        assert hits
        assert hits[0]["key"].startswith("recipe:project:")
        record = project.get(hits[0]["key"])
        assert "layer_sum" in record["body"] or "geometry_extent" in record["body"]


class TestTool:
    async def test_tool_round_trip(self, core, tmp_path: Path):
        from ifc_console.application.operations import build_operations

        directory = recipes_dir(tmp_path)
        directory.mkdir(parents=True)
        (directory / "walls.yaml").write_text(WALL_RECIPES, encoding="utf-8")
        service = build_operations(core)
        result = await service.call(
            "get_measurement_recipe",
            {"ifc_class": "IfcWall", "property_name": "thickness"},
        )
        assert result.ok is True
        assert result.data["recipe"]["method"] == "geometry_extent"

    async def test_tool_miss_is_a_clear_error(self, core):
        from ifc_console.application.operations import build_operations

        service = build_operations(core)
        result = await service.call(
            "get_measurement_recipe",
            {"ifc_class": "IfcBeam", "property_name": "span"},
        )
        assert result.ok is False
        assert result.error.code == "NOT_FOUND"
