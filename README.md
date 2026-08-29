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
| `ifc-console` | console/TUI, deterministic IFC operations and workflows, MCP, Python SDK, and the local 3D viewer |
| `ifc-console-agents` | compatible core plus the complete agent SDK, providers/chat, built-in/custom packs, browser panel, PDF ingestion/rendering, LangGraph checkpoints, devkit, and testing helpers |
| `ifc-console[validation]` | IDS validation support |
| `ifc-console[geometry]` | Trimesh-backed raw-mesh health checks |

```bash
# Core product:
uv tool install ifc-console

# Or core plus the complete agents product:
uv tool install --with ifc-console-agents ifc-console

cd path/to/your/models
ifc-console
```

You can use `pip` instead, or run the core application once with
`uvx ifc-console`. In the console:

```text
> /file             choose an IFC model
> /connect codex    copy one-time client setup
> /viewer           open the bundled browser viewer
> /agent            open the Agent workspace (agent install only)
```

With `pip`, install `ifc-console` for the deterministic product or
`ifc-console-agents` for the agent product and its compatible core. Installed
agent features register through `ifc_console.extensions`; core never imports
an agent implementation directly. Existing `ifc-console[viewer]` and
`ifc-console-viewer` installs remain one-release compatibility no-ops/shims.
New installations need neither because Three.js, web-ifc, WASM, and the viewer
application are part of `ifc-console`.

## Safety

`ask` mode is read-only. Use `/mode edit` to allow in-memory changes, review
them, then `/save` to keep them or `/reload` to discard them. The AI cannot
change the mode and cannot save unless you enable `files.allow_ai_save`.

Eligible read-only generated code runs in a restricted process on CPython
3.12+. Python 3.10 and 3.11 use the documented `auto` fallback, while `strict`
refuses an unavailable boundary. Read the [safety model](https://nbharathik.github.io/ifc-console/safety/)
before editing untrusted files or prompts.

## Included features

- IFC queries, schema and IDS validation, clashes, quantities, geometry, CSV export, and multi-model review.
- A typed, framework-neutral core SDK with scoped toolsets, MCP sources, jobs, artifacts, and deterministic workflows.
- A bundled local 3D viewer with selection-aware MCP tools, measurements, sections, and screenshots, usable without an LLM.
- Optional general, measurement, document, and model-review agents from `ifc-console-agents`.
- Optional provider chat, custom packs, project document retrieval, vision, skills, and reviewable AI-marked changes.

## Documentation

- [Getting started](https://nbharathik.github.io/ifc-console/getting-started/)
- [Console and client setup](https://nbharathik.github.io/ifc-console/console/)
- [Python SDK](https://nbharathik.github.io/ifc-console/sdk/) and [agent applications](https://nbharathik.github.io/ifc-console/agents/)
- [MCP tools](https://nbharathik.github.io/ifc-console/tools/) and [workflows](https://nbharathik.github.io/ifc-console/workflows/)
- [3D viewer](https://nbharathik.github.io/ifc-console/viewer/) and [Agent workspace](https://nbharathik.github.io/ifc-console/chat/)
- [Troubleshooting](https://nbharathik.github.io/ifc-console/troubleshooting/)

For development setup and tests, see [Contributing](docs/contributing.md).

## License

The core and agent packages are Apache-2.0. IfcOpenShell is
LGPL-3.0-or-later, Trimesh is MIT, Three.js is MIT, and web-ifc is MPL-2.0.

Inspired by [Bonsai MCP](https://github.com/Show2Instruct/bonsai-mcp).
