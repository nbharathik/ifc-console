# Python SDK

Everything the LLM can do, from a script. No server, no terminal, no port, no
token, and no MCP runtime: `Workbench` opens a model in this process and calls
the transport-neutral operation service. The MCP server and embedded agent use
that same operation registry as adapters.

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    print(wb.info()["project"]["name"])
    walls = wb.query("IfcWall, Pset_WallCommon.FireRating=F30")
    print(len(walls), "fire-rated walls")
```

The workbench closes its model and worker threads on exit, so use it as a
context manager (or call `close()` yourself).

## The mode switch still applies

`Workbench.open(..., mode="ask")` is the default and behaves exactly like the
console: reads run, mutations are refused.

```python
wb = Workbench.open("tower.ifc", mode="edit")   # you are the human in the loop
wb.run_code("ifc_api.attribute.edit_attributes(ifc, product=..., attributes=...)",
            "rename the walls")
wb.save()
```

In the console the user owns that switch and the model cannot touch it. In a
script you are the user, so `set_mode()` is yours. If you build an agent, keep
`set_mode` in your own code and never expose it as a tool.

## Reads

| method | returns |
| ------ | ------- |
| `info()` | project summary: schema, units, counts, materials |
| `orient()` | status, summary, and the spatial tree in one call |
| `tree(root=None, depth=10)` | the spatial containment tree |
| `query(selector, limit=50, fields=None)` | element rows for a selector |
| `element(global_ids, include=[...])` | full detail per element |
| `psets(global_ids)` | property sets and quantity sets |
| `quantities(selector, by="storey")` | quantity takeoff, aggregated |
| `validate()` / `validate_ids(path)` | schema issues / IDS report |
| `clashes(set_a, set_b, tolerance=0.01)` | geometric clashes |
| `georeferencing()` | CRS, map conversion, true north |
| `schema_docs(entity=..., pset=..., property=...)` | IFC documentation |

Errors raise `IfcConsoleError`, which carries the same `code`, `message`, and
`hint` the LLM would see:

```python
from ifc_console import IfcConsoleError

try:
    wb.query("IfcWall, ((broken")
except IfcConsoleError as exc:
    print(exc.code, exc.hint)   # INVALID_QUERY, and what to do about it
```

## More than one model

```python
wb.attach("structural.ifc")
wb.attach("mep.ifc")
print(wb.models()["models"])
hits = wb.clashes("IfcWall", "IfcDuctSegment", other_model="mep", tolerance=0.02)
```

One model stays active and writable; attached models are read only, exactly as
in the console.

## The knowledge index

```python
wb.build_knowledge()                       # once, a few seconds, no network
wb.search_knowledge("which pset carries fire rating")
wb.api_docs("pset.add_pset")["meta"]["signature"]
```

## Agent bindings, provider neutral

`tools()` hands out plain JSON Schema definitions and `call()` runs one by
name. No LLM vendor is involved, and nothing here depends on a particular API
client, so the same two calls drive any provider's tool-use loop.

```python
tools = wb.tools()
# [{"name": "query_elements", "input_schema": {...}, "data_schema": {...}}, ...]

result = wb.call("query_elements", query="IfcDoor", limit=10)
# {"ok": True, "data": {...}, "meta": {...}}
```

A minimal loop looks like this, whatever the provider:

```python
def run_tool_call(name, arguments):
    envelope = wb.call(name, **arguments)
    return envelope          # errors included; the hint is written for a model
```

Feed `tools()` to your client as its tool list, route every tool call through
`run_tool_call`, and hand the envelope back as the tool result. Failures come
back as data (`ok: False` plus a hint), which is what lets a model correct
itself instead of stopping.

Two things worth keeping out of the model's reach: `set_mode` (the human owns
it) and any code path that changes allowed directories.

## Typed operation contracts

The v2 SDK contracts are being introduced additively. Existing dictionary
methods remain available. Code that wants validation and editor type support
can use the typed definitions, envelopes, and reference result models:

```python
from ifc_console import QueryElementsData, ValidationData, Workbench

with Workbench.open("tower.ifc") as wb:
    definitions = wb.operation_definitions()
    query: QueryElementsData = wb.query_result("IfcWall")
    report: ValidationData = wb.validation_result()

    print(wb.context.workspace_id)
    print(wb.context.active_model.model_id)
    print(wb.context.active_model.revision_id)
    print(wb.context.active_model.content_sha256)
