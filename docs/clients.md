# Connecting clients

Connect each AI client once. After that, every client uses the model currently
open in the shared ifc-console session.

## Quick setup

1. Start `ifc-console`.
2. Run `/connect <client>`, for example `/connect codex`.
3. Paste the copied configuration into the location shown by the console.
4. Restart or reload the client once.

The normal daily flow is then `ifc-console`, `/file`, and chat. Changing the
model does not require another client setup.

You can generate the same configuration without opening the console:

```bash
ifc-console mcp-config --client codex
```

Accepted names are `claude-code`, `claude-desktop`, `cursor`, `vscode`, and
`codex`. Use `/connect all` to display every setup or `/copy <client>` to copy
one again.

## Client instructions

### Claude Code

Run the command printed by `/connect claude-code`:

```bash
claude mcp add --scope user ifc-console -- /path/to/ifc-console bridge
```

`--scope user` makes the connection available in every project. The generated
command uses the absolute executable path so it also works when Claude Code
does not inherit your shell `PATH`.

### Claude Desktop

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

Save the file and restart Claude Desktop.

### Cursor

Open the global MCP configuration at `~/.cursor/mcp.json` and add:

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

Restart Cursor or reload its MCP servers.

### VS Code

Run **MCP: Open User Configuration** from the Command Palette and add:

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

Save the file, then start or restart the server from VS Code's MCP controls.

### Codex

Add the output from `/connect codex` to `~/.codex/config.toml`:

```toml
[mcp_servers.ifc-console]
command = "/path/to/ifc-console"
args = ["bridge"]
```

Restart Codex after changing the configuration. The desktop app, CLI, and IDE
extension share this file.

If an older entry contains `bearer_token_env_var`, replace it with the current
bridge output. The bridge does not require a token in the client configuration.

## How the default connection works

The client starts a small stdio bridge, which forwards requests to the console
over localhost:

```text
AI client -> ifc-console bridge -> running console -> active model
```

This design has three useful properties:

- the client and console can start in either order;
- several clients can share one model and one terminal-owned mode switch;
- the client configuration contains no IFC path or bearer token.

The bearer token is stored in `~/.ifc-console/token` and normally persists on
one machine. Rotate it with `ifc-console token rotate`. You only need to
regenerate client setups after changing the port, rotating the token, or
disabling persistent tokens.

Before sending the token, the bridge verifies that the listener on the chosen
port can prove it is the matching ifc-console process. This protects against an
unrelated program that happens to occupy the port. Programs running as the same
OS user can usually read the token file, so the bridge is not an isolation
boundary between applications owned by that user.

## Alternative transports

The generated bridge setup is recommended for interactive use. Two alternatives
are available:

| transport | use it when | limitation |
| --------- | ----------- | ---------- |
| `bridge` | you want the shared console and flexible start order | starts one small proxy per client |
| `http` | the console always starts first and you want no proxy | direct configs may contain a token |
| `stdio` | one client should own a separate server process | it does not share `/file`, the console feed, or viewer |

Generate a specific transport with:

```bash
ifc-console mcp-config --client <client> --transport http
ifc-console mcp-config --client <client> --transport stdio --file model.ifc
```

!!! warning "Keep HTTP tokens private"
    A direct HTTP configuration may contain a bearer token. Do not commit it.
    If it leaks, run `ifc-console token rotate` and regenerate the affected
    configuration.

For connection failures, see [Troubleshooting](troubleshooting.md).
