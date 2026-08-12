# Troubleshooting

Start with:

```bash
ifc-console doctor --file your-model.ifc
```

It checks Python, dependencies, settings, the port, viewer assets, and model
parsing. Add `--json` for machine-readable output.

## Client connections

### Client is disabled or not connected

Regenerate the current bridge setup and restart the client once:

```bash
ifc-console mcp-config --client <client>
```

The entry should run `ifc-console ... bridge`. Replace older direct URL, `npx`,
or `mcp-remote` setups. The bridge allows the client and console to start in
either order.

Regenerate setup after changing `server.port`. If persistent tokens are
disabled, use direct HTTP or standalone stdio instead.

### 401 unauthorized

The client has a stale token. Run `/connect <client>` again after token
rotation, deleting `~/.ifc-console`, or moving a config between machines.

For Codex, `bearer_token_env_var` names an environment variable; it is not the
token value. Replacing the old entry with the current bridge setup is simpler.

### Client uses an old model

It is probably a standalone stdio server with `--file` in its configuration.
Replace it with the default bridge setup. Then `/file` controls the shared
model.

## Server and terminal

### Port 8383 is in use

`ifc-console doctor` identifies the listener when possible.

- Existing ifc-console: use it or start another session with `--port 8390`.
- Other application: set another port, then regenerate client configs.
- Different ifc-console token: check whether processes use different
  `IFC_CONSOLE_HOME` directories.

Use `/port 8390` to move a running session. Rotate the token if an old direct
HTTP client may have sent it to an untrusted listener.

### Console needs a terminal

Use `--no-tui` for headless HTTP or `serve --stdio` for a client-owned process.
On Windows, prefer Windows Terminal.

### Windows firewall prompt

Deny external access. Loopback on `127.0.0.1` continues to work.

## Model and code

| problem | fix |
| ------- | --- |
| mutations are blocked | review the change, run `/mode edit`, then `/save` or `/reload` |
| `MODEL_BUSY` or paused session | an in-process call timed out; run `/reload` |
| tainted session | guarded code changed memory unexpectedly; run `/reload` |
| `sandboxed: false` | run `/sandbox` to see why |
| first code run is slow | sandbox startup loads a second model copy; optionally enable `sandbox.warm_on_load` |

Common sandbox fallbacks are Python 3.10 or 3.11, unsaved changes, mutating
code, a model over `sandbox.max_model_mb`, or worker startup failure.
Save/reload, upgrade Python where applicable, try `/sandbox restart`, or use
`sandbox.mode=strict` to refuse fallback.

## Viewer and chat

### Assets are missing

```bash
uv tool install "ifc-console[viewer]" --force
# or: pip install --force-reinstall "ifc-console[viewer]"
```

Restart ifc-console. `doctor` reports `optional` for an intentional core-only
install and `ok` when assets are present.

### Model is too large

Raise `viewer.max_model_mb` only if the browser has enough memory:

```text
/settings viewer.max_model_mb 500
```

### Viewer says unauthorized

Close the stale tab and run `/viewer` again.

### Chat cannot reach a provider

Check the key, model ID, and base URL. Local servers need an OpenAI-compatible
`/v1` URL. `chat.local_only=true` intentionally refuses remote URLs.

## Logs and bug reports

- Live activity: console feed.
- Application log: `~/.ifc-console/logs/ifc-console.log`.
- Audit: `/audit` or `ifc-console sessions show <id>`.

Include `ifc-console doctor --json` and relevant log lines in a bug report.
Remove private paths and never upload a confidential model.
