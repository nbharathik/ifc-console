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
   3 bad usage, 4 file not found or unparseable, 5 check failed.
5. **Resource URIs** (`ifc://...`) and prompt names.

## What is and is not a breaking change

Additive changes arrive in minor releases and do not break correct clients:

- new tools, new optional tool categories, new prompts and resources
- new optional parameters with unchanged defaults
- new fields inside `data` or `meta`
- new error codes

Breaking changes gate a major release:

- renaming or removing a tool, prompt, resource, or error code
- narrowing an input schema or changing a default
- changing the envelope shape

Everything else (console commands, TUI layout, viewer UI, log formats, the
internal event schema) is product surface, not API, and may change freely.

## How this is enforced

The contract is executable: `tests/golden/api_contract.json` snapshots the
tool schemas, the envelope schema, and the error-code registry, and CI fails
on any drift. An intended change requires deliberately regenerating the
golden file (`python scripts/snapshot_api.py`), which makes every API change
a reviewed, versioned decision rather than an accident.

## Optional extras

Capabilities that need an ecosystem package degrade, never break: calling
`validate_ids` without `ifctester` installed returns `EXTRA_NOT_INSTALLED`
with the install command in the hint. Extras are versioned independently of
the core API.
