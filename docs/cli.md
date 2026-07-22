# CLI reference

```
ifc-console [flags]                     interactive console (default)
ifc-console serve --stdio|--http        run without the console
ifc-console doctor [--file X] [--json]  diagnose the environment
ifc-console mcp-config [...]            print client wiring snippets
ifc-console settings <subcommand>       inspect and edit user settings
ifc-console token show|rotate|path      manage the persistent server token
ifc-console recents list|clear          recently opened models
ifc-console sessions list|show|clear    audit-log sessions
ifc-console --version
```

## Run flags

Accepted by the bare command and by `serve`:

| flag | effect |
| ---- | ------ |
| `--file PATH` | load this model at startup (optional; `/file` works any time) |
| `--mode ask\|edit` | session mode (default from settings, normally ask = AI is query-only) |
| `--port N` | HTTP port (default 8383) |
| `--viewer` | enable the 3D web viewer at startup (`/viewer` works any time) |
| `--allow-dir PATH` | extra directory the LLM may open/save models in (repeatable) |
| `--log-level debug\|info\|warning\|error` | log verbosity |
| `--no-tui` | headless HTTP daemon instead of the console |

## serve

```bash
ifc-console serve --stdio --file model.ifc --mode ask
ifc-console serve --http --file model.ifc --viewer
```

`--stdio` speaks MCP on stdin/stdout for client-managed configs; logs go to
stderr and the log file only. `--http` equals `--no-tui`: it prints the
endpoint, token, and (with `--viewer`) the viewer URL, then serves until
Ctrl+C.

## doctor

Checks ifc-console, Python, ifcopenshell, mcp, textual, uvicorn, the bundled viewer
assets, settings readability, and port availability. With `--file` it also
parses the model and reports schema, product count, and parse time. `--json` for
machines. Exit code 0 only if everything essential is ok.

## mcp-config

```bash
ifc-console mcp-config --client claude-code|claude-desktop|cursor|vscode|codex \
                    [--transport http|stdio] [--file X] [--mode M] [--port N]
```

Prints a paste-ready snippet for the chosen client, including the machine's
persistent token. It works without a running server and stays valid across
restarts. (`/connect` in the console prints the same thing.)

## token

```bash
ifc-console token show      # print the persistent bearer token
ifc-console token rotate    # new token; existing client configs must be re-added
ifc-console token path      # where it is stored (~/.ifc-console/token)
```

The token is created on first use and reused by every run, which makes client
config a one-time step. With `server.persistent_token false` each run generates
its own token instead, and `token show` explains that.

## settings

```bash
ifc-console settings list [--sources] [--json]   # every key, optionally with provenance
ifc-console settings get exec.timeout_seconds
ifc-console settings set viewer.max_model_mb 400
ifc-console settings unset viewer.max_model_mb
ifc-console settings path                        # where the user file lives
```

## recents and sessions

```bash
ifc-console recents list [--json]
ifc-console recents clear
ifc-console sessions list [--json]     # audit sessions, newest first
ifc-console sessions show <id>         # dump one session's JSONL records
ifc-console sessions clear             # remove all but the active session
```

## Exit codes

| code | meaning |
| ---- | ------- |
| 0 | ok |
| 1 | runtime error |
| 2 | environment problem (doctor failures, missing deps) |
| 3 | bad usage, unknown setting, or no TTY for the console |
| 4 | file not found or unparseable |

## Environment variables

- `IFC_CONSOLE_HOME`: relocate `~/.ifc-console` (settings, logs, backups, sessions).
- `IFC_CONSOLE_<SECTION>_<KEY>`: override any setting, e.g.
  `IFC_CONSOLE_SERVER_PORT=9000`, `IFC_CONSOLE_MODE_DEFAULT=ask`. Values parse as JSON
  when possible (`true`, `42`, `"text"`).
