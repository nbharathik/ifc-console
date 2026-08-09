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
# [{"name": "query_elements", "required_capabilities": ["model:read"], ...}, ...]

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

Each definition includes `required_capabilities` and a `permitted` value for
the current compatibility profile. Applications can inspect policy without
executing an operation:

```python
from ifc_console import Capability

print(wb.granted_capabilities())
decision = wb.capability_decision([Capability.MODEL_COMMIT])
print(decision.allowed, decision.missing, decision.rule)
```

Tool authority and direct caller authority are intentionally different. For
example, `model:approve` is available to a direct SDK/CLI caller but never to
an AI-visible tool profile. The returned operation envelope contains a
`meta.correlation_id`; durable records and artifacts retain that identity.

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

The current audit chain can be verified from embedded code with
`wb.verify_audit()`. A valid result proves that the local JSONL records have
not been reordered or modified since they were written. External append-only
storage is still required for a strong enterprise retention boundary.

Retained job outputs, recent artifacts, explicit pins, and complete transaction
history are protected. Approval and collection are caller-owned lifecycle
actions and are not exposed as general AI tools.

Jobs survive process restarts as terminal records. A previously running
validation job whose supervisor disappeared is recovered as failed. Commit and
restore jobs consult their transaction journal: a verified durable receipt is
recovered as success, a verified rollback becomes an interrupted failure, and
an ambiguous target becomes `TRANSACTION_RECOVERY_REQUIRED`. Cancellation
terminates a worker before the commit point. It is rejected after the commit
point while receipt persistence or rollback completes.

Validation jobs currently require a clean model because their worker reads
verified bytes from disk. Save a dirty model first. Dirty in-memory snapshot
jobs remain future work.

### Resumable read-only batches

The SDK can run validation or streaming queries without opening a model. Both
async and sync faces expose submit, inspect, list, watch, wait, cancel, and
resume methods:

```python
with Workbench.open(home=".ifc-console-ci") as wb:
    batch = wb.submit_validation_batch(
        ["architecture.ifc", "structure.ifc", "mep.ifc"],
        ids_paths=("submission.ids",),
        concurrency=2,
        failure_policy="continue",
    )

    for update in wb.watch_batch(batch.batch_id):
        print(update.progress, update.message)

    completed = wb.batch(batch.batch_id)
    print(completed.summary)
    print(completed.aggregate_artifact.artifact_id)

    query = wb.submit_query_batch(
        ["architecture.ifc", "structure.ifc", "mep.ifc"],
        query="IfcWall, Pset_WallCommon.IsExternal=TRUE",
        output_format="jsonl",
    )
    query_result = wb.wait_batch(query.batch_id)
```

`resume_batch()` never silently adopts current file contents. It requires every
captured IFC and IDS identity to match, checksum-verifies previous artifacts,
and only then schedules unfinished children. Use a new submission when any
source intentionally changes. Validation and streaming query batches are
currently local and read-only; mutation batches and a generic remote batch
endpoint are not yet part of the public contract.

The worker receives a minimal environment without credentials. Network,
subprocess, and filesystem capabilities are restricted with the same process
and audit-hook controls used by the generated-code sandbox.

### Versioned automation workflows

A typed workflow composes validation and query batches into a deterministic,
durable DAG. Planning hashes and validates every input but schedules nothing:

```python
with Workbench.open(home=".ifc-console-ci") as wb:
    plan = wb.plan_workflow("workflow.yaml")
    run = wb.submit_workflow_plan(plan)

    for update in wb.watch_workflow(run.workflow_id):
        print(update.progress, update.message)

    completed = wb.workflow(run.workflow_id)
    print(completed.summary)
```

Use `submit_workflow(path)` to plan and submit in one call. `workflows`,
`wait_workflow`, `cancel_workflow`, and `resume_workflow` cover the durable
lifecycle. `plan_workflow_spec(spec, base_dir=...)` accepts the exported typed
`WorkflowSpec` contract for applications that generate manifests. The same
methods are asynchronous on `AsyncWorkbench`.

Resume rejects changed source identities and only reuses a complete step after
its batch record and content-addressed artifact verify. The final workflow
manifest references every batch manifest and child report. Version 1 is local,
read-only, and limited to validation and selector queries. See
[Automation workflows](workflows.md) for the complete schema.

## Safe structured property and classification changes

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

Creation is opt-in and remains a preview until approved:

```python
property_preview = wb.preview_property_change(
    wall["global_id"],
    pset_name="Company_QA",
    property_name="ReviewStatus",
    value="Checked",
    create_missing=True,
    nominal_type="IfcLabel",
)
classification_preview = wb.preview_classification_assignment(
    wall["global_id"],
    classification_name="Company Classification",
    identification="WALL-EXT",
    reference_name="External wall",
)
```

The property nominal type is inferred for ordinary non-null scalars when
`nominal_type` is omitted. An explicit IFC value or measure type is validated
inside the isolated worker. Classification previews reuse exact systems and
references, deduplicate the relationship, and create direct occurrence
assignments. Both return the same `ChangeSetRecord` and use the approval,
commit, job, journal, audit, and restore APIs shown above.

The convenience methods above submit and wait for durable jobs. Applications
that need non-blocking orchestration can use the job-first surface directly:

```python
job = wb.submit_commit_job(preview.change_set_id, approval_id=approval.approval_id)
for update in wb.watch_job(job.job_id):
    print(update.phase, update.progress, update.cancellable)

completed = wb.job(job.job_id)
journal = wb.transaction_journal(completed.transaction_id)
print(journal.phase, journal.receipt_artifact_id)
```

Commit rechecks the revision and source checksum, applies the ChangeSet in a
restricted worker, writes and reopens a candidate IFC, runs schema validation,
rejects findings not present in the source, creates a checksum-verified backup
artifact, and replaces the target under a
cross-process lock. A failed post-replacement check automatically attempts to
restore the original bytes. Restore refuses if the committed target changed in
the meantime. Candidate, backup, replacement, and rollback paths stream their
bytes, keeping supervisor memory bounded for large IFC files.

Replacement is guarded by an fsynced state machine with candidate, backup,
commit-point, target, receipt, and rollback phases. Recovery compares target
and artifact hashes before another write is allowed. A corrupt journal or a
target matching neither known revision blocks writes for manual inspection.

`preview_property_change`, `preview_classification_assignment`, and
`get_change_set` are available in the generic operation and MCP tool surface.
`approve_change_set`, `commit_change_set`, and
`restore_commit` are deliberately direct SDK methods only. Do not add them to
an agent's tool list.

The editor supports existing and explicitly created occurrence-level
`IfcPropertySingleValue` properties plus direct classification assignments. It
refuses inherited type-property edits, ambiguous duplicates, null property
creation, dirty models, stale revisions, and values whose nominal IFC type
cannot be preserved.

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
await wb.aclose()
```

`Workbench` is a thin synchronous wrapper around it, running its own event loop
on a private thread. Its `close()` waits for supervised job cleanup. Async code
should use `await wb.aclose()` or `async with` so transaction rollback completes
before model sessions close.

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
