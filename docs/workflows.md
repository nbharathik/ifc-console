# Automation workflows

Use a workflow for repeatable, read-only checks across one or more IFC files.
No console, server, or LLM is required.

```text
manifest -> plan -> reviewed plan -> run -> artifact manifest
              no work             jobs and batches
```

Version 1 supports schema/IDS validation and selector queries.

## Example manifest

```yaml
version: "1"
name: submission-gate

inputs:
  - id: models
    paths: [models/*.ifc]

failure_policy: continue

steps:
  - id: validate
    input_ids: [models]
    concurrency: 2
    operation:
      kind: validation
      version: "1"
      ids_paths: [requirements/submission.ids]

  - id: external-walls
    input_ids: [models]
    needs: [validate]
    operation:
      kind: query
      version: "1"
      query: IfcWall, Pset_WallCommon.IsExternal=TRUE
      fields: [name, storey, type_name]
      output_format: csv
```

Paths and globs are relative to the manifest. Absolute paths and `..` are
rejected. Every pattern must match at least one file. Step IDs, output names,
and dependencies must be valid and unique; cycles and unknown keys fail before
work starts.

`step_concurrency` limits running steps. A step's `concurrency` limits its child
jobs. `failure_policy` accepts `continue` or `fail_fast`.

## Plan and run

```bash
ifc-console workflows schema > workflow-v1.schema.json
ifc-console run workflow.yaml --plan --json
ifc-console run workflow.yaml --output-dir reports
```

Planning resolves paths, hashes every IFC and IDS source, checks policy, and
produces a stable `plan_id`. It creates no jobs or artifacts.

Running verifies the captured inputs again, stores the workflow record, then
schedules ready steps. Exit codes:

| code | meaning |
| ---- | ------- |
| `0` | every step ran and validation passed |
| `5` | work ran, but validation findings failed the gate |
| `1` | source, worker, batch, workflow, or cancellation failure |

## Manage runs

```bash
ifc-console workflows list
ifc-console workflows show workflow-0123456789abcdef
ifc-console workflows watch workflow-0123456789abcdef
ifc-console workflows cancel workflow-0123456789abcdef
ifc-console workflows resume workflow-0123456789abcdef
```

Records store steps, dependencies, attempts, batches, failures, and artifacts.
Resume re-hashes inputs, verifies completed artifacts, reuses only valid work,
and retries unfinished steps. Changed source bytes require a new plan.

The final content-addressed manifest references every step and child report, so
artifact cleanup retains the complete result graph.

## Python

```python
from ifc_console import Workbench

with Workbench.open(home=".ifc-console-ci") as wb:
    plan = wb.plan_workflow("workflow.yaml")
    run = wb.submit_workflow_plan(plan)

    for update in wb.watch_workflow(run.workflow_id):
        print(update.progress, update.message)

    completed = wb.workflow(run.workflow_id)
```

`submit_workflow()` plans and submits in one call. The SDK also provides list,
wait, cancel, and resume methods. `AsyncWorkbench` exposes async equivalents.

`examples/workflows/submission-gate.yaml` is a copyable template.

## Security boundary

Version 1 cannot run generated Python, shell commands, plugins, network calls,
or model mutations. YAML uses safe loading; manifests are size limited; paths
stay inside allowed roots; and source counts, hashing, worker time, memory,
filesystem, network, and subprocess access are bounded.

Manifests do not interpolate environment variables or secrets.
