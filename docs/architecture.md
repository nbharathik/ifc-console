# Architecture

All interfaces share one operation core:

```text
SDK       Agent       CLI/TUI       MCP       Browser
 |          |            |           |           |
 +----------+------------+-----------+-----------+
                         |
            OperationService + Registry
              schemas, policy, envelopes
                         |
                      AppCore
         model sessions, settings, audit, viewer state
              |                       |
       model worker threads     jobs and transactions
                                      |
                              restricted workers
```

`ifc-console` contains this complete Python runtime, including MCP, SDK,
agents, chat, plugins, and workflows. `ifc-console-viewer` contains static
browser assets only and is installed through `ifc-console[viewer]`.

## Operation contract

Built-in operations register once. Every interface receives the same schema,
capability requirements, policy decision, `{ok, data/error, meta}` envelope,
and correlation ID. Adapters do not reimplement IFC behavior.

`ask` and `edit` are capability profiles. Mode changes, ChangeSet approval,
commit, restore, and allowed paths remain host-controlled actions.

## Model access

IfcOpenShell file objects are not thread-safe. Each resident model has one
worker thread, and all access is serialized through it. One model is active
and writable; attached models are read-only.

```text
model thread          short reads and approved in-memory edits
restricted process   read-only code, validation, and queries
transaction process  preview, commit, restore, and verification
```

Workers receive bounded filesystem, network, subprocess, time, and memory
capabilities. The generated-code sandbox uses a verified on-disk copy, so it
is limited to clean read-only work.

## Durable work

```text
job -> batch -> workflow
  \------ content-addressed artifacts ------/
```

Jobs run one validation or transaction. Batches capture inputs for repeated
validation or queries. Workflows connect batches through a versioned dependency
graph. Source hashes and artifact checksums prevent unsafe resume.

Structured writes follow one path:

```text
preview -> ChangeSet -> host approval -> commit -> receipt and backup
```

Commit rechecks the revision and source hash, validates a reopened candidate,
and replaces the target under a cross-process lock. Durable journals support
recovery and checksum-guarded restore.

## Audit and artifacts

Audit JSONL uses sequence numbers and a SHA-256 hash chain. Generated source,
secrets, and sensitive values are redacted. An external append-only sink is
still required for enterprise retention.

Artifact cleanup preserves pinned outputs and references held by retained jobs,
recent activity, and transaction history.

## Browser boundary

The optional wheel ships plain browser modules, Three.js, web-ifc, and WASM.
It has no Python application logic, CDN dependency, or Node runtime. The core
package owns the HTTP routes, authentication, selection bridge, chat runtime,
and seven stable viewer operations. Without the assets or a connected tab,
those operations return an actionable availability state.

## Runtime

Textual and Uvicorn share one asyncio loop. Operation handlers send IFC access
to model threads; long validation, queries, code, and transactions use
supervised subprocesses. Results return to the loop and update the console and
browser clients.
