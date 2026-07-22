# Connecting clients

The recommended setup is one shared Streamable HTTP session:

```text
Claude, Cursor, VS Code, or Codex
                 |
                 v
http://127.0.0.1:8383/mcp
                 |
                 v
one ifc-console console -> the model currently selected with /file
```

Configure each client once. The config holds only the MCP URL and bearer token,
never an IFC path. After that, start `ifc-console` and use `/file` to open or
switch models. Every connected client uses the model owned by that console
session.

`/connect <client>` prints the setup for one client and copies the complete
snippet to your clipboard. `/connect all` prints them all without changing the
clipboard. Use `/copy <client>` whenever you want to copy one again. The same
output is available without opening the console:

```bash
ifc-console mcp-config --client claude-code
ifc-console mcp-config --client claude-desktop
ifc-console mcp-config --client cursor
ifc-console mcp-config --client vscode
ifc-console mcp-config --client codex
```

The default is HTTP for every client. `--transport stdio` is an opt-in for a
separate client-owned server; see [Standalone stdio](#standalone-stdio).

## One-time setup

1. Run `ifc-console` and type `/connect <client>`, such as `/connect codex`.
2. Paste the automatically copied setup into the location shown by the TUI.
3. Restart or reload that client so it discovers the MCP server.
4. For future sessions, just run `ifc-console`, pick a model with `/file`, and
   chat.

The bearer token persists in `~/.ifc-console/token`, so restarting ifc-console or
opening a different file needs no config change. Re-add the configs only after
changing the port or running `ifc-console token rotate`.

## Claude Code

Run the command printed by `/connect claude-code`:

```bash
claude mcp add --transport http --scope user ifc-console \
  http://127.0.0.1:8383/mcp \
  --header "Authorization: Bearer <token>"
```

`--scope user` makes the connection available in every project, not just the
directory where you added it.

## Claude Desktop

Claude Desktop launches local MCP entries over stdio, so the generated config
uses `mcp-remote` as a small bridge to the shared local HTTP console. This needs
Node.js 18 or newer with `npx` available.

Open **Settings > Developer > Edit Config** and add the output from
`/connect claude-desktop` to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ifc-console": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote@0.1.38",
        "http://127.0.0.1:8383/mcp",
        "--allow-http",
        "--transport",
        "http-only",
        "--header",
        "Authorization:${IFC_CONSOLE_AUTH_HEADER}"
      ],
      "env": {
        "IFC_CONSOLE_AUTH_HEADER": "Bearer <token>"
      }
    }
  }
}
```

Then warm the npx cache once in any terminal:

```bash
npx -y mcp-remote@0.1.38 --help
```

Claude Desktop gives a starting server 60 seconds to answer, and the first
uncached `npx` run downloads `mcp-remote` from the npm registry, which can take
longer than that. Warming the cache once makes every later launch instant. The
version is pinned so npx keeps serving the cached install instead of checking
the registry for a newer release on each launch; the warm-up command must use
the same pinned version, because npx caches each version spec separately.

Save and restart Claude Desktop. The bridge starts when Claude needs the server;
the ifc-console console must already be running.

## Cursor

For a connection available in every project, open Cursor's global MCP config at
`~/.cursor/mcp.json` and add:

```json
{
  "mcpServers": {
    "ifc-console": {
      "url": "http://127.0.0.1:8383/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Restart Cursor or reload its MCP servers after saving.

## VS Code

Run **MCP: Open User Configuration** from the Command Palette. User profile
config keeps the connection available across workspaces. Add:

```json
{
  "servers": {
    "ifc-console": {
      "type": "http",
      "url": "http://127.0.0.1:8383/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Save, then start or restart the server from VS Code's MCP controls.

## Codex

Add the output from `/connect codex` to `~/.codex/config.toml`:

```toml
[mcp_servers.ifc-console]
url = "http://127.0.0.1:8383/mcp"
http_headers = { Authorization = "Bearer <token>" }
```

Restart Codex after changing the config. Codex desktop, the CLI, and the IDE
extension use this shared configuration.

An older entry may use `bearer_token_env_var = "IFC_CONSOLE_MCP_TOKEN"`. That names
an environment variable, not the token itself, and the variable must exist
before Codex starts. Replacing the entry with `/connect codex` output is simpler
and avoids a missing-variable 401.

## Standalone stdio

Use stdio only when you want the client to start and own a separate ifc-console
process instead of sharing the console:

```bash
ifc-console mcp-config --client <client> --transport stdio
```

To set that server's startup model, add a path:

```bash
ifc-console mcp-config --client <client> --transport stdio \
  --file C:/models/model.ifc
```

An stdio server does not join the console session, does not follow `/file`, and
has no bearer token (its transport is a private process pipe).

## Which transport should I use?

| you want | pick |
| -------- | ---- |
| switch IFC files without editing client config | shared HTTP |
| several clients using the same loaded model | shared HTTP |
| the console mode switch, live feed, and 3D viewer | shared HTTP |
| no separate terminal and one client-owned process | standalone stdio |

!!! warning "Keep the local token private"
    The generated HTTP snippets contain a bearer token. Do not commit them to a
    public repository. If the token leaks, run `ifc-console token rotate` and
    regenerate the client configs once.
