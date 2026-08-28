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
6. **The documented core Python SDK**: names exported from `ifc_console`,
   documented `Workbench` and `AsyncWorkbench` methods, their call signatures,
   and the public fields of exported typed records and result models. The
   deterministic IFC knowledge surface remains under `ifc_console.knowledge`.
7. **The documented agent SDK**: names exported from `ifc_console_agents`,
   including the provider-neutral loop, events, approvals, storage, testing
   helpers, pack contracts, and documented optional integrations. New code
   uses this namespace rather than the transitional core aliases.
8. **Plugin API version 1**: `PluginManifest`, `PluginAPI`,
   `OperationPlugin`, synchronous registration and shutdown, structured
   operation registration, capability declarations, and the result helpers
   described in the [plugin guide](plugins.md). A future incompatible plugin
   contract will use a new `manifest.api_version`.
9. **Extension API version 1**: installed companion products register through
   `ifc_console.extensions`. Manifest validation, attach/register-once,
   status, HTTP routes, declarative browser panels, and shutdown form the
   cross-distribution boundary. Core never imports extension implementations.

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

The contracts are executable. Core-only and core-plus-agents snapshots cover
tool schemas, the envelope, error codes, and each package's documented Python
surface. CI fails on unintended drift. An intended change requires deliberately
regenerating the relevant golden file and reviewing the SemVer impact. Separate
tests cover lazy imports, plugin and extension lifecycle behavior, and the
installable example plugin. Release checks verify that core contains the full
reviewed viewer bundle but no agent implementation or panel assets, while the
agent wheel owns only `ifc_console_agents` and declares a compatible core range.

## Optional extras

Capabilities that need an ecosystem package degrade, never break. Calling
`validate_ids` without `ifctester` returns `EXTRA_NOT_INSTALLED`. Agent PDF
features require `ifc-console-agents[documents]`; LangGraph adapters require
`ifc-console-agents[graph]`; `[full]` installs both. The viewer has no runtime
extra: `ifc-console[viewer]` and `ifc-console-viewer` are one-release no-op/shim
compatibility paths only.

For the same one-release transition, the former `ifc-console[graph]` and
`ifc-console[keys]` install commands forward to the matching
`ifc-console-agents` capability. New installations should use the agents
package directly.

For the initial split, core and agents release together and agents declares a
compatible core minor range. Core publishes first. A future independent agent
release cadence must introduce an explicit tag/version policy before the two
versions diverge.
