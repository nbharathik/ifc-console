<p align="center">
  <a href="https://nbharathik.github.io/ifc-console/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/horizontal-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/horizontal-light.svg">
      <img alt="IFC CONSOLE" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/horizontal-light.svg" width="60%">
    </picture>
  </a>
</p>

**Connect IFC models to LLMs without a host BIM application.** `ifc-console`
loads a model with IfcOpenShell and exposes it through MCP, Python, a terminal,
and built-in agent workflows. Claude, Cursor, VS Code, Codex, and other MCP
clients can inspect or edit the model while you keep control from the console.

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
| `ifc-console` | terminal, MCP, SDK, agents, workflows, chat runtime, and IFC operations |
| `ifc-console[viewer]` | everything above plus the local 3D viewer and browser chat assets |
| `ifc-console[validation]` | IDS validation support |
| `ifc-console[geometry]` | Trimesh-backed raw-mesh health checks |

```bash
uv tool install ifc-console
# Or include the browser workspace:
uv tool install "ifc-console[viewer]"

cd path/to/your/models
ifc-console
```

You can use `pip` instead, or run the core application once with
`uvx ifc-console`. In the console:

```text
> /file             choose an IFC model
> /connect codex    copy one-time client setup
> /viewer           open the optional browser workspace
```

The `[viewer]` extra installs `ifc-console-viewer`, an asset-only wheel that
contains Three.js, web-ifc, and the browser application. All MCP, SDK, chat,
agent, and workflow code remains in `ifc-console`.

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
- A typed, framework-neutral SDK with scoped toolsets, MCP sources, approvals, jobs, artifacts, and workflows.
- General, measurement, document, and model-review agents in the main package.
- Project document retrieval, PDF and image vision, recipes, skills, and reviewable AI-marked changes.
- Optional local 3D viewing, browser chat, selection-aware tools, plugins, and conversation history.

## Documentation

- [Getting started](https://nbharathik.github.io/ifc-console/getting-started/)
- [Console and client setup](https://nbharathik.github.io/ifc-console/console/)
- [Python SDK](https://nbharathik.github.io/ifc-console/sdk/) and [agent applications](https://nbharathik.github.io/ifc-console/agents/)
- [MCP tools](https://nbharathik.github.io/ifc-console/tools/) and [workflows](https://nbharathik.github.io/ifc-console/workflows/)
- [3D viewer](https://nbharathik.github.io/ifc-console/viewer/) and [browser chat](https://nbharathik.github.io/ifc-console/chat/)
- [Troubleshooting](https://nbharathik.github.io/ifc-console/troubleshooting/)

For development setup and tests, see [Contributing](docs/contributing.md).

## License

The core and viewer asset packages are Apache-2.0. IfcOpenShell is
LGPL-3.0-or-later, Trimesh is MIT, Three.js is MIT, and web-ifc is MPL-2.0.

Inspired by [Bonsai MCP](https://github.com/Show2Instruct/bonsai-mcp).
