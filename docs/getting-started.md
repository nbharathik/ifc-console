# Getting started

This guide takes you from installation to your first model question. The 3D
viewer, browser chat, editing, and automation are optional.

## Requirements

- Python 3.10 to 3.14 on Windows, macOS, or Linux. The restricted generated-code
  sandbox requires CPython 3.12 or newer; older versions use the documented
  `auto` fallback or `strict` refusal behavior.
- A terminal. Windows Terminal is recommended on Windows.
- An MCP client such as Claude Code, Claude Desktop, Cursor, VS Code, or Codex.

You can skip the MCP client if you plan to use only the Python SDK or the
optional browser chat panel.

## Install

Choose one package:

| package | includes |
| ------- | -------- |
| `ifc-console` | terminal, MCP server, Python SDK, and core operations |
| `ifc-console[viewer]` | everything above, plus the 3D viewer and browser chat |
| `ifc-console[validation]` | core package plus IDS validation support |

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install ifc-console
# or, with the viewer:
uv tool install "ifc-console[viewer]"
```

With pip:

```bash
pip install ifc-console
# or, with the viewer:
pip install "ifc-console[viewer]"
```

For a one-time core run, use `uvx ifc-console`. To update an installed uv tool,
run `uv tool upgrade ifc-console`.

!!! note "Working on the source code?"
    Clone the repository, run `uv sync --extra dev`, and use
    `uv run ifc-console`. See [Development](development.md).

Check the installation:

```bash
ifc-console doctor
```

Essential checks should report `ok`. `viewer assets: optional` is expected
when you installed only the core package.

## Open your first model

Start in the folder that contains your IFC files:

```bash
cd path/to/your/models
ifc-console
```

The terminal opens and the MCP server starts on
`http://127.0.0.1:8383/mcp`. In the terminal, type:

```text
/file
```

Choose a model from the picker. It shows recent files from the current folder,
then IFC files in that folder and its immediate subfolders. You can also use
`/file path/to/model.ifc`.

Useful commands for a first session:

| command | purpose |
| ------- | ------- |
| `/file` | open or switch the active model |
| `/status` | show the model, mode, server, and viewer state |
| `/connect <client>` | copy setup for one AI client |
| `/viewer` | open the optional 3D viewer |
| `/mode edit` | allow in-memory model changes |
| `/save` / `/reload` | keep or discard in-memory changes |
| `/help` | show terminal help |

## Connect an AI client once

In the running console, use the name of your client:

```text
/connect codex
/connect cursor
/connect claude-desktop
```

The command shows where the configuration belongs and copies the complete
snippet. Paste it, then restart or reload the client once.

The setup connects to the console, not to a particular IFC file. You do not
need to edit it when you change models. The default bridge also allows the
client and console to start in either order.

Now ask your AI client:

> Summarize the project, its storeys, and the number of elements by type.

Every operation appears in the console feed. See [Connecting clients](clients.md)
for client-specific locations and alternative transports.

## Make a first edit safely

The default `ask` mode is read-only. To see the safety flow:

1. Ask the AI to set `FireRating` to `F30` on walls that lack it. The edit is
   blocked and the model remains unchanged.
2. Review the proposed action, then run `/mode edit` and confirm the switch.
3. Ask the AI to continue. The model changes in memory and becomes dirty.
4. Review the result, then run `/save` to keep it or `/reload` to discard it.
5. Run `/mode ask` when editing is finished.

AI tools cannot save an IFC file by default. Automated saving requires the
separate user setting `files.allow_ai_save=true`; every overwrite still creates
a timestamped backup. Read [Safety](safety.md) before using editing with
untrusted content.

## Run without the terminal UI

For an HTTP server without the console:

```bash
ifc-console --no-tui --file model.ifc
```

For a client-owned stdio server:

```bash
ifc-console serve --stdio --file model.ifc --mode ask
```

These are advanced deployment options. Most users should keep the interactive
console because it exposes the mode switch, activity feed, and viewer controls.

## Next steps

- Learn the terminal controls in [The console](console.md).
- Open the optional [3D viewer](viewer.md).
- Use ifc-console from scripts with the [Python SDK](sdk.md).
- If anything failed, start with [Troubleshooting](troubleshooting.md).
