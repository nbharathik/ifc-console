# Versioning and API stability

ifc-console follows semantic versioning against a precisely defined public
API. If you build a client, an agent workflow, or CI on the surfaces below,
minor and patch releases will not break you.

## What the public API is

1. **The MCP tool list**: tool names, their input schemas, their output
   schemas, and their read-only/destructive annotations.
2. **The response envelope**: every tool returns
   `{ok, data, error, meta}`, delivered both as `structuredContent` and as
   JSON text. `error` always carries `code`, `message`, and `hint`.
3. **The error-code registry**: the `code` values listed in the
   [tools reference](tools.md#error-codes).
4. **The CLI exit codes**: 0 ok, 1 runtime error, 2 environment problem,
   3 bad usage, 4 file not found or unparseable, 5 validation findings.
5. **Resource URIs** (`ifc://...`) and prompt names.
6. **The documented Python SDK**: names exported from `ifc_console`, documented
   `Workbench` and `AsyncWorkbench` methods, their call signatures, and the
   public fields of exported typed records and result models. Auxiliary
   modules public by name: `ifc_console.testing` (the agent test doubles),
   the exports of `ifc_console.knowledge` (the corpus surface), the exports
   of `ifc_console.agents.packs` (the agent pack contract), and the exports
   of `ifc_console.credentials`. Other modules below `ifc_console.*` that
   are not re-exported are internal.
7. **Plugin API version 1**: `PluginManifest`, `PluginAPI`,
   `OperationPlugin`, synchronous registration and shutdown, structured
   operation registration, capability declarations, and the result helpers
   described in the [plugin guide](plugins.md). A future incompatible plugin
   contract will use a new `manifest.api_version`.

## What is and is not a breaking change

Additive changes arrive in minor releases and do not break correct clients:

- new tools, new optional tool categories, new prompts and resources
- new optional parameters with unchanged defaults
- new fields inside `data` or `meta`
- new error codes

When a name has to go, it is deprecated first: it keeps working for at least
one minor release, warns where practical, and its replacement is named in the
changelog entry that deprecates it.

Breaking changes gate a major release:

- renaming or removing a tool, prompt, resource, or error code
- narrowing an input schema or changing a default
- changing the envelope shape
- removing a documented SDK export, narrowing a documented method signature,
  or removing a public field from an exported typed record
- accepting plugin API version 1 while changing its documented contract

Everything else (undocumented console commands and Python modules, TUI layout,
viewer UI, log formats, and the internal event schema) is product surface, not
API, and may change freely.

## How this is enforced

The contracts are executable. `tests/golden/api_contract.json` snapshots the
tool schemas, envelope schema, and error-code registry.
`tests/golden/sdk_contract.json` snapshots top-level exports, documented method
signatures, typed model schemas, enums, and plugin API records. CI fails on any
drift. An intended change requires deliberately regenerating both golden files
with `python scripts/snapshot_api.py` and reviewing the SemVer impact. Separate
tests cover lazy imports, plugin lifecycle behavior, and the installable
example plugin. Release checks verify that the base wheel is free of viewer
assets and that the separate viewer wheel carries the complete reviewed bundle.

## Optional extras

Capabilities that need an ecosystem package degrade, never break. Calling
`validate_ids` without `ifctester` returns `EXTRA_NOT_INSTALLED`; `/viewer`
without the viewer extra prints its install command. Extras are versioned
independently of the core API.
