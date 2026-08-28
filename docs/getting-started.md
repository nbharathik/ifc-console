# Getting started

Install IFC Console, open a model, and connect one AI client. The browser
workspace, editing, and automation are optional.

## Requirements

- Python 3.10 to 3.14 on Windows, macOS, or Linux.
- A terminal.
- Optionally, an MCP client such as Claude, Cursor, VS Code, or Codex.

The strict generated-code sandbox requires CPython 3.12 or newer. Older
versions support the documented `auto` fallback.

## Install

| install | includes |
| ------- | -------- |
| `ifc-console` | terminal, MCP, SDK, agents, workflows, chat runtime, and core operations |
| `ifc-console[viewer]` | the core package plus local viewer and browser chat assets |
| `ifc-console[validation]` | the core package plus IDS validation |

```bash
# uv
uv tool install ifc-console
uv tool install "ifc-console[viewer]"  # browser workspace

# pip
pip install ifc-console
pip install "ifc-console[viewer]"      # browser workspace
```

For a one-time core run, use `uvx ifc-console`. Upgrade an installed uv tool
with `uv tool upgrade ifc-console`.

!!! note "Working on the source code?"
    Clone the repository, run `uv sync --extra dev`, then use
    `uv run ifc-console`. See [Development](development.md).

Verify the install:

```bash
ifc-console doctor
```

`viewer assets: optional` is expected after a core-only install.

## Open a model

Start in the folder containing your IFC files:

```bash
cd path/to/your/models
ifc-console
```

The console starts an MCP endpoint at `http://127.0.0.1:8383/mcp`. Open a
model with `/file` or `/file path/to/model.ifc`.

| command | purpose |
| ------- | ------- |
| `/file` | open or switch the active model |
| `/status` | show model, mode, server, and viewer state |
| `/connect <client>` | copy setup for an AI client |
| `/viewer` | open the optional browser workspace |
| `/agent` | open the General assistant |
| `/agent list` | list every built-in and custom assistant |
| `/mode edit` | allow in-memory model changes |
| `/save` / `/reload` | keep or discard in-memory changes |
| `/help` | show terminal help |

## Connect a client

Run the command for your client:

```text
/connect codex
/connect cursor
/connect claude-desktop
```

Paste the generated configuration, then restart or reload that client once.
The setup connects to the console, so it remains valid when you switch models.

Try:

> Summarize the project, its storeys, and the number of elements by type.

Every operation appears in the console feed. See [Connecting clients](clients.md)
for configuration locations and alternative transports.

## Edit safely

The default `ask` mode blocks model changes. To edit:

1. Run `/mode edit` and confirm.
2. Ask for the change and review the in-memory result.
3. Run `/save` to keep it or `/reload` to discard it.
4. Return to `/mode ask` when finished.

AI tools cannot save by default. `files.allow_ai_save=true` enables automated
saving, but each overwrite still creates a backup. Read [Safety](safety.md)
before using edit mode with untrusted content.

## Run without the terminal UI

```bash
# HTTP server without the TUI
ifc-console --no-tui --file model.ifc

# Client-owned stdio server
ifc-console serve --stdio --file model.ifc --mode ask
```

stdio has no browser surface. Use the console or `--no-tui` when you need the
viewer or chat panel.

## Next steps

- [The console](console.md)
- [3D viewer](viewer.md) and [browser chat](chat.md)
- [Python SDK](sdk.md) and [agent applications](agents.md)
- [Troubleshooting](troubleshooting.md)
