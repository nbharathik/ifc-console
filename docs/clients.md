# Connecting clients

Every client talks to one shared console session, through a small stdio bridge
the client starts for itself:

```text
Claude, Cursor, VS Code, or Codex
                 |
                 v      (stdio, started by the client)
      ifc-console bridge
                 |
                 v      (loopback HTTP, token from ~/.ifc-console/token)
one ifc-console console -> the model currently selected with /file
```

The bridge is why start order does not matter: it always starts, so the client
always connects, and it reaches the console as soon as one is running. If you
open your AI client first and ifc-console second, it still works, and the tool
list refreshes by itself.

Before forwarding a request, the bridge sends a fresh random challenge to the
configured loopback port. It sends the bearer token only after the listener
returns a valid HMAC proof bound to that port and the identity route. This
prevents an unrelated application that happens to own the port from collecting
the token. It does not isolate applications running as the same OS user: those
can usually read the persistent token file directly.

Configure each client once. With the default persistent-token setting, the
config holds no IFC path and no token, only the command to launch the bridge.
After that, start `ifc-console` and use `/file` to open or switch models. Every
connected client uses the model owned by that console session.

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

The default is the bridge for every client when persistent tokens are enabled.
`--transport http` wires the client straight at the HTTP endpoint instead (no
extra process, but the client must start after the console). `--transport
stdio` is an opt-in for a separate client-owned server and becomes the safe
default when persistent tokens are disabled; see [Standalone stdio](#standalone-stdio).

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
claude mcp add --scope user ifc-console -- /path/to/ifc-console bridge
```

`--scope user` makes the connection available in every project, not just the
directory where you added it. The command carries the absolute path to the
executable, because GUI-launched clients do not always inherit your shell PATH.

## Claude Desktop

Open **Settings > Developer > Edit Config** and add the output from
`/connect claude-desktop` to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "ifc-console": {
      "command": "/path/to/ifc-console",
      "args": ["bridge"]
    }
  }
}
```

Save and restart Claude Desktop. No Node.js, no npx, and no token in the file:
the bridge reads this machine's token itself, and Claude Desktop can be started
before or after ifc-console.

## Cursor

For a connection available in every project, open Cursor's global MCP config at
`~/.cursor/mcp.json` and add:

```json
{
  "mcpServers": {
    "ifc-console": {
      "command": "/path/to/ifc-console",
      "args": ["bridge"]
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
      "type": "stdio",
      "command": "/path/to/ifc-console",
      "args": ["bridge"]
    }
  }
}
```

Save, then start or restart the server from VS Code's MCP controls.

## Codex

Add the output from `/connect codex` to `~/.codex/config.toml`:

```toml
[mcp_servers.ifc-console]
command = "/path/to/ifc-console"
args = ["bridge"]
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
| to stop caring whether the console or the client started first | bridge (default) |
| switch IFC files without editing client config | bridge or HTTP |
| several clients using the same loaded model | bridge or HTTP |
| the console mode switch, live feed, and 3D viewer | bridge or HTTP |
| one less process, and you always start the console first | HTTP |
| no separate terminal and one client-owned process | standalone stdio |

!!! warning "Keep the local token private"
    The HTTP snippets (`--transport http`) contain a bearer token. Do not commit
    them to a public repository. If the token leaks, run `ifc-console token
    rotate` and regenerate the client configs once. Bridge snippets carry no
    token: the bridge reads `~/.ifc-console/token` at launch.
