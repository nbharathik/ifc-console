# Architecture

One process, one active model, several faces over one core.

```
            +---------------------------------------------------------+
 MCP client |  FastMCP tool layer (28 tools, JSON envelope)           |
 ---------->|  TokenAuthMiddleware (bearer token, loopback only)      |
            |                                                         |
 browser    |  Viewer routes + WebSocket hub (selection, screenshots) |
 ---------->|                                                         |
            |            AppCore (the only owner of state)            |
 you ------>|  Console TUI            PolicyEngine                    |
            |  (Textual)              (ask/edit gate)                 |
            |                                                         |
            |  ModelRegistry: one worker thread per resident model    |
            |  SettingsStore  RecentsStore  BackupStore  AuditLog     |
            +---------------------------------------------------------+
```

## Key decisions

- **AppCore owns everything.** The MCP layer, console, CLI, and viewer are thin
  faces over one object. Nothing reaches the model except through it.
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
- **The revision counter.** Every load, mutation, and save bumps
  `ModelSession.revision`. `fingerprint-revision` is the ETag for the live
  model, which is how viewer tabs know when to refetch.
- **Build-free viewer.** The SPA is plain ES modules plus vendored three.js and
  web-ifc WASM, served from package data. No Node in the build, no CDN at
  runtime.
- **The viewer is a bolt-on, not a pillar.** Its code lives in one subpackage
  (`viewer/`), its HTTP routes answer 404 while disabled, and its four MCP
  tools form an optional category. Nothing in the core query/edit path imports
  viewer code; delete the subpackage and the 24 core tools still work.

## Threading model in one paragraph

The Textual app and uvicorn share the asyncio loop. MCP tool handlers are
coroutines on that loop; any touch of the IFC model is `await session.run(fn)`,
which hops to the single model-worker thread and back. The EventBus emits
synchronously on the loop; subscribers that need the UI or the viewer hub
schedule onto the same loop. The only true concurrency is the model worker, and
it is fully serialized, so there are no locks anywhere else.
