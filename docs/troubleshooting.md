---
description: Fixes for connection, port, viewer, and sandbox problems.
---

# Troubleshooting

Start here:

```bash
ifc-console doctor --file your-model.ifc
```

It checks Python, dependencies, settings, the port, viewer assets, and model
parsing. Add `--json` for a bug report.

## Client connections

**The client does not see ifc-console.**
Run `/connect <client>` again and paste the printed setup; copying is not a
connection check. Then ask the client to report the active model and watch the
console feed for its calls. Replace older direct URL, `npx`, or `mcp-remote`
entries with the bridge setup.

**401 unauthorized.**
The client has a stale token. Run `/connect <client>` again after a token
rotation, after deleting `~/.ifc-console`, or after moving a config between
machines.

**The client sees an old model.**
Compare the model it reports with `/status`. A second `ifc-console` process is
a separate session; generate the setup from the one you mean. A stdio entry
with `--file` owns its own model; replace it with the bridge setup so `/file`
controls what every client sees.

## Server

**Port 8383 is in use.**
`ifc-console doctor` names the listener when it can. Use `/port 8390` in a
running session or start with `--port 8390`, then regenerate client setups.

**The console needs a terminal.**
Use `--no-tui` for a headless HTTP server or `serve --stdio` for a
client-owned process. On Windows, prefer Windows Terminal.

**Windows firewall prompt.**
Deny external access. Loopback on `127.0.0.1` keeps working.

## Model and code

| Problem | Fix |
| :--- | :--- |
| changes are blocked | run `/mode edit`, then `/save` or `/reload` when done |
| `MODEL_BUSY` or a paused session | a call timed out; run `/reload` |
| tainted session | guarded code changed memory unexpectedly; run `/reload` |
| `sandboxed: false` | run `/sandbox` to see why; Python 3.10 and 3.11 have no sandbox |
| first code run is slow | the sandbox loads a second copy; set `sandbox.warm_on_load=true` |

**Where did my changes go?**
Run `/status`. It shows unsaved changes, the file the next `/save` writes, and
the original path when a working copy is in use.

## Viewer

**Assets are missing.**
Reinstall: `uv tool install --force ifc-console` or
`pip install --force-reinstall ifc-console`. The viewer ships in the main
package, so missing assets mean a broken install.

**Model is too large.**
Raise `viewer.max_model_mb` only if the browser has the memory:
`/settings viewer.max_model_mb 500`.

**Unauthorized.**
Get a fresh link with `/viewer`. Paste the whole URL including the part after
`#`; a URL copied from the address bar later no longer has it.

**`/viewer vscode` did not open a tab.**
It prepares a link; it does not launch a tab. Ctrl+click the link or use
**Browser: Open Integrated Browser** and paste it.

## Agent workspace

**Chat cannot reach a provider.**
Confirm `ifc-console-agents` is installed, then check the key, model ID, and
base URL. Local servers need an OpenAI-compatible `/v1` URL, and
`chat.local_only=true` refuses remote URLs on purpose. The console, MCP
server, SDK, and viewer keep working if the extension fails to load.

## Logs

- Live activity: the console feed.
- Application log: `~/.ifc-console/logs/ifc-console.log`.
- Audit: `/audit` or `ifc-console sessions show <id>`.

For a bug report include `ifc-console doctor --json` and the relevant log
lines. Remove private paths and never upload a confidential model.
