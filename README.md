<p align="center">
  <a href="https://nbharathik.github.io/ifc-console/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/horizontal-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/horizontal-light.svg">
      <img alt="IFC CONSOLE" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/horizontal-light.svg" width="60%">
    </picture>
  </a>
</p>

**A terminal interface to connect IFC files to LLMs.** `ifc-console` loads
your model with IfcOpenShell and serves it over MCP, so any LLM client
(Claude Code, Claude Desktop, Cursor, VS Code, Codex) can query and edit it,
while you stay in control from your terminal. No Blender, no host app.

<div align="center">
  <table align="center" width="90%">
    <tr>
      <th align="center" width="30%">Terminal console</th>
      <th align="center" width="24%">AI assistant</th>
      <th align="center" width="30%">3D viewer</th>
    </tr>
    <tr>
      <td align="center"><img alt="The ifc-console terminal with a model loaded and the command menu open" width="92%" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/console.png"></td>
      <td align="center"><img alt="Claude Desktop summarising the loaded IFC model" width="92%" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/claude.png"></td>
      <td align="center"><img alt="The ifc-console 3D web viewer with model tree, 3D view and properties panel" width="92%" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/viewer.png"></td>
    </tr>
    <tr>
      <td align="center">The console to open an IFC file and decide what the AI is allowed to do.</td>
      <td align="center">An AI assistant connected to ifc-console, answering questions about the open model.</td>
      <td align="center">The built-in viewer in the browser, showing the model tree, the 3D view, and element properties.</td>
    </tr>
  </table>
</div>

## Install and run

