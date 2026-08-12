# Python SDK

Use the SDK for scripts, notebooks, CI, and applications that should work with
IFC models without MCP or a running console.

| interface | use it for |
| --------- | ---------- |
| `Workbench` | synchronous scripts and CI |
| `AsyncWorkbench` | the same API inside async applications |
| `LocalRuntime` | embedded agent tools over a local model |
| `ConsoleRuntime` | agent tools connected to a running console |
| `Agent` | a bounded provider-neutral tool loop |

Start with `Workbench` unless you need an agent runtime.

## Quick start

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    print(wb.info()["project"]["name"])
    walls = wb.query("IfcWall")
    report = wb.validate()
    print(len(walls), report["valid"])
```

The context manager closes model and worker resources automatically.

## Common operations

| method | result |
| ------ | ------ |
| `orient()` | status, project summary, and shallow spatial tree |
| `info()` | schema, units, counts, and materials |
| `tree(depth=10)` | spatial containment tree |
| `query(selector, limit=50)` | selector result rows |
| `element(global_ids)` | attributes, properties, type, and container |
| `psets(global_ids)` | property and quantity sets |
| `quantities(selector, by="storey")` | aggregated stored quantities |
| `validate()` / `validate_ids(path)` | schema or IDS results |
| `clashes(set_a, set_b)` | overlap or clearance results |
| `georeferencing()` | CRS and map conversion |
| `schema_docs(...)` | IFC entity, pset, or property documentation |

Search the offline reference with `search_knowledge()`,
`knowledge_record()`, and `api_docs()`.

## Ask and edit modes

`Workbench.open()` starts in `ask` mode. Reads work; mutations do not.

```python
with Workbench.open("tower.ifc", mode="edit") as wb:
    wb.run_code(
        'project = ifc.by_type("IfcProject")[0]\n'
        'ifc_api.attribute.edit_attributes('
        'ifc, product=project, attributes={"Name": "Tower"})',
        "rename the project",
    )
    wb.save()
```

In a script, your code owns `set_mode()`. In an agent application, never expose
mode changes, allowed directories, or approval methods as model-callable tools.

## Errors

Convenience methods raise `IfcConsoleError` with the same code and hint an MCP
client receives:

```python
from ifc_console import IfcConsoleError

try:
    wb.query("IfcWall, ((broken")
except IfcConsoleError as exc:
    print(exc.code, exc.hint)
```

Low-level `call()` returns the normal `{ok, data/error, meta}` envelope instead.

## More than one model

```python
with Workbench.open("architecture.ifc") as wb:
    wb.attach("structure.ifc")
    wb.attach("mep.ifc")
    hits = wb.clashes(
        "IfcWall",
        "IfcDuctSegment",
        other_model="mep",
        tolerance=0.02,
    )
```

One model is active and writable. Attached models are read-only.

## Tool bindings

`tools()` returns provider-neutral JSON Schema definitions. `call()` runs one
operation by name.

```python
tools = wb.tools(permitted_only=True)
result = wb.call("query_elements", query="IfcDoor", limit=10)
```

Use `permitted_only=True` so an AI sees only operations allowed by the current
profile. Definitions also include required capabilities and current permission
state.

For typed results, use `query_result()`, `validation_result()`,
`call_result()`, and `operation_definitions()`. `wb.context` provides immutable
workspace, model, revision, and source-hash information. Read it again after a
write.

The package includes a `py.typed` marker. Public contracts are exported from
`ifc_console` for IDEs, Pyright, and mypy.

## Durable automation

Long work can run in restricted workers and produce checksum-verified artifacts:

```python
with Workbench.open("tower.ifc") as wb:
    job = wb.submit_validation_job(ids_paths=("requirements.ids",))
    completed = wb.wait_job(job.job_id)
    for artifact in completed.artifacts:
        wb.export_artifact(artifact.artifact_id, artifact.name)
```

The SDK also supports:

- validation and query batches across many IFC files;
- versioned JSON or YAML workflows;
- watch, wait, cancel, and safe resume;
- artifact pinning and reference-aware cleanup.

Resume verifies source hashes and previous artifacts before reusing work.
Validation jobs require a clean model because workers read the file from disk.
See [Automation workflows](workflows.md).

## Structured changes

Property and classification edits use a preview, approval, commit, and optional
restore flow:

```python
with Workbench.open("tower.ifc") as wb:
    wall = wb.query("IfcWall")[0]
    preview = wb.preview_property_change(
        wall["global_id"],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value="F60",
    )
    approval = wb.approve_change_set(
        preview.change_set_id,
        approved_by="bim-manager",
    )
    wb.set_mode("edit")
    commit = wb.commit_change_set(
        preview.change_set_id,
        approval_id=approval.approval_id,
    )
```

Preview does not change the model. Commit rechecks the revision, validates a
candidate, and creates a verified backup. Approval, commit, and restore are
direct caller methods and must not be added to an AI tool list.

## Async

`AsyncWorkbench` uses the same method names as coroutines:

```python
from ifc_console import AsyncWorkbench

async with await AsyncWorkbench.create("tower.ifc") as wb:
    walls = await wb.query("IfcWall")
    report = await wb.validate()
```

Use `async with` or `await wb.aclose()` so supervised cleanup finishes before
the model closes.

## Agent applications

Use `LocalRuntime` or `ConsoleRuntime` when a model should choose tools. They
provide scoped toolsets, custom Python and MCP sources, limits, middleware,
threads, and host-owned approvals. See [Building agent applications](agents.md).

## Options

```python
Workbench.open(
    "tower.ifc",
    mode="ask",
    home=".ifc-console-ci",
    allowed_dirs=("models", "requirements"),
    settings={"sandbox.mode": "strict"},
)
```

Settings passed here apply only to this workbench and do not edit the user file.
