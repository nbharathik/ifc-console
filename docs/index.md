<p align="center">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-light.svg#only-light">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-dark.svg#only-dark">
</p>

**A local IFC workbench and bridge.** `ifc-console` provides the terminal,
MCP server, deterministic Python SDK and workflows, IFC operations, and local
3D viewer. It works fully without an LLM. Install `ifc-console-agents` when you
also want provider chat, agent applications, reusable packs, and their browser
panel.

```text
MCP client ---------+
Python application -+--> ifc-console --> IfcOpenShell model
Terminal -----------+       |   |
Browser viewer -----+       |   +--> policy, jobs, workflows, audit
                            |
                    ifc-console-agents (optional extension)
```

## Start here

```bash
uv tool install ifc-console
cd path/to/your/models
ifc-console
```

Run `/file`, then `/viewer` for local visual work or `/connect <client>` for an
external MCP client. Add `ifc-console-agents` only when you want the built-in
Agent workspace experience.

```text
> /file
> /connect codex
> /viewer
```

[Follow the first-session guide](getting-started.md){ .md-button .md-button--primary }

## Find a topic

| goal | page |
| ---- | ---- |
| learn the terminal or connect a client | [Console](console.md) and [Clients](clients.md) |
| understand editing and saving | [Safety](safety.md) |
| use browser tools | [3D viewer](viewer.md) and [Chat](chat.md) |
| automate or build an agent | [Python SDK](sdk.md) and [Agents](agents.md) |
| run repeatable checks | [Workflows](workflows.md) |
| look up commands | [CLI](cli.md) and [MCP tools](tools.md) |
| fix a problem | [Troubleshooting](troubleshooting.md) |