Install from [PyPI](https://pypi.org/project/ifc-console/) with
[uv](https://docs.astral.sh/uv/) or pip:

```bash
uv tool install ifc-console
# or: pip install ifc-console
```

Or try it without installing: `uvx ifc-console`. To update later:
`uv tool upgrade ifc-console`.

Start it in the folder with your models:

```bash
cd path/to/your/models
ifc-console
```

The MCP server comes up right away. Then, in the console:

```
> /file        pick a model from this folder (or /file <path>)
> /connect all one-time bridge setup for every supported LLM client
> /copy codex  copy one complete client config to the clipboard
> /mode edit   let the AI change the model (ask = query-only, the default)
> /viewer      3D view in your browser
> /kb          search the offline IFC reference
> /help        everything else
```

Wire up each client once. The token is stable per machine and no IFC path is
stored in the client config, so your daily loop is just `ifc-console`, `/file`,
and chat.

## The mode switch

One switch, owned by you; the LLM cannot change it. Internally, every operation
declares typed capabilities and `ask`/`edit` expand to compatibility profiles.
This keeps the simple interactive switch while giving SDKs, automation, and
future enterprise policy one enforceable permission vocabulary.

| mode | what the LLM can do |
| ---- | ------------------- |
| `ask` (default) | query and generate code; anything that would change the model errors and tells it to ask you |
| `edit` | change and save the model; saves still make backups |

Switch with `/mode`. Every mutation path is gated, saves are atomic with
timestamped backups, and each session writes a redacted, integrity-chained
audit log. Verify a stored chain with `ifc-console sessions verify <id>`.

## The code sandbox

The mode switch decides whether generated code may change your model. The
sandbox decides what it can do to everything else.

Eligible read-only runs, which includes everything allowed in `ask` mode, use a
separate process with **no network, no subprocesses, no credentials in its
environment**, a memory cap, and read access limited to your model directories.
The default `sandbox.mode=auto` reports and uses in-process guards if the model
copy or worker is unavailable; `/sandbox strict` refuses instead. Enforcement
inside the worker sits on CPython audit hooks, so even code that escapes the
namespace guards and reaches the real builtins still cannot open a socket or
start a process.

Honest caveat: mutating code still runs in-process behind the guards, because
the edit has to land in the live model. Treat `edit` mode plus untrusted prompts
like running a stranger's script.

## What the LLM gets

**36 core tools**: project info, spatial tree, selector queries, element
details, property sets, schema docs, validation, quantities, clash detection,
CSV export, file list/open/save, workspace tools for multi-file work
(find, attach, switch), the offline knowledge search, and a gated Python
`execute_ifc_code` power tool. Durable validation jobs add progress,
cancellation, revision checks, and checksum-verified JSON/SARIF artifacts.
Resumable validation and streaming-query batches add bounded concurrency,
fail-fast or continue policies, immutable input manifests, verified result
reuse, and aggregate artifacts without requiring an open model or an LLM.
Versioned JSON/YAML workflows compose those batches into dependency graphs with
no-execution planning, durable progress, cancellation, dead-process recovery,
safe resume, and one content-addressed result manifest. Run one with
`ifc-console run workflow.yaml --plan`, then without `--plan` to execute it.
Structured property and classification previews add caller-only approval, verified commit,
automatic rollback, and guarded restore without giving an AI direct commit
authority. Commit and restore are durable jobs with explicit cancellation
boundaries and fsynced recovery journals. Large IFC transaction and artifact
copies are streamed with checksum verification. Artifact pins and
reference-aware dry-run cleanup keep retained
jobs and transaction history safe. Correlation IDs connect operation calls,
jobs, workers, transaction records, artifacts, and audit events.

**4 more while the viewer runs**: read your click-selection, highlight
elements, apply color themes, and screenshot the canvas so it can check
its own work.

Every response is one JSON envelope with an actionable hint on failure.
Full reference: [MCP tools](https://nbharathik.github.io/ifc-console/tools/).

## The offline knowledge index

The most common way an AI gets IFC wrong is a confident guess: a property set
that does not exist, an `ifcopenshell.api` call with the wrong name. So the
model gets a searchable reference instead, built on your machine from the
ifcopenshell package you already installed: 2,300 entities, 1,600 property
sets, 8,500 properties, every API function, and 25 code recipes that the test
suite actually executes. No download, no network, no embeddings, about 23 MB of
SQLite. Search it yourself with `/kb`, or
`ifc-console knowledge search "which pset carries fire rating"`.

## The SDK

Everything the LLM can do, from a script. No server, no terminal, no port.

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    walls = wb.query("IfcWall, Pset_WallCommon.FireRating=F30")
    issues = wb.validate()
    tools = wb.tools()            # schemas + capabilities, any provider
    wb.call("query_elements", query="IfcDoor")
```

`tools()` plus `call()` is a complete agent binding, and it is deliberately
vendor neutral: no LLM client, no API key, no provider SDK. Use
`tools(permitted_only=True)` to give an agent only operations its current
authority can invoke. The wheel includes a PEP 561 marker for IDE and type
checker support. The ask/edit gate still applies. Full reference:
[Python SDK](https://nbharathik.github.io/ifc-console/sdk/).

Trusted local packages can add typed operations through the versioned,
deny-by-default [plugin API](https://nbharathik.github.io/ifc-console/plugins/).
One registered operation is immediately available through the SDK, MCP, and
chat under the same capability checks and audit trail.

The same SDK runs headless multi-model workflows without opening a model:

```python
with Workbench.open(home=".ifc-console-ci") as wb:
    plan = wb.plan_workflow("workflow.yaml")
    run = wb.submit_workflow_plan(plan)
    completed = wb.wait_workflow(run.workflow_id)
```

## The 3D viewer

Type `/viewer` to open it. It runs entirely on localhost behind your session
token:
click an element and the LLM knows what "this wall" means; it highlights
elements back and takes screenshots. Edits refresh the view live.

## The chat panel

Type `/chat` and the 3D view opens in your browser with a chat panel docked
beside it (`/chat solo` leaves the 3D view out). It drives the same tools an
MCP client gets, under the same ask/edit gate, and shows every tool call it
made under the answer.

Bring your own model: **OpenAI**, **Claude**, **OpenRouter**, or any
OpenAI-compatible local server (**vLLM**, LM Studio, Ollama). The key comes
from the usual environment variable or from the panel, and is never written to
disk. This is the one part of ifc-console that talks to the internet, so it is
off until you turn it on; `chat.local_only true` keeps it on your machine.

The same loop is one SDK call: `wb.ask("which walls have no fire rating?")`.

## Install from source (for development)

If  you would like to work on the code itself, clone the repo and install from your checkout (needs git and uv):
```bash
git clone https://github.com/nbharathik/ifc-console
cd ifc-console
uv tool install .
```

To update later: `git pull`, then `uv tool install . --force`. For working on
the code itself, see
[Development](https://nbharathik.github.io/ifc-console/development/).

## Docs

- [Getting started](https://nbharathik.github.io/ifc-console/getting-started/)
- [Python SDK](https://nbharathik.github.io/ifc-console/sdk/), [knowledge index](https://nbharathik.github.io/ifc-console/knowledge/), and [chat panel](https://nbharathik.github.io/ifc-console/chat/)
- [The console](https://nbharathik.github.io/ifc-console/console/) and [connecting clients](https://nbharathik.github.io/ifc-console/clients/)
- [Safety model](https://nbharathik.github.io/ifc-console/safety/), [code sandbox](https://nbharathik.github.io/ifc-console/sandbox/), and [3D viewer](https://nbharathik.github.io/ifc-console/viewer/)
- [Development](https://nbharathik.github.io/ifc-console/development/)
- [Changelog](https://github.com/nbharathik/ifc-console/blob/main/CHANGELOG.md)

## License

Apache-2.0. Bundles three.js (MIT) and web-ifc (MPL-2.0, unmodified); uses
IfcOpenShell (LGPL-3.0-or-later) as a library.

## Acknowledgments

Inspired by [Bonsai MCP](https://github.com/Show2Instruct/bonsai-mcp). If you
would like to work with the Bonsai viewer in Blender instead of a standalone
terminal, check that project out.
