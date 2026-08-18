<p align="center">
  <a href="https://nbharathik.github.io/ifc-console/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/horizontal-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/horizontal-light.svg">
      <img alt="IFC CONSOLE" src="https://raw.githubusercontent.com/nbharathik/ifc-console/main/docs/assets/brand/horizontal-light.svg" width="60%">
    </picture>
  </a>
</p>

**A terminal interface that connects IFC files to LLMs.** `ifc-console` loads
your model with IfcOpenShell and serves it over MCP. Claude, Cursor, VS Code,
Codex, and other MCP clients can inspect or edit the model while you keep
control from the terminal.

No Blender or host BIM application is required. It runs locally on Windows,
macOS, and Linux.

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

## Why ifc-console

LLMs are good at BIM data work: finding elements, auditing properties, writing
IfcOpenShell scripts, and explaining schema quirks. ifc-console is the safe,
zero-setup bridge between an LLM client and an IFC file.

- **You stay in control.** The `ask`/`edit` switch decides whether the LLM may
  change the in-memory model. AI saving is disabled by default, so only you
  persist reviewed changes with `/save`.
- **It runs anywhere.** The core Python package runs on Windows, macOS, and
  Linux. The local 3D viewer is an optional install.
- **It is honest.** Errors include useful hints. Mutations are audited, saves
  are atomic with backups, and the docs state what the sandbox does and does
  not guarantee.

## Install and run

```bash
uv tool install ifc-console
# Include the optional 3D viewer and browser chat:
uv tool install "ifc-console[viewer]"
```

You can use pip instead, or try the core application once with
`uvx ifc-console`.

Start it in the folder containing your models:

```bash
cd path/to/your/models
ifc-console
```

Then:

```text
> /file             choose an IFC model
> /connect codex    copy one-time client setup
> /viewer           open the optional 3D viewer
```

Paste the copied setup into your AI client and restart that client once. Future
sessions are simply: start ifc-console, choose a file, and chat.

## Ask or edit

| mode | what the AI can do |
| ---- | ------------------ |
| `ask` (default) | inspect and analyze the model |
| `edit` | also change the model in memory |

Use `/mode edit` only when you want changes. Review them, then `/save` to keep
them or `/reload` to discard them. The AI cannot change the mode, and AI tools
cannot save unless you explicitly enable `files.allow_ai_save`.

On CPython 3.12+, eligible read-only generated code normally runs in a
restricted process without network or subprocess access. Python 3.10 and 3.11
remain supported, but cannot provide that complete audit-hook boundary; `auto`
reports a guarded fallback and `strict` refuses it. Mutating code runs in the
main process because it must reach the live model. Read
[Safety](https://nbharathik.github.io/ifc-console/safety/) before editing
untrusted files or prompts.

## What it includes

- IFC queries, properties, quantities, validation, clash detection, and CSV
  export.
- Multi-model coordination and an offline IFC/IfcOpenShell reference.
- A framework-neutral agent SDK with individually selectable IFC tools, lazy
  framework adapters, MCP composition, host approvals, and embeddable surfaces.
- Optional browser viewer, chat panel, and trusted operation plugins.

```python
from ifc_console import Workbench

with Workbench.open("tower.ifc") as wb:
    walls = wb.query("IfcWall")
    print(len(walls))
```

## Documentation

- [Getting started](https://nbharathik.github.io/ifc-console/getting-started/)
- [Console and client setup](https://nbharathik.github.io/ifc-console/console/)
- [Safety model](https://nbharathik.github.io/ifc-console/safety/)
- [Python SDK](https://nbharathik.github.io/ifc-console/sdk/)
- [MCP tools](https://nbharathik.github.io/ifc-console/tools/) and [CLI](https://nbharathik.github.io/ifc-console/cli/)
- [Troubleshooting](https://nbharathik.github.io/ifc-console/troubleshooting/)

For development setup and tests, see the [contributing guide](docs/contributing.md).

## License

The core package is Apache-2.0 and uses IfcOpenShell (LGPL-3.0-or-later). The
optional viewer includes Three.js (MIT) and web-ifc (MPL-2.0).

Inspired by [Bonsai MCP](https://github.com/Show2Instruct/bonsai-mcp).
