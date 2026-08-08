# Architecture

One process, one active model, several faces over one core.

```text
 SDK      chat agent      CLI/TUI      MCP client      browser viewer
  |           |              |             |                 |
  |           |              |       FastMCP adapter     HTTP/WS routes
  +-----------+--------------+-------------+-----------------+
                              |
                 OperationService + OperationRegistry
                 typed definitions, validation, envelopes
                              |
                 AppCore, current owner of runtime state
                 PolicyEngine, ModelRegistry, AuditLog,
                 SettingsStore, BackupStore, ViewerHub
                     |                       |
        one thread per resident model    JobService + TransactionService
                                             |
                              restricted validation and edit workers
                              durable jobs, ChangeSets, and artifacts
```

## Key decisions

- **Operations are transport-neutral.** Built-in operations register once with
  `OperationRegistry`. The SDK and agent call `OperationService` directly;
  FastMCP projects the same definitions and invocation service into MCP tools.
  Viewer operations join or leave both surfaces together at runtime.
- **AppCore still owns runtime state.** The operation service, console, CLI,
  viewer, and MCP adapter currently converge on one object. Splitting its model,
  workspace, policy, and runtime responsibilities is part of the remaining v2
  extraction.
- **One worker thread per model.** IfcOpenShell file objects are not
  thread-safe, so every read and write on a model is serialized through its
  session's single executor thread with async timeouts. A timed-out job leaves
  that worker occupied ("poisoned"); recovery swaps in a fresh worker and
  reloads from disk.
- **One active model; attached models are read-only.** A ModelRegistry holds
  up to `workspace.max_resident` sessions inside a memory budget, evicting
  clean ones LRU. Exactly one is writable; `core.session` always means it.
- **One event loop for everything else.** uvicorn (MCP + viewer HTTP/WS) is
  co-hosted on the Textual event loop, so there is no cross-thread UI traffic. A
  small synchronous EventBus fans state changes out to the console and viewer
  hub.
- **Errors as data.** Tools never raise through the protocol. Every failure is
  an `{ok:false, error:{code, message, hint}}` envelope so the LLM can read the
  hint and self-correct.
- **Content-backed revisions.** Every clean load records the full source SHA-256
  and uses its first 12 characters as the short fingerprint. Every load,
  mutation, and save also bumps `ModelSession.revision`. Viewer ETags combine
  model ID, fingerprint, and revision so identical attached files keep distinct
  cache entries while sharing the same content identity.
- **The sandbox is a process, and the policy is an audit hook.** Namespace
  guards can be escaped; CPython audit hooks cannot be removed and fire inside
  the C implementation of each dangerous call, so that is where the boundary
  lives. The worker holds a read-only copy read from disk, which is why only
  non-mutating runs can use it, and why a mutation that slips past the
  classifier lands on a copy nobody keeps.
- **Long validation is a durable job.** The supervisor starts a restricted
  subprocess with a minimal environment, source hashes, revision context,
  cancellation, timeout, process limits, and audit-hook capabilities. JSON and
  SARIF results enter content-addressed artifact storage only after the worker
  verifies that its inputs did not change.
- **Structured writes are previewed transactions.** Property previews run on
  an isolated copy and produce immutable ChangeSet artifacts. Approval is a
  separate caller-only artifact. Commit checks the workspace revision and
  source checksum, verifies a reopened candidate, records a content-addressed
  backup, and atomically replaces the target under a cross-process lock.
  Restore is checksum guarded and produces its own receipt and safety backup.
  IFC candidates and backups are copied and verified as streams, so the
  supervisor does not buffer whole model files. Approval, commit, and restore
  are not AI-callable operations.
- **Artifacts have an explicit lifecycle.** Artifact metadata records references
  to other artifacts. Recent outputs, retained job outputs, caller pins, and all
  transaction records are garbage-collection roots. Collection is dry-run by
  default and requires an unchanged plan plus explicit caller confirmation.
- **Build-free viewer.** The SPA is plain ES modules plus vendored three.js and
  web-ifc WASM, served from package data. No Node in the build, no CDN at
  runtime.
- **The viewer is a bolt-on, not a pillar.** Its code lives in one subpackage
  (`viewer/`), its HTTP routes answer 404 while disabled, and its four MCP
  tools form an optional category. Nothing in the core query/edit path imports
  viewer code, and its 8 MB asset bundle ships as a separate
  distribution (`ifc-console[viewer]`); without it the 35 core tools still work.

## Threading model in one paragraph

The Textual app and uvicorn share the asyncio loop. Operation handlers are
coroutines on that loop; any touch of the IFC model is `await session.run(fn)`,
which hops to the single model-worker thread and back. The EventBus emits
synchronously on the loop; subscribers that need the UI or the viewer hub
schedule onto the same loop. Model access remains serialized per session.
Validation jobs and structured property transactions run in supervised
subprocesses with minimal environments and capability restrictions. They
communicate through typed JSON and durable content-addressed artifacts.
