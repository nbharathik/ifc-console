<p align="center">
  <a href="https://nbharathik.github.io/ifc-console/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/horizontal-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/horizontal-light.svg">
      <img alt="IFC CONSOLE" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/horizontal-light.svg" width="60%">
    </picture>
  </a>
</p>

**Inspect, automate, and connect IFC models without a host BIM application.**
`ifc-console` loads a model with IfcOpenShell and exposes it through MCP,
Python, a terminal, and a bundled local 3D viewer. Claude, Cursor, VS Code,
Codex, and other MCP clients can inspect or edit the model while you keep
control from the console. No LLM, provider account, or API key is required.

It runs locally on Windows, macOS, and Linux.

<div align="center">
  <table align="center" width="90%">
    <tr>
      <th align="center" width="30%">Terminal console</th>
      <th align="center" width="24%">AI assistant</th>
      <th align="center" width="30%">3D viewer</th>
    </tr>
    <tr>
      <td align="center"><img alt="The ifc-console terminal with a model loaded" width="92%" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/console.png"></td>
      <td align="center"><img alt="Claude Desktop summarising an IFC model" width="92%" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/claude.png"></td>
      <td align="center"><img alt="The local ifc-console 3D viewer" width="92%" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/viewer.png"></td>
    </tr>
  </table>
</div>

## Install and run

| install | includes |
| ------- | -------- |
| `ifc-console` | console/TUI, IFC operations and workflows, MCP, Python SDK, the local 3D viewer, and the Agent workspace with its SDK, providers, packs, and skills |
| `ifc-console[agents]` | agent engines over ACP, the system keyring for provider keys, and PDF text and page rendering for project documents |
| `ifc-console[validation]` | IDS validation support |
| `ifc-console[all]` | both extras |

```bash
uv tool install "ifc-console[agents]"

cd path/to/your/models
ifc-console
```

You can use `pip` instead, or run the application once with
`uvx ifc-console`. In the console:

```text
> /file             choose an IFC model
> /connect codex    copy one-time client setup
> /viewer           open the bundled browser viewer
> /agent            open the Agent workspace
```

Everything is in one package: Three.js, web-ifc, the viewer, and the Agent
workspace ship inside `ifc-console`, and the extras only add third-party
libraries. The separate `ifc-console-viewer` package is retired.

## Safety

`ask` mode is read-only. `/mode edit` copies the open file aside and allows
in-memory changes; `/save` writes that copy, `/save <path>` writes the result
somewhere else, and `/reload` discards it. Every edit is one undoable step
(`/undo`, `/redo`), and an edit that fails is rolled back whole. The AI cannot
change the mode, and the file you opened is never written unless you name it
yourself or enable `files.allow_ai_save`.

Eligible read-only generated code runs in a restricted process on CPython
3.12+. Python 3.10 and 3.11 use the documented `auto` fallback, while `strict`
refuses an unavailable boundary. Read the [safety model](https://nbharathik.github.io/ifc-console/safety/)
before editing untrusted files or prompts.

## Included features

- IFC queries, schema and IDS validation, clashes, quantities, geometry, CSV export, and multi-model review.
- Structured, undoable edits: `set_properties` with a dry run, all-or-nothing execution, and a viewer that follows without a rebuild.
- A lean tool profile (14 tools plus a search) that costs a client a sixth of the context of the full listing.
- A typed, framework-neutral core SDK with scoped toolsets, MCP sources, jobs, artifacts, and deterministic workflows.
- A bundled local 3D viewer with selection-aware MCP tools, measurements, sections, and screenshots, usable without an LLM.
- General, measurement, document, and model-review agents in the browser Agent workspace.
- Provider chat, custom packs, project document retrieval, vision, skills, and reviewable AI-marked changes.

## Documentation

- [Getting started](https://nbharathik.github.io/ifc-console/getting-started/)
- [The console](https://nbharathik.github.io/ifc-console/console/) and [connecting a client](https://nbharathik.github.io/ifc-console/clients/)
- [Safety](https://nbharathik.github.io/ifc-console/safety/)
- [3D viewer](https://nbharathik.github.io/ifc-console/viewer/) and [Agent workspace](https://nbharathik.github.io/ifc-console/chat/)
- [Python SDK](https://nbharathik.github.io/ifc-console/sdk/)
- [MCP tools](https://nbharathik.github.io/ifc-console/tools/) and [CLI and settings](https://nbharathik.github.io/ifc-console/cli/)
- [Troubleshooting](https://nbharathik.github.io/ifc-console/troubleshooting/)

For development setup and tests, see [Contributing](docs/contributing.md).

## License

ifc-console is Apache-2.0. IfcOpenShell is LGPL-3.0-or-later, Trimesh is MIT,
Three.js is MIT, web-ifc is MPL-2.0, and pypdfium2 is Apache-2.0 or BSD-3-Clause.

Inspired by [Bonsai MCP](https://github.com/Show2Instruct/bonsai-mcp).
