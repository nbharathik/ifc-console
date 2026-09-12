---
title: Documentation
description: Guides for installing ifc-console, connecting an AI client, using the viewer, and scripting with Python.
---

# Documentation

ifc-console loads your IFC models once and lets you work with them from
the terminal, an AI client, Python, or a 3D viewer. Start with the install, then
pick the surface you need.

```mermaid
flowchart LR
    ifc[("your IFC models")] --> console["ifc-console"]
    console --> tui["terminal"]
    console --> mcp["AI client<br/>Claude, Codex, Cursor, VS Code"]
    console --> viewer["3D viewer"]
    console --> py["Python"]
```

<div class="ic-doc-start" markdown>

## Your first session

Install the package, open a model, and connect the client you already use.
Ten minutes, no API key.

[Get started](getting-started.md){ .md-button .md-button--primary }
[How safety works](safety.md){ .md-button }

</div>

## Use it

<div class="grid cards" markdown>

-   **The console**

    The terminal: commands, keys, files, and the ask/edit switch.

    [Learn the console](console.md)

-   **Connect a client**

    Claude Code, Claude Desktop, Cursor, VS Code, and Codex.

    [Connect a client](clients.md)

-   **3D viewer**

    Select, section, measure, and let the AI see what you see.

    [Open the viewer](viewer.md)

-   **Agent workspace**

    Optional chat panel beside the viewer, with your own provider key.

    [Use the workspace](chat.md)

</div>

## Build with it

<div class="grid cards" markdown>

-   **Python SDK**

    Scripts, notebooks, CI, and your own agents on the same operations.

    [Use the SDK](sdk.md)

-   **Architecture**

    How one operation core serves every interface.

    [Read the architecture](architecture.md)

</div>

## Reference

| Page | What you will find |
| :--- | :--- |
| [MCP tools](tools.md) | Every tool by group, the response envelope, and error codes. |
| [CLI and settings](cli.md) | Subcommands, startup flags, and the settings that matter. |
| [Troubleshooting](troubleshooting.md) | Connection, port, viewer, and sandbox problems with fixes. |
| [Contributing](contributing.md) | Development setup, tests, and how to report a security issue. |
