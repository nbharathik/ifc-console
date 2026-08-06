<p align="center">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-light.svg#only-light">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-dark.svg#only-dark">
</p>

**A terminal interface to connect IFC files to LLMs.** `ifc-console` is a
terminal-first MCP server for IFC/BIM models. One command loads a model with
IfcOpenShell and starts an MCP endpoint any LLM client can connect to. A console
lets you pick files, set a safety mode, and watch every tool call live.

No Blender. No host application. Works on Windows, macOS, and Linux.

```
+------------+   MCP (streamable HTTP / stdio)   +---------------------------+
| LLM client | --------------------------------> | ifc-console (one process) |
| Claude ... |                                   |  +- MCP tool layer        |
+------------+                                   |  +- IfcOpenShell session  |
                                                 |  +- Policy engine (modes) |
        you, in a terminal --------------------> |  +- Interactive console   |
        your browser (optional 3D viewer) -----> |  +- Web viewer (localhost)|
                                                 +---------------------------+
```

## Why ifc-console

LLMs are good at BIM data work: finding elements, auditing properties, writing
IfcOpenShell scripts, explaining schema quirks. ifc-console is the safe,
zero-setup bridge between an LLM client and an IFC file.

- **You stay in control.** One switch (`ask` or `edit`) gates everything the
  LLM does. In `ask` it can only query. In `edit` it may change the model. Only
  you flip the switch, in your terminal. Finer permission prompts stay in your
  AI client.
- **It runs anywhere.** A pure Python package that runs on Windows, macOS,
  and Linux, with the 3D viewer built in.
- **It is honest.** Errors are machine-readable with hints. Mutations are
  audited to JSONL. Saves are atomic with automatic backups. The docs say
  plainly what the sandbox does and does not guarantee.

## Highlights

- **24 core MCP tools, plus an optional 4-tool viewer.** Structured queries
  (project info, spatial tree, selectors, element details, psets, schema docs),
  file handling, multi-file workspace tools, and a gated Python
  `execute_ifc_code` power tool.
- **Interactive console** styled after coding-agent CLIs: slash commands
  (`/file`, `/mode`, `/viewer`, `/connect`), a completion menu, command history,
  and a live feed of every MCP call.
- **3D viewer** in your browser, included in every install. Click an
  element so the LLM knows what "this wall" means. It highlights elements and
  takes screenshots to check its own work. Fully local and token-protected.
- **Works with any MCP client:** Claude Code, Claude Desktop, Cursor, VS Code,
  Codex. Streamable HTTP or stdio.

## Quick taste

```console
$ ifc-console
IFC CONSOLE  v0.1.4
a terminal interface to connect IFC files to LLMs
  model  /file to pick
  mode   ask  (AI is query-only; /mode edit lets it change the model)

> /file          # pick Duplex_A.ifc from the list
> /connect       # copy the claude mcp add command
> /viewer        # open the 3D view in the browser
```

Then, in your LLM client:

> "How many walls per storey, and which of them have no FireRating?"

## Where next

- [Getting started](getting-started.md): install and first session.
- [The console](console.md): every slash command.
- [Connecting clients](clients.md): Claude Code, Claude Desktop, Cursor,
  VS Code, Codex.
- [Safety model](safety.md): what each mode guarantees.
- [Code sandbox](sandbox.md): where AI-generated code runs, and what it cannot reach.
- [MCP tools](tools.md): the full tool reference.
