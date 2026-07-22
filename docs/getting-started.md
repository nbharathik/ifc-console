# Getting started

## Requirements

- Python 3.10 to 3.14 (Windows, macOS, Linux).
- A terminal. On Windows, use Windows Terminal for correct colors and keys.
- An MCP client to chat from: Claude Code, Claude Desktop, Cursor, VS Code,
  Codex, or anything that speaks MCP.

## Install

Install from source with git and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/nbharathik/ifc-console
cd ifc-console
uv tool install .
```

This puts the `ifc-console` command on your PATH, 3D viewer included. To
update later, run `git pull` in the checkout, then `uv tool install . --force`.

!!! note "Working on ifc-console itself?"
    Run `uv sync --extra dev` in the checkout instead and use
    `uv run ifc-console` wherever these docs say `ifc-console`. See
    [Development](development.md).

Check the environment:

```bash
ifc-console doctor
```

Every line should read `ok`.

## First session

Start ifc-console in the folder with your IFC files:

```bash
cd path/to/your/models
ifc-console
```

The console opens and the MCP server starts on
`http://127.0.0.1:8383/mcp`. You do not need a file path on the command line.
Type `/file` and pick a model from the list. It shows recent models plus every
IFC file in the current folder and one level down, filterable as you type. A
bare path typed into the prompt works too.

Useful first commands:

| command | effect |
| ------- | ------ |
| `/file` | pick and load an IFC model |
| `/status` | model, mode, server, viewer summary |
| `/connect <client>` | show and copy one client's complete one-time HTTP setup |
| `/mode edit` | let the AI change the model (`ask`, the default, is query-only) |
| `/viewer` | open the 3D viewer in your browser |
| `/help` | everything else |

## Connect your LLM clients (once)

This is a one-time step. The bearer token is persistent per machine, so client
configs keep working across restarts and model changes. Type `/connect codex`,
`/connect cursor`, or another client name in a running console. The complete
setup is copied automatically, ready to paste into the location the TUI shows.
Use `/connect all` for an overview and `/copy <client>` to copy one again. No
HTTP config contains an IFC path; `/file` selects the model for every client.

You can also generate one client setup any time:

```bash
ifc-console mcp-config --client claude-code
```

Then run the printed command:

```bash
claude mcp add --transport http --scope user ifc-console http://127.0.0.1:8383/mcp \
  --header "Authorization: Bearer <your machine token>"
```

From now on your daily flow is: `ifc-console`, `/file`, chat. Then ask your LLM
something:

> "Give me an overview of this model: schema, storeys, and element counts."

Watch the console: every tool call appears in the feed as it happens. See
[Connecting clients](clients.md) for exact client locations and opt-in
standalone stdio setups.

## Your first edit, safely

1. In `ask` mode (the default), ask your LLM: "Set FireRating F30 on every wall
   that lacks it." The mutation is blocked with an error. The LLM shows you the
   code and asks you to enable editing. Nothing touched your file.
2. When you are happy, type `/mode edit` in the console (a y/n confirm protects
   the switch) and tell the LLM to go ahead. Your AI client's own permission
   prompts, if any, still apply on top.
3. The model is now dirty. The LLM finishes with `save_ifc_file`, or you type
   `/save`. A timestamped backup of the previous version is made automatically.
   `/mode ask` locks the model again.

## Headless and scripted use

No terminal UI? Two options:

```bash
# HTTP daemon: same server, no console
ifc-console --no-tui --file model.ifc --viewer

# stdio: the MCP client starts and owns the process
ifc-console serve --stdio --file model.ifc --mode ask
```

Both work the same way: `ask` keeps the model untouchable, `--mode edit` allows
unattended edits (your AI client's own prompts are then the only gate).
