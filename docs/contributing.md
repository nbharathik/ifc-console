---
description: Development setup, tests, project scope, and how to report a security issue.
---

# Contributing

## Setup

```bash
git clone https://github.com/nbharathik/ifc-console && cd ifc-console
uv sync --group dev --all-extras
uv run ifc-console doctor
uv run pytest
uv run ruff check src tests scripts
```

```text
src/ifc_console/           deterministic IFC behavior, MCP, terminal, viewer
src/ifc_console/agents/    provider chat, agent packs, the Agent panel, engines
```

One wheel ships everything and supports Python 3.10 to 3.14. Core never
imports `ifc_console.agents` at import time: the console attaches it as a
built-in extension, and `tests/unit/test_package_boundaries.py` keeps that true.
Tests for it live in `tests/agents/`.

For the browser panel, `npm run dev` serves a demo project and opens the
Agent workspace, `npm run check` exercises it headlessly, and `npm test`
runs the panel unit tests. None of them need an API key.

Docs are built with `uv sync --group docs` and `uv run mkdocs serve`.

Three checks keep the product lean, and each fails a test when a number moves:

- **Context budget** (`tests/unit/test_tool_budget.py`): the characters a client
  reads to list the tools and the server instructions, for the lean and the full
  profile, plus a ceiling on every description.
- **File size** (`scripts/check_file_sizes.py`): no Python file over 1,500 lines
  and no JavaScript file over 2,500. Files already over may only shrink; run it
  with `--update` after splitting one.
- **Contract goldens**: tool names, argument schemas, annotations, the envelope,
  and the error codes. After an intended change run
  `uv run python scripts/update_goldens.py` and review the diff; additions are
  fine, renames and removals are breaking.

`uv run python scripts/bench.py` measures load, queries, edits, contention,
imports, dependencies and the tool listing, and writes JSON to compare runs.

## Scope

- Everything except the Agent workspace stays deterministic: a safe IFC, MCP,
  SDK, terminal, and viewer product that needs no LLM. Provider chat, packs,
  and the Agent panel live in `ifc_console.agents`.
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