```

`wb.context` is immutable and reports the active model revision. The full
`content_sha256` is stable for identical clean source bytes, while the revision
identifier also changes after an in-memory mutation, reload, or save. Callers
should read it again instead of caching it across writes. Operation-specific data
models currently cover status, element query, and schema validation; more
operations will gain them during the remaining v2 core extraction.

Use `call_result(name, **arguments)` when you want the typed `Envelope` rather
than the dictionary returned by `call()`. `operation_definitions()` returns
typed `OperationDefinition` values, while `tools()` remains the provider-neutral
dictionary form intended for LLM client libraries.

## Validation jobs and artifacts

Long validation can run in an isolated worker instead of blocking the model
session. The job is bound to the current revision and verified source hashes.
It produces content-addressed JSON and SARIF artifacts:

```python
with Workbench.open("tower.ifc") as wb:
    revision = wb.context.active_model.revision_id
    job = wb.submit_validation_job(
        ids_paths=("requirements.ids",),
        expected_revision=revision,
    )

    for update in wb.watch_job(job.job_id):
        print(update.progress, update.message)

    completed = wb.job(job.job_id)
    for artifact in completed.artifacts:
        print(artifact.artifact_id, artifact.media_type)
        wb.export_artifact(artifact.artifact_id, artifact.name)
```

Artifact ingestion and export are bounded-memory streams. Callers can pin
important outputs and plan reference-aware cleanup:

```python
ref = completed.artifacts[0]
wb.pin_artifact(ref.artifact_id)

plan = wb.plan_artifact_gc(older_than_days=30)
print(plan.candidate_ids, plan.candidate_bytes)

# Collection refuses a changed plan and requires explicit confirmation.
result = wb.collect_artifacts(plan, confirm=True)
```

Retained job outputs, recent artifacts, explicit pins, and complete transaction
history are protected. Approval and collection are caller-owned lifecycle
actions and are not exposed as general AI tools.

Jobs survive process restarts as terminal records. A previously running job
whose supervisor disappeared is recovered as failed, never as successful.
Cancellation terminates the worker, and another local CLI or SDK process can
place a cancellation request for the owning process to observe.

Validation jobs currently require a clean model because their worker reads
verified bytes from disk. Save a dirty model first. Dirty in-memory snapshot
jobs will be added with the ChangeSet transaction work.

The worker receives a minimal environment without credentials. Network,
subprocess, and filesystem capabilities are restricted with the same process
and audit-hook controls used by the generated-code sandbox.

## Safe structured property changes

Structured edits use a preview, approval, commit, and optional restore flow.
The preview runs against an isolated copy and stores a revision-bound
ChangeSet artifact. It does not mutate the live model or source file.

```python
with Workbench.open("tower.ifc") as wb:
    wall = wb.query("IfcWall, Pset_WallCommon.FireRating=F30")[0]
    preview = wb.preview_property_change(
        wall["global_id"],
        pset_name="Pset_WallCommon",
        property_name="FireRating",
        value="F60",
        expected_revision=wb.context.active_model.revision_id,
    )

    for change in preview.change_set.changes:
        print(change.before, "->", change.after)

    approval = wb.approve_change_set(
        preview.change_set_id,
        approved_by="bim-manager@example.com",
        reason="Reviewed against the fire strategy",
    )
    wb.set_mode("edit")
    commit = wb.commit_change_set(
        preview.change_set_id,
        approval_id=approval.approval_id,
    )
    print(commit.commit_id, commit.result.committed_sha256)

    # Explicit confirmation protects restore in the same way approval protects commit.
    restored = wb.restore_commit(commit.commit_id, confirm=True)
```

Commit rechecks the revision and source checksum, applies the ChangeSet in a
restricted worker, writes and reopens a candidate IFC, runs schema validation,
creates a checksum-verified backup artifact, and replaces the target under a
cross-process lock. A failed post-replacement check automatically attempts to
restore the original bytes. Restore refuses if the committed target changed in
the meantime. Candidate, backup, replacement, and rollback paths stream their
bytes, keeping supervisor memory bounded for large IFC files.

`preview_property_change` and `get_change_set` are available in the generic
operation and MCP tool surface. `approve_change_set`, `commit_change_set`, and
`restore_commit` are deliberately direct SDK methods only. Do not add them to
an agent's tool list.

The first editor intentionally supports existing occurrence-level
`IfcPropertySingleValue` properties only. It refuses missing property sets,
inherited type properties, ambiguous duplicates, dirty models, stale
revisions, and properties whose nominal IFC type cannot be preserved.

## Async

`AsyncWorkbench` is the same surface as coroutines, for code that already runs
an event loop:

```python
from ifc_console import AsyncWorkbench

wb = await AsyncWorkbench.create("tower.ifc")
walls = await wb.query("IfcWall")
job = await wb.submit_validation_job()
async for update in wb.watch_job(job.job_id):
    print(update.progress)
preview = await wb.preview_property_change(
    walls[0]["global_id"],
    pset_name="Pset_WallCommon",
    property_name="FireRating",
    value="F60",
)
wb.close()
```

`Workbench` is a thin synchronous wrapper around it, running its own event loop
on a private thread.

## Options

```python
Workbench.open(
    "tower.ifc",
    mode="ask",                       # or "edit"
    home="/tmp/ifc-home",             # where settings, audit, and the index live
    allowed_dirs=("/data/models",),   # extra readable roots
    settings={"knowledge.enabled": False},   # in-memory overrides
)
```

`settings` accepts any dotted key from [Settings](settings.md) and never writes
to the user's settings file.
