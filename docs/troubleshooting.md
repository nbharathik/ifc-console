# Troubleshooting

First stop, always:

```bash
ifc-console doctor --file your-model.ifc
```

It checks the interpreter, every dependency, the bundled viewer assets, settings
readability, port availability, and (with `--file`) parses your model.

## Common issues

### The client gets 401 unauthorized

The token in your client config does not match the server's. Since the token is
persistent per machine, this normally happens only after `ifc-console token
rotate`, after deleting `~/.ifc-console`, or when a config was copied from another
machine. Fix: `ifc-console mcp-config --client ...` (or `/connect all` in the
console) and re-add the client once. stdio setups have no token.

For Codex, `bearer_token_env_var = "IFC_CONSOLE_MCP_TOKEN"` stores the name of an
environment variable, not its value. If that variable was missing when Codex
started, its tools will not load. Replace the entry with the static
`http_headers` output from `/connect codex`, or define the variable before
starting Codex and restart it.

### Claude Desktop times out on its first launch

Symptom: the new ifc-console entry shows as failed or disconnected, its MCP log
has a single `initialize` line followed by roughly 60 seconds of silence and a
disconnect, and on Windows the log may end with "The batch file cannot be
found."

Claude Desktop launches the `mcp-remote` bridge through `npx` and allows a
starting server 60 seconds to answer. On the very first launch npx still has to
download `mcp-remote` from the npm registry, and when that download takes
longer than the limit, Claude Desktop gives up while npx is still installing.
Nothing is misconfigured.

Fix: warm the npx cache once in any terminal, then fully quit Claude Desktop
(also from the system tray on Windows) and reopen it while the ifc-console
console is running:

```bash
npx -y mcp-remote@0.1.38 --help
```

Use the same pinned version as your config entry; npx caches each version spec
separately. Every launch after a warm cache starts instantly.

### The client keeps opening an old IFC file

The client is probably a standalone stdio server with `--file` in its arguments.
That is a separate process and does not follow the console. Replace it with the
default HTTP output from `/connect <client>`. The HTTP setup has no model path;
use `/file` in the console to switch the model for every connected client.

### Port 8383 already in use

ifc-console refuses to start on an occupied port and tells you **who owns it**.
`ifc-console doctor` reports the same:

- **"your running ifc-console session (same token)"**: you already have a session
  up; your clients are talking to it. For a second parallel session use
  `--port 8390`, and wire the second port up once too. Per project, drop a
  `.ifc-console/settings.json` with `{"server": {"port": 8390}}` next to the models
  and that folder always uses its own port.
- **"an ifc-console session with a different token"**: probably a different
  `IFC_CONSOLE_HOME`; check `ifc-console token show` on both sides.
- **"an application that is not ifc-console"**: some other program owns your
  configured port. Do not leave this as is: MCP clients pointing at that URL
  would send their requests, bearer token included, to that program. Move
  permanently with `ifc-console settings set server.port 8390`, re-add clients
  (`ifc-console mcp-config`), and consider `ifc-console token rotate` if the foreign
  app may have seen requests.

Inside a running console, `/port 8390` moves the live server.

### The console does not start ("needs a terminal")

You are piping output or running without a TTY. Use `--no-tui` for a headless
daemon or `serve --stdio` for client-managed mode. On Windows, prefer Windows
Terminal.

### "session paused" / MODEL_BUSY after a long code run

A run exceeded `exec.timeout_seconds`, and CPython cannot kill a C call
mid-flight. The session protects itself; `/reload` swaps in a fresh worker and
reloads the file from disk. Raise the timeout if your model genuinely needs
longer runs.

### "session tainted" in the status bar

Guarded (mutation-locked) code managed to mutate the in-memory model. The
classifier missed it and the canary caught it. Nothing on disk changed.
`/reload` restores a pristine copy; the audit log records what ran.

### The viewer shows "model too large"

`viewer.max_model_mb` (default 200) refused the download.
`/settings viewer.max_model_mb 500` if you want to try anyway. Parsing very
large models in the browser takes memory and patience.

### The viewer tab shows "unauthorized"

Open it through `/viewer` (the URL carries the token). A stale tab from a
previous run holds the old token: close it and `/viewer` again.

### Mutations are blocked and the LLM keeps apologizing

You are in `ask` mode, the default on purpose: the AI can query but never change
the model. `/mode edit` (with a y/n confirm) lets it make changes; `/mode ask`
locks the model again. For per-change prompts, use your AI client's own
permission settings.

### Windows: firewall prompt on startup

ifc-console binds to 127.0.0.1 only, which normally avoids firewall prompts. If one
appears anyway, it is safe to deny external access; loopback keeps working.

### Where are the logs?

- Console feed: live view, `ctrl+l` clears.
- File log: `~/.ifc-console/logs/ifc-console.log` (rotating).
- Audit records: `/audit` in the console or `ifc-console sessions show <id>`.

## Reporting bugs

`ifc-console doctor --json` output plus the relevant audit-log lines make a good
bug report skeleton. Please strip file paths you consider private before
sharing.
