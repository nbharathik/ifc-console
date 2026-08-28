<p align="center">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-light.svg#only-light">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-dark.svg#only-dark">
</p>

**A local bridge between IFC models and AI tools.** `ifc-console` provides the
terminal, MCP server, Python SDK, agent workflows, chat runtime, and IFC
operations. The optional `ifc-console-viewer` wheel adds only browser assets.

```text
LLM client ---- MCP ----+
Python application -----+--> ifc-console --> IfcOpenShell model
Terminal ---------------+       |
Browser, with [viewer] --+       +--> policy, jobs, workflows, audit
```

## Start here

```bash
uv tool install ifc-console
cd path/to/your/models
ifc-console
```

Run `/file`, then `/connect <client>`. Add `ifc-console[viewer]` if you also
want the local 3D viewer and browser chat.

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
