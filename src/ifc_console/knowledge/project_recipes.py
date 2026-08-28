"""Measurement recipes: the company's method for one property, as data.

A recipe pins what to execute (method and parameters for measure_elements)
and where the rule comes from (document and page). Recipes are YAML files in
the project's .ifc-console/recipes directory, written by hand or drafted from
ingested documents and approved by a human. The model can look them up but
never write them.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ifc_console.core.results import ToolError
from ifc_console.knowledge.records import Record

RECIPES_DIRNAME = "recipes"
_MAX_FILE_BYTES = 256_000


class RecipeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document: str
    page: int | None = Field(default=None, ge=1)
    section: str | None = None


class RecipeAppliesTo(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    ifc_class: str = Field(alias="class")
    type_name: str | None = None
    predefined_type: str | None = None


class MeasurementRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    applies_to: RecipeAppliesTo
    property: str
    method: Literal["stored_qto", "layer_sum", "geometry_extent"]
    params: dict[str, Any] = Field(default_factory=dict)
    unit: str | None = None
    tolerance: float | None = Field(default=None, ge=0)
    source: RecipeSource | None = None
    notes: str | None = None


@dataclass(frozen=True)
class LoadedRecipe:
    recipe: MeasurementRecipe
    file: str
    index: int

    @property
    def slug(self) -> str:
        stem = Path(self.file).stem
        return f"{stem}-{self.index + 1}"


def recipes_dir(project_dir: Path) -> Path:
    return project_dir / ".ifc-console" / RECIPES_DIRNAME


def load_recipes(project_dir: Path) -> tuple[list[LoadedRecipe], list[str]]:
    """Every valid recipe in the project, plus per-file problems."""
    import yaml

    directory = recipes_dir(project_dir)
    recipes: list[LoadedRecipe] = []
    problems: list[str] = []
    if not directory.is_dir():
        return recipes, problems
    for path in sorted(directory.glob("*.y*ml")):
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                problems.append(f"{path.name}: larger than {_MAX_FILE_BYTES} bytes, skipped")
                continue
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        entries = raw if isinstance(raw, list) else [raw]
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                problems.append(f"{path.name}[{index}]: not a mapping")
                continue
            try:
                recipe = MeasurementRecipe.model_validate(entry)
            except ValidationError as exc:
                first = exc.errors(include_url=False)[0]
                where = ".".join(str(part) for part in first.get("loc", ()))
                problems.append(f"{path.name}[{index}]: {where}: {first.get('msg')}")
                continue
            recipes.append(LoadedRecipe(recipe=recipe, file=path.name, index=index))
    return recipes, problems


def _matches(
    loaded: LoadedRecipe,
    ifc_class: str,
    prop: str,
    type_name: str | None,
    predefined_type: str | None,
) -> int:
    """Match specificity, with type names preferred over predefined types."""
    recipe = loaded.recipe
    if recipe.property.casefold() != prop.casefold():
        return 0
    if recipe.applies_to.ifc_class.casefold() != ifc_class.casefold():
        return 0
    specificity = 1
    predefined_pattern = recipe.applies_to.predefined_type
    if predefined_pattern is not None:
        if predefined_type is None or not fnmatch.fnmatch(
            predefined_type.casefold(), predefined_pattern.casefold()
        ):
            return 0
        specificity += 1
    type_pattern = recipe.applies_to.type_name
    if type_pattern is not None:
        if type_name is None or not fnmatch.fnmatch(type_name.casefold(), type_pattern.casefold()):
            return 0
        specificity += 2
    return specificity


def find_recipe(
    project_dir: Path,
    *,
    ifc_class: str,
    property_name: str,
    type_name: str | None = None,
    predefined_type: str | None = None,
) -> dict[str, Any]:
    """The most specific recipe for class, property, and optional type data."""
    recipes, problems = load_recipes(project_dir)
    scored = []
    for loaded in recipes:
        specificity = _matches(
            loaded,
            ifc_class,
            property_name,
            type_name,
            predefined_type,
        )
        if specificity:
            scored.append((specificity, loaded))
    if not scored:
        known = sorted({r.recipe.property for r in recipes})
        hint = (
            "No recipe matched. Fall back to search_ifc_knowledge(corpus='project') "
            "and choose a measure_elements method yourself, saying so in the report."
        )
        if known:
            hint += f" Recipes exist for: {', '.join(known)}."
        else:
            hint += (
                f" No recipes are defined yet; the user adds YAML files under "
                f"{recipes_dir(project_dir)}."
            )
        raise ToolError("NOT_FOUND", "no measurement recipe matched", hint)
    scored.sort(key=lambda item: (-item[0], item[1].file, item[1].index))
    specificity, best = scored[0]
    recipe = best.recipe

    arguments: dict[str, Any] = {"method": recipe.method, "metric": recipe.property}
    params = dict(recipe.params)
    if recipe.method == "stored_qto":
        arguments["quantity"] = params.pop("quantity", None)
        arguments["qto_set"] = params.pop("qto_set", None)
    elif recipe.method == "layer_sum":
        arguments["include_layers"] = params.pop("include_layers", None)
        arguments["exclude_layers"] = params.pop("exclude_layers", None)
    else:
        arguments["axis"] = params.pop("axis", "local_y")
    arguments = {k: v for k, v in arguments.items() if v is not None}

    result: dict[str, Any] = {
        "recipe": recipe.model_dump(mode="json", by_alias=True, exclude_none=True),
        "matched": {
            "class": ifc_class,
            "type_name": type_name,
            "predefined_type": predefined_type,
            "specificity": {
                1: "class",
                2: "predefined_type",
                3: "type",
                4: "type+predefined_type",
            }[specificity],
        },
        "file": best.file,
        "suggested_arguments": arguments,
    }
    if params:
        result["unused_params"] = params
    if len(scored) > 1:
        result["alternatives"] = len(scored) - 1
    if problems:
        result["problems"] = problems
    return result


def recipe_records(project_dir: Path) -> list[Record]:
    """Recipes as searchable records for the project index."""
    import yaml

    recipes, _ = load_recipes(project_dir)
    records = []
    for loaded in recipes:
        recipe = loaded.recipe
        source = recipe.source
        cite = f" (per {source.document} p.{source.page})" if source and source.page else ""
        name = f"{recipe.applies_to.ifc_class} {recipe.property} recipe"
        body = yaml.safe_dump(
            recipe.model_dump(mode="json", by_alias=True, exclude_none=True),
            sort_keys=False,
            allow_unicode=True,
        )
        meta: dict[str, Any] = {
            "path": f".ifc-console/recipes/{loaded.file}",
            "method": recipe.method,
            "aliases": [recipe.property, recipe.applies_to.ifc_class],
        }
        if recipe.applies_to.predefined_type is not None:
            meta["predefined_type"] = recipe.applies_to.predefined_type
        if source is not None:
            meta["source"] = source.model_dump(mode="json", exclude_none=True)
        records.append(
            Record(
                kind="recipe",
                key=f"recipe:project:{loaded.slug}",
                name=name,
                summary=f"{recipe.method} for {recipe.applies_to.ifc_class}"
                + (f" type {recipe.applies_to.type_name}" if recipe.applies_to.type_name else "")
                + (
                    f" predefined type {recipe.applies_to.predefined_type}"
                    if recipe.applies_to.predefined_type
                    else ""
                )
                + cite,
                body=body,
                meta=meta,
            )
        )
    return records


__all__ = [
    "LoadedRecipe",
    "MeasurementRecipe",
    "find_recipe",
    "load_recipes",
    "recipe_records",
    "recipes_dir",
]
