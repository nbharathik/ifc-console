# Extensions

ifc-console is the core package; agents are separate products built on top of
it. Each one installs on its own, runs in its own environment, and can be
shipped to a single customer. The main logic stays in core, where every MCP
client benefits; an extension carries only what makes it a product: the
instructions, the scoped toolset, and company-specific tools.

```text
extension catalog          private customer repo        PyPI: ifc-console
      |                            |                          |
      +---- extensions install ----+---- dependency, pinned --+
                       |
        agent extension, own isolated environment
                       |
     opens a project folder          attaches to a running
     (LocalRuntime)                  console (ConsoleRuntime, MCP)
```

## The store

Store v1 is a static catalog, no server: `catalog.json` in the
`ifc-console-extensions` repository, with a bundled seed as the offline
fallback.

```bash
ifc-console extensions search measure     # search the catalog
ifc-console extensions install measure    # uv tool install, isolated env
ifc-console extensions list               # catalog + what is installed here
ifc-console extensions uninstall measure
```

`install` puts the agent in its own environment via `uv tool install`, so two
agents never fight over dependency versions and each can pin its own
ifc-console. What was installed is recorded in
`~/.ifc-console/extensions.json`; the console never imports an agent, it runs
as its own process (`ifc-measure ...`). The TUI shows the same picture with
`/extensions`.

Private customer agents are never listed in the public catalog. They install
directly by requirement:

```bash
ifc-console extensions install git+https://github.com/acme/ifc-agent-acme.git
```

Operation plugins (the same-environment, deny-by-default kind documented in
[Plugins](plugins.md)) appear in the catalog too, but install with pip and
`plugins.allow`, not `uv tool`.

## Two run modes

Every templated agent supports both:

- **Standalone**: the agent opens a project folder directly
  (`ifc-measure path/to/project`). The folder convention: models beside the
  company documents, with settings, the ingested knowledge index, and
  measurement recipes under `.ifc-console/`.
- **Attach**: the agent connects to a running console over MCP
  (`--attach http://127.0.0.1:8383/mcp --token TOKEN`). The console owner
  keeps the mode switch and approvals. The compatibility surface is the MCP
  protocol plus the `{ok, data, meta}` envelope the contract snapshots guard.

## Build your own

```bash
ifc-console extensions new acme-measure
cd ifc-agent-acme-measure
uv sync
uv run python -m pytest -q       # offline, no LLM key
uv run ifc-acme-measure path/to/project --model MODEL_ID
```

The generated project is complete: pyproject with a pinned core range, one
agent module (edit the tool selection, the instructions, and the company
tool), a terminal entry point with both run modes, and an offline test built
on `ifc_console.testing.ScriptedAgentModel`. Deliver it to a customer as a
private repository; they install it by git URL as above.

## The measurement agent

`ifc-agent-measure` is the reference extension (in `packages/` of the main
repository until it moves to its own). It measures what company documents say
to measure:

1. resolve the scope: names, selectors, or the viewer selection;
2. `get_measurement_recipe(class, property, type_name)` picks the company's
   method, most specific match first;
3. `measure_elements` executes it (stored quantities, material layer sums, or
   mesh geometry), returning file units and SI side by side;
4. deviations and low-confidence values are color-themed in the viewer;
5. the final answer is a typed `MeasurementReport`, exportable as CSV, with
   the method and the document page cited per value;
6. optionally, `--propose` lets the agent request a ChangeSet preview of the
   measured values; a human approves and commits, never the model.

```bash
uv tool install ifc-agent-measure
ifc-console knowledge ingest path/to/project/docs
ifc-measure path/to/project --model MODEL_ID --viewer
```

## Measurement recipes

Recipes make the method deterministic and auditable. YAML files under
`.ifc-console/recipes/`, one or many per file:

```yaml
applies_to: {class: IfcWall, type_name: "Basic Wall: Interior*"}
property: thickness
method: layer_sum
params: {exclude_layers: ["*Finish*", "*Render*"]}
unit: mm
tolerance: 2
source: {document: "QS-Manual.pdf", page: 12}
notes: structural layers only, per section 4.2
```

`get_measurement_recipe` resolves the most specific match (type beats class)
and returns the recipe, its citation, and ready-to-use `measure_elements`
arguments. No match is a clear miss that points the agent at
`search_ifc_knowledge(corpus="project")` so it can read the manual chunk and
choose a method itself, saying so in its report. Recipes are written by hand
or drafted from ingested documents and approved by a human; the model can
look them up but never write them.
