# Automation workflows

IFC-Console 0.1.4 can execute a versioned JSON or YAML workflow without opening a
model, starting a server, or involving an LLM. A workflow is a deterministic
directed acyclic graph of read-only validation and selector-query steps. The
CLI and Python SDK are clients of the same durable `WorkflowService`.

## Manifest version 1

```yaml
version: "1"
name: submission-gate

inputs:
  - id: models
    paths:
      - models/*.ifc

step_concurrency: 1
failure_policy: continue

steps:
  - id: validate
    output: validation-reports
    input_ids: [models]
    concurrency: 2
    failure_policy: continue
    operation:
      kind: validation
      version: "1"
      ids_paths: [requirements/submission.ids]
      express_rules: false
      max_issues: 200

  - id: external-walls
    output: wall-schedule
    input_ids: [models]
    needs: [validate]
    operation:
      kind: query
      version: "1"
      query: IfcWall, Pset_WallCommon.IsExternal=TRUE
      fields: [name, storey, type_name]
      order_by: storey
      output_format: csv
      limit: 100000
```

Paths and globs are relative to the manifest. Absolute paths and `..` are
rejected. Every pattern must match at least one file. Step IDs and effective
output names must be unique, dependencies must exist, and cycles are rejected
before any work is scheduled. Unknown keys and operation versions are also
rejected.

The optional `output` is a stable logical name used when CLI artifacts are
exported. It defaults to the step ID. `step_concurrency` bounds concurrently
running steps, while each step's `concurrency` bounds child jobs across its IFC
inputs. Both workflow-level and child-level `failure_policy` accept `continue`
or `fail_fast`.

## Plan and run

```bash
ifc-console workflows schema > workflow-v1.schema.json
ifc-console run workflow.yaml --plan --json
ifc-console run workflow.yaml --output-dir reports --json

ifc-console workflows list
ifc-console workflows show workflow-0123456789abcdef --json
ifc-console workflows watch workflow-0123456789abcdef
ifc-console workflows cancel workflow-0123456789abcdef
ifc-console workflows resume workflow-0123456789abcdef --output-dir reports
```

`workflows schema` prints the authoritative JSON Schema without starting a
model, server, or automation service. Point an editor or manifest generator at
that file for completion and validation. The command is safe to run in CI and
its output is versioned by the manifest's required `version` field.

Planning resolves inputs, checks policy and paths, hashes every IFC and IDS
source, validates the graph, reports the child count and required capabilities,
and creates a stable `plan_id`. It does not create a workflow, batch, job, or
artifact. The generation timestamp is informational and is not part of the
plan identity.

Running first verifies the captured identities and then persists the workflow
record before scheduling a step. Ready steps execute through the existing
bounded batch and restricted job services. Progress is printed on stderr so
`--json` stdout remains safe for automation.

Exit code 0 means every step executed successfully and every validation passed.
Exit code 5 means execution succeeded but validation findings failed the gate.
Exit code 1 means a workflow, batch, worker, source, or cancellation failure.

## Durability and artifacts

Records live under `IFC_CONSOLE_HOME/workflows/records`. Each step records its
captured batch specification, dependency list, attempts, batch ID, summary,
failure, and artifact. A terminal workflow creates one content-addressed JSON
manifest referencing every step's batch manifest. Those manifests reference
the validation JSON/SARIF reports or streamed query JSONL/CSV files, forming one
retention-safe artifact graph.

Cancellation is cooperative and also uses a durable local cancellation flag,
so another local process can request it. Resume re-hashes all IFC and IDS
sources, verifies artifacts and batch records for successful steps, reuses only
complete verified steps, and retries unfinished steps. A record owned by a dead
process is recovered as `interrupted` and can be resumed. Changed source bytes
always require a new plan and submission.

## Python SDK

```python
from ifc_console import Workbench

with Workbench.open(home=".ifc-console-ci") as wb:
    plan = wb.plan_workflow("workflow.yaml")
    print(plan.plan_id, plan.total_children)

    run = wb.submit_workflow_plan(plan)
    for update in wb.watch_workflow(run.workflow_id):
        print(update.progress, update.message)

    completed = wb.workflow(run.workflow_id)
    print(completed.summary, completed.aggregate_artifact)
```

Typed in-memory specifications can use `plan_workflow_spec(spec,
base_dir=...)`. Async applications use the same method names on
`AsyncWorkbench`. Both clients also expose `submit_workflow`, `workflows`,
`wait_workflow`, `cancel_workflow`, and `resume_workflow`.

All types needed to build a manifest are exported from the top-level package:

```python
from pathlib import Path

from ifc_console import (
    Workbench,
    WorkflowInputSpec,
    WorkflowQueryOperation,
    WorkflowSpec,
    WorkflowStepSpec,
)

spec = WorkflowSpec(
    name="wall-inventory",
    inputs=(WorkflowInputSpec(id="models", paths=("models/*.ifc",)),),
    steps=(
        WorkflowStepSpec(
            id="walls",
            operation=WorkflowQueryOperation(query="IfcWall"),
        ),
    ),
)

with Workbench.open(home=".ifc-console-ci") as wb:
    plan = wb.plan_workflow_spec(spec, base_dir=Path.cwd())
    completed = wb.wait_workflow(
        wb.submit_workflow_plan(plan).workflow_id,
    )
```

`examples/workflows/submission-gate.yaml` is a copyable project template. Its
paths are intentionally relative to the manifest, so copy it beside the
project's `models` and `requirements` folders before planning it.

## Security boundaries and current scope

- Version 1 supports only built-in validation and selector-query operations.
  It cannot execute generated Python, shell commands, plugins, network calls, or
  model mutations.
- YAML uses safe loading and manifests are limited to 2 MiB.
- At most 10,000 unique source files are accepted. Hashing is bounded and child
  processes keep their existing time, memory, environment, filesystem, network,
  and subprocess restrictions.
- Planning and execution enforce the same allowed-directory and capability
  policy used by the SDK and CLI.
- Manifests contain no secret interpolation or implicit environment reads.

Remote execution, MCP workflow tools, mutation workflows, output-to-input data
bindings, caching across separate workflow IDs, and plugin operations remain
future interfaces. They require scoped remote authorization and stable plugin
contracts rather than silently widening version 1.
