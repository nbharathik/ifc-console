<p align="center">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-light.svg#only-light">
  <img alt="ifc-console" width="360" src="assets/brand/horizontal-dark.svg#only-dark">
</p>

**A local automation interface for IFC models and LLM workflows.** `ifc-console`
loads models with IfcOpenShell and exposes the same operations through MCP, a
Python SDK, automation workflows, and an interactive console. You can inspect,
validate, coordinate, and make approved structured changes without a host BIM
application or cloud service.

No Blender. No host application. Works on Windows, macOS, and Linux.

```
+------------+   MCP (streamable HTTP / stdio)   +---------------------------+
| LLM client | --------------------------------> | ifc-console (one process) |
| Claude ... |                                   |  +- MCP tool layer        |
+------------+                                   |  +- IfcOpenShell session  |
                                                 |  +- Policy engine (modes) |
        you, in a terminal --------------------> |  +- Interactive console   |
        your browser (built-in 3D viewer) -----> |  +- Web viewer (localhost)|
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
- **It runs anywhere.** A Python package that runs on Windows, macOS, and
  Linux, with the local 3D viewer included.
- **It is honest.** Errors are machine-readable with hints. Mutations are
  audited to JSONL. Saves are atomic with automatic backups. The docs say
  plainly what the sandbox does and does not guarantee.

## Highlights

- **36 core operations, plus 4 tools enabled with the viewer.** Structured queries,
  validation, durable jobs, artifacts, approved change previews, file handling,
  multi-model workspaces, offline IFC knowledge, and a gated Python
  `execute_ifc_code` power tool all share one capability policy.
- **SDK and deterministic automation.** Use synchronous or asynchronous Python
  without starting a server, or run resumable validation and query workflows
  across many models from JSON or YAML.
- **Trusted operation plugins.** Disabled-by-default, user-allowlisted Python
  packages can add typed operations to the SDK, MCP, and browser assistant
  together.
- **Interactive console** styled after coding-agent CLIs: slash commands
  (`/file`, `/mode`, `/viewer`, `/connect`), a completion menu, command history,
  and a live feed of every MCP call.
- **Built-in 3D viewer** in your browser. Click an element so the LLM knows
  what "this wall" means, and let it highlight or screenshot results. The
  viewer stays local and token-protected.
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
- [Python SDK](sdk.md): embedded, typed access without a server.
- [Automation workflows](workflows.md): durable jobs, batches, and workflow graphs.
