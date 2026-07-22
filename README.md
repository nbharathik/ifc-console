<p align="center">
  <a href="https://nbharathik.github.io/ifc-console/">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="docs/assets/brand/horizontal-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="docs/assets/brand/horizontal-light.svg">
      <img alt="IFC CONSOLE" src="docs/assets/brand/horizontal-light.svg">
    </picture>
  </a>
</p>

**A terminal interface to connect IFC files to LLMs.** `ifc-console` loads
your model with IfcOpenShell and serves it over MCP, so any LLM client
(Claude Code, Claude Desktop, Cursor, VS Code, Codex) can query and edit it,
while you stay in control from your terminal. No Blender, no host app.

Docs: <https://nbharathik.github.io/ifc-console/>

## Install and run

Install from source. Needs git and [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/nbharathik/ifc-console
cd ifc-console
uv tool install .    # puts the ifc-console command on your PATH, viewer included
```

To update later: `git pull`, then `uv tool install . --force`. Working on
ifc-console itself? See
[Development](https://nbharathik.github.io/ifc-console/development/).

Start it in the folder with your models:

```bash
cd path/to/your/models
ifc-console
```

The MCP server comes up right away. Then, in the console:

```
> /file        pick a model from this folder
> /connect all one-time HTTP setup for every supported LLM client
> /copy codex  copy one complete client config to the clipboard
> /mode edit   let the AI change the model (ask = query-only, the default)
> /viewer      3D view in your browser
> /help        everything else
```

Wire up each client once. The token is stable per machine and no IFC path is
stored in the client config, so your daily loop is just `ifc-console`, `/file`,
and chat.

## The mode switch

One switch, owned by you; the LLM cannot change it. Anything finer-grained
(per-tool prompts, allowlists) belongs to your AI client.

| mode | what the LLM can do |
| ---- | ------------------- |
| `ask` (default) | query and generate code; anything that would change the model errors and tells it to ask you |
| `edit` | change and save the model; saves still make backups |

Switch with `/mode`. Every mutation path is gated, saves are atomic with
timestamped backups, and each session writes an audit log.

Honest caveat: the guards stop accidents, not a determined adversary. Treat
`edit` mode plus untrusted prompts like running a stranger's script.

## What the LLM gets

**11 core tools**: project info, spatial tree, selector queries, element
details, property sets, schema docs, file list/open/save, and a gated
Python `execute_ifc_code` power tool.

**3 more while the viewer runs**: read your click-selection, highlight
elements, and screenshot the canvas so it can check its own work.

Every response is one JSON envelope with an actionable hint on failure.
Full reference: [MCP tools](https://nbharathik.github.io/ifc-console/tools/).

## The 3D viewer

Type `/viewer`. It runs entirely on localhost behind your session token:
click an element and the LLM knows what "this wall" means; it highlights
elements back and takes screenshots. Edits refresh the view live.

## Docs

- [Getting started](https://nbharathik.github.io/ifc-console/getting-started/)
- [The console](https://nbharathik.github.io/ifc-console/console/) and [connecting clients](https://nbharathik.github.io/ifc-console/clients/)
- [Safety model](https://nbharathik.github.io/ifc-console/safety/) and [3D viewer](https://nbharathik.github.io/ifc-console/viewer/)
- [Development](https://nbharathik.github.io/ifc-console/development/)

## License

Apache-2.0. Bundles three.js (MIT) and web-ifc (MPL-2.0, unmodified); uses
IfcOpenShell (LGPL-3.0-or-later) as a library.
