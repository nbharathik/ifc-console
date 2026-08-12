# Architecture

Several interfaces share one operation core:

```text
 SDK       Agent       CLI/TUI       MCP client       Browser
  |          |            |              |               |
  +----------+------------+--------------+---------------+
                              |
             OperationService + OperationRegistry
             schemas, capabilities, envelopes, audit IDs
                              |
                         AppCore
          policy, models, settings, backups, viewer state
                    |                         |
          model worker threads         durable services
                                      jobs, batches,
                                   workflows, transactions
                                                |
                                      restricted workers
```

Built-in operations register once. The SDK and agents call them directly; MCP,
the CLI, and browser tools are adapters over the same definitions and policy.

## Main decisions

### One operation contract

Every operation declares its schema and required capabilities. `ask` and `edit`
are permission profiles over those capabilities. Results use the same
`{ok, data/error, meta}` envelope on every interface.

One correlation ID follows a call into jobs, workers, artifacts, transactions,
and audit events.

### Serialized model access

IfcOpenShell file objects are not thread-safe. Each resident model therefore
has one worker thread, and every read or write is serialized through it.

Exactly one model is active and writable. Attached models remain read-only and
may be evicted when clean and over the workspace memory budget.

### Separate workers for long or risky work

```text
live model thread        short reads and approved in-memory edits
restricted process      read-only generated code, validation, queries
transaction process     preview, commit, restore, and verification
```

Restricted workers receive minimal environments and bounded filesystem,
network, subprocess, time, and memory capabilities. Generated-code sandboxing
uses a verified disk copy, so it is available only for clean read-only work.

### Durable automation

```text
job -> batch -> workflow
  \------ content-addressed artifacts ------/
```

- A **job** runs one validation or transaction.
- A **batch** applies validation or a selector query to many captured inputs.
- A **workflow** connects batches through a versioned dependency graph.

Input hashes prevent resume from silently adopting changed files. Records are
written before work starts, and successful outputs enter artifact storage only
after checksum verification.

### Previewed writes

```text
preview -> ChangeSet -> caller approval -> commit -> receipt
                                           \-> backup
```

Structured changes run against an isolated copy first. Commit rechecks the
model revision and source hash, validates a reopened candidate, creates a
backup, and replaces the target under a cross-process lock. A durable journal
supports recovery and checksum-guarded restore.

Approval, commit, restore, and mode changes are not AI-callable operations.

### Audit and artifact lifecycle

Audit JSONL uses sequence numbers and a SHA-256 hash chain. Generated source,
secrets, and sensitive values are redacted. Verification detects local edits,
but an external append-only sink is still needed for enterprise retention.

Artifacts record references to other artifacts. Recent outputs, retained jobs,
explicit pins, and transaction history are protected from garbage collection.

### Optional viewer

The viewer is a separate package containing plain browser modules, Three.js,
and web-ifc WASM. It has no CDN or Node runtime. Its HTTP routes and four MCP
tools exist only while the viewer is enabled.

## Runtime model

Textual and uvicorn share one asyncio event loop. Operation handlers run on that
loop and send IFC access to the owning model thread. Long validation, queries,
generated code, and transactions use supervised subprocesses. State changes
return to the loop and update the console and viewer.
