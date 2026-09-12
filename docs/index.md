---
title: Load your models, connect any LLM
description: A local IFC workbench with a terminal, an MCP server, a Python SDK and a 3D viewer. No LLM required.
template: home.html
hide:
  - navigation
  - toc
  - footer
---

## Up and running in a minute

Install, open a model, then pick how you want to work with it.

=== "Terminal"

    ```bash title="Install and open a model"
    uv tool install ifc-console
    cd path/to/your/models
    ifc-console
    ```

    ```text title="In the console"
    > /file             choose an IFC model
    > /viewer           open the 3D viewer
    > /connect codex    print the setup for your AI client
    > /mode edit        allow changes, in a copy of the file
    ```

=== "AI client"

    ```bash title="One command per client"
    ifc-console mcp-config --client claude-code
    ifc-console mcp-config --client claude-desktop
    ifc-console mcp-config --client cursor
    ifc-console mcp-config --client vscode
    ifc-console mcp-config --client codex
    ```

    Paste the output where the console tells you, reload the client, and ask:

    > Summarize the project, its storeys, and the number of elements by type.

=== "Python"

    ```python title="Same operations, no server"
    from ifc_console import Workbench

    with Workbench.open("tower.ifc") as wb:
        print(wb.info()["project"]["name"])
        walls = wb.query("IfcWall, Pset_WallCommon.IsExternal=TRUE")
        report = wb.validate()
        print(len(walls), report["valid"])
    ```

[Read the getting started guide](getting-started.md){ .ic-text-link }
