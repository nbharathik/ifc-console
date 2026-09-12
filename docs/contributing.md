---
description: Development setup, tests, project scope, and how to report a security issue.
---

# Contributing

## Setup

```bash
git clone https://github.com/nbharathik/ifc-console && cd ifc-console
uv sync --all-packages --all-extras
uv run --all-packages --all-extras ifc-console doctor
uv run --all-packages --all-extras pytest
uv run --all-packages --all-extras ruff check src tests packages scripts
```

The workspace holds two packages:

```text
src/ifc_console/                                     ifc-console
packages/ifc-console-agents/src/ifc_console_agents/  ifc-console-agents
```

Both support Python 3.10 to 3.14. Core owns deterministic IFC behavior and
the viewer; agents depend on core and register through
`ifc_console.extensions`. Never import from core into `ifc_console_agents`.

For the browser panel, `npm run dev` serves a demo project and opens the
Agent workspace, `npm run check` exercises it headlessly, and `npm test`
runs the panel unit tests. None of them need an API key.

Docs are built with `uv sync --extra docs` and `uv run mkdocs serve`.

## Scope

- `ifc-console` stays deterministic: a safe IFC, MCP, SDK, terminal, and
  viewer product that needs no LLM. Provider chat, packs, and the Agent
  panel belong in `ifc-console-agents`.
- The MCP tool names, their input schemas, and the response envelope are
  public API. Changing them needs a version bump.
- Anything that widens what generated code can reach, weakens the mode model,
  or adds network calls needs a strong case.

Open a [GitHub issue](https://github.com/nbharathik/ifc-console/issues) for
bugs and ideas. Include `ifc-console doctor` output and never upload a
confidential model.

## Security

Report vulnerabilities privately through GitHub Security Advisories
("Report a vulnerability" on the repository's Security tab), not in a public
issue.

The read-only sandbox is a real boundary: an escape from the restricted
process is a security issue. The in-process guards used for mutating code in
edit mode reduce accidents but are not a boundary against hostile Python.
Reports of particular interest:

- writing to disk, or the on-disk model, from `ask` mode
- reaching the network or the OS from a sandboxed run
- reading files outside the allowed directories
- bypassing the bearer token, or reaching the server off loopback
- the viewer gaining any way to change the model

Keep `ask` mode for untrusted prompts and models, and rotate a leaked token
with `ifc-console token rotate`. See [Safety](safety.md).
