"""Every recipe in the cookbook is executed, not just stored.

The point of shipping recipes is that the LLM retrieves code that ran. These
tests run them through the same namespace and compiler as execute_ifc_code:
`read` recipes with mutation locked, `edit` recipes against a throwaway copy.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from ifc_console.knowledge.recipes import RECIPES
from ifc_console.policy.guards import build_namespace
from ifc_console.session import executor

READ = [r for r in RECIPES if r.verify == "read"]
EDIT = [r for r in RECIPES if r.verify == "edit"]


def _run(recipe, ifc, *, allow_mutation: bool):
    namespace = build_namespace(
        ifc,
        allow_mutation=allow_mutation,
        allow_system=False,
        allowed_dirs=[],
    )
    compiled = executor.prepare(recipe.code, filename=f"<recipe:{recipe.slug}>")
    return executor.run(compiled, namespace, output_limit=20_000)


def test_the_cookbook_is_not_empty():
    assert len(READ) >= 10 and len(EDIT) >= 5


@pytest.mark.parametrize("recipe", READ, ids=lambda r: r.slug)
def test_read_recipe_runs_with_mutation_locked(recipe, ifc4):
    result = _run(recipe, ifc4, allow_mutation=False)
    assert result.result_repr is not None, "a recipe should end in a value"


@pytest.mark.parametrize("recipe", EDIT, ids=lambda r: r.slug)
def test_edit_recipe_runs_in_edit_mode(recipe, minimal_ifc4_path: Path, tmp_path: Path):
    import ifcopenshell

    copy = tmp_path / f"{recipe.slug}.ifc"
    shutil.copy2(minimal_ifc4_path, copy)
    ifc = ifcopenshell.open(str(copy))
    result = _run(recipe, ifc, allow_mutation=True)
    assert result.result_repr is not None


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda r: r.slug)
def test_recipe_mode_matches_the_classifier(recipe):
    """What the cookbook says a recipe needs is what the gate will decide."""
    from ifc_console.policy.classify import classify
    from ifc_console.policy.modes import OpClass

    verdict = classify(recipe.code).op_class
    expected = OpClass.EDIT if recipe.mode == "edit" else OpClass.QUERY
    assert verdict is expected, f"{recipe.slug} classified as {verdict}"


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda r: r.slug)
def test_recipe_metadata_is_consistent(recipe):
    assert recipe.code.strip(), "a recipe needs code"
    assert recipe.mode in ("ask", "edit")
    if recipe.mode == "edit":
        assert "ifc_api." in recipe.code, "edit recipes should use the API, not raw attributes"
    else:
        assert "ifc_api." not in recipe.code, "ask mode cannot reach ifc_api"
