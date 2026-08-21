# ifc-agent-measure

A document-grounded measurement agent for IFC models, built on
[ifc-console](https://github.com/nbharathik/ifc-console). It resolves scopes
like "all walls of the same type as this one", looks up the company's
measurement recipe, measures with explicit methods (stored quantities,
material layer sums, mesh geometry), shows the result in the 3D viewer, and
cites the document and page every method came from.

The generic capability lives in ifc-console core; this package is the thin
product on top: instructions, the scoped toolset, the report shape, and one
optional proposal tool whose approval and commit stay with the host.

## Install

End users install it as an isolated tool (recommended):

```bash
uv tool install ifc-agent-measure
uv tool install "ifc-agent-measure[viewer,pdf]"   # 3D viewer + PDF manuals
```

Developers clone this folder and run it in place:

```bash
uv sync
uv run ifc-measure path/to/project --model MODEL_ID
```

## Run

Standalone, on a project folder (the folder convention: models beside the
company documents, recipes under `.ifc-console/recipes/`):

```bash
ifc-measure path/to/project --model MODEL_ID
ifc-measure path/to/project --model MODEL_ID --viewer
ifc-measure tower.ifc --model MODEL_ID --provider anthropic
```

Attached to a running IFC Console, which keeps its mode switch and approvals:

```bash
ifc-measure --attach http://127.0.0.1:8383/mcp --token TOKEN --model MODEL_ID
```

One-shot with a CSV report:

```bash
ifc-measure path/to/project --model MODEL_ID \
  --prompt "measure the thickness of all interior walls" --report thickness.csv
```

`--propose` adds one narrow write path: the agent may request a ChangeSet
preview of measured values into `Company_Measurements.MeasuredThickness`; a
human approves and commits it, never the model.

## Feed it the company's conventions

```bash
ifc-console knowledge ingest path/to/project/docs
```

indexes the project's manuals (markdown, text, PDF; images are referenced).
Measurement recipes make the method deterministic; put YAML files under
`path/to/project/.ifc-console/recipes/`:

```yaml
applies_to: {class: IfcWall, type_name: "Basic Wall: Interior*"}
property: thickness
method: layer_sum
params: {exclude_layers: ["*Finish*", "*Render*"]}
unit: mm
tolerance: 2
source: {document: "QS-Manual.pdf", page: 12}
```

## Test without an LLM key

The tests drive the agent with `ifc_console.testing.ScriptedAgentModel`
against a generated model, offline:

```bash
uv run --package ifc-agent-measure python -m pytest packages/ifc-agent-measure/tests -q
```
