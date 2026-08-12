# Contributing to ifc-console

Thanks for considering a contribution.

- **Setup, tests, style, release process**: see the
  [development guide](https://nbharathik.github.io/ifc-console/development/).
  Short version: `uv sync --extra dev`, `uv run pytest`,
  `uv run ruff check src tests packages scripts`; all three must be green.
- **Bugs and feature requests**: open a GitHub issue. For bugs, include the
  output of `ifc-console doctor` and, if a model is involved, its schema and
  rough size (never upload confidential models).
- **Security issues**: do not open a public issue; see
  [SECURITY.md](SECURITY.md).
- **Scope**: ifc-console is deliberately small: a safe MCP bridge to
  IfcOpenShell plus a console, an SDK, and an optional viewer. Features that
  grow the attack surface of `execute_ifc_code`, weaken the mode model, or add
  network calls need a strong case. The knowledge index is built from the
  installed ifcopenshell and stays offline by design.
- **Compatibility**: the MCP tool names, input schemas, response envelope, and
  documented top-level `ifc_console` exports are public API. Changes to them
  need a changelog entry and, pre-1.0, a minor version bump. Run
  `python scripts/snapshot_api.py` and review the golden diff like any other
  contract change.
- **Recipes**: every entry in the cookbook is executed by
  `tests/unit/test_recipes.py`. A recipe that is not verified does not ship.
