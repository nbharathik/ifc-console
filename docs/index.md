<p align="center">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-light.svg#only-light">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-dark.svg#only-dark">
</p>

**A local bridge between IFC models and AI tools.** `ifc-console` loads a model
with IfcOpenShell and exposes it through MCP, Python, an interactive terminal,
and optional browser tools.

```text
+------------+   MCP (streamable HTTP / stdio)   +---------------------------+
| LLM client | --------------------------------> | ifc-console (one process) |
| Claude ... |                                   |  +- MCP tool layer        |
+------------+                                   |  +- IfcOpenShell session  |
                                                 |  +- Policy engine (modes) |
        you, in a terminal --------------------> |  +- Interactive console   |
       your browser (optional 3D viewer) ------> |  +- Web viewer (localhost)|
                                                 +---------------------------+
```

## Start here

```bash
uv tool install ifc-console
cd path/to/your/models
ifc-console
```

In the console, run `/file`, then `/connect <client>`. The setup is one-time;
after that, choose a model and ask questions from your AI client.

```text
> /file
> /connect codex
> /viewer
```

[Follow the first-session guide](getting-started.md){ .md-button .md-button--primary }

## Where next

| goal | page |
| ---- | ---- |
| learn the terminal | [Console](console.md) |
| connect Claude, Cursor, VS Code, or Codex | [Connecting clients](clients.md) |
| understand editing and saving | [Safety](safety.md) |
| use the browser tools | [3D viewer](viewer.md) and [Chat](chat.md) |
| automate with Python | [Python SDK](sdk.md) |
| build an agent | [Agent applications](agents.md) |
| run repeatable checks | [Workflows](workflows.md) |
| look up commands | [CLI](cli.md) and [MCP tools](tools.md) |
| fix a problem | [Troubleshooting](troubleshooting.md) |

The base install contains no browser assets. Add `ifc-console[viewer]` only
when you need the local 3D viewer and chat panel.
