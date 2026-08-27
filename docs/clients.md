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

## Visual inspection from Codex or Claude Code

Use the recommended `bridge` connection for viewer work. It reaches the same
HTTP session as the terminal and browser; a standalone `stdio` server has no
web surface.

An external agent can complete the workflow itself:

1. Call `orient`. If no model is loaded, call `find_files` or `list_ifc_files`,
   then `open_ifc_file`.
2. Call `open_viewer(wait_for_connection_s=10)`. It opens the local tokenized
   viewer without putting the token in the tool result.
3. Call `control_viewer(action="context")`, then orient, section, isolate,
   focus, or measure the scene.
4. Call `highlight_elements` or `apply_color_theme` when the user should see
   the evidence.
5. Call `get_viewer_screenshot`; it returns standard MCP image content plus a
   short text note. A vision-capable MCP host can inspect the rendered IFC
   directly.

The viewer tools remain in `tools/list` while the viewer is off. This is
deliberate: clients may cache the list, and the agent must still be able to
call `open_viewer` and continue without reconnecting.

## Which agent capabilities are exported

The boundary is explicit:

| capability | MCP exposure |
| ---------- | ------------ |
| registered IFC operations and trusted operation-plugin tools | shared by MCP, built-in chat, the agent runtime, and the SDK |
| viewer selection, control, measurement, highlighting, themes, and screenshots | shared MCP tools; require bridge/HTTP and a connected tab |
| agent-only `FunctionToolSource` or imported `McpToolSource` tools | private to the `Toolset` that owns them unless deliberately promoted to an operation plugin |
| blocks, delegation, thread memory, response validation, and approval handlers | orchestration behavior, not remotely callable tools |
| `/mode`, `/save`, credentials, settings, and approval decisions | human/host controls; intentionally never exposed as AI tools |

This avoids turning the console into an unreviewed proxy for every tool another
agent can reach. Use an operation plugin when a trusted application tool should
become part of the shared MCP surface; it then receives the same schema,
capability, policy, audit, and result-envelope handling as built-in operations.
