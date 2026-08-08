# CLI reference

```
ifc-console [flags]                     interactive console (default)
ifc-console serve --stdio|--http        run without the console
ifc-console bridge                      stdio proxy to a running console
ifc-console check MODEL [...]           validate a model for CI (schema + IDS)
ifc-console jobs <subcommand>            run and inspect durable automation jobs
ifc-console artifacts <subcommand>       inspect and export durable outputs
ifc-console changes <subcommand>         preview, approve, commit, or restore edits
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
| `--no-tui` | headless HTTP daemon instead of the console (bare command only) |

## serve

```bash
ifc-console serve --stdio --file model.ifc --mode ask
ifc-console serve --http --file model.ifc --viewer
```

`--stdio` speaks MCP on stdin/stdout for client-managed configs; logs go to
stderr and the log file only. `--http` equals `--no-tui`: it prints the
endpoint, token, and (with `--viewer`) the viewer URL, then serves until
Ctrl+C.

## check

```bash
ifc-console check model.ifc
ifc-console check model.ifc --ids requirements.ids --format sarif --output report.sarif
```

One-shot validation for scripts and CI: schema validation (add
`--express-rules` for the slow EXPRESS where-rules) plus any number of
`--ids FILE` checks. `--format text|json|sarif|junit`; SARIF uploads straight
to GitHub code scanning, JUnit to anything that ingests test reports.
Exit code 0 when everything passes, **5** when the model fails a check, 4 for
an unreadable file, 2 when an IDS file is given but the optional `ifctester`
package is missing.

## doctor

Checks ifc-console, Python, ifcopenshell, mcp, textual, uvicorn, the bundled viewer
assets, settings readability, and port availability. With `--file` it also
parses the model and reports schema, product count, and parse time. `--json` for
machines. Exit code 0 only if everything essential is ok.

## jobs and artifacts

```bash
ifc-console jobs validate model.ifc --ids requirements.ids --output-dir reports
ifc-console jobs validate model.ifc --expected-revision HASH:REV --json
ifc-console jobs list --json
ifc-console jobs show job-0123456789abcdef
ifc-console jobs cancel job-0123456789abcdef

ifc-console artifacts list
ifc-console artifacts show sha256:DIGEST --json
ifc-console artifacts export sha256:DIGEST report.sarif
ifc-console artifacts pin sha256:DIGEST
ifc-console artifacts unpin sha256:DIGEST
ifc-console artifacts gc --older-than-days 30 --json
ifc-console artifacts gc --older-than-days 30 --apply --confirm
```

`jobs validate` runs schema and optional IDS validation in a restricted worker
process. Progress is written to stderr, so `--json` keeps stdout machine-safe.
It exits 0 when validation passes, 5 when checks complete but findings fail the
model, 1 when the job fails or is cancelled, and 2 for a structured input or
policy error.

Every completed validation job publishes checksum-verified JSON and SARIF
artifacts under the IFC-Console home. `--output-dir` exports both. Job records
include the workspace, model revision, source hashes, progress events, worker
controls, result summary, and artifact references.

Artifact export streams and verifies content instead of loading the complete
file into memory. Garbage collection is reference-aware and is a dry run unless
both `--apply` and `--confirm` are present. Recent artifacts, artifacts named by
retained job records, explicit pins, and transaction history are retained.
Use the JSON plan to review the exact candidate IDs and byte count before an
apply. Transaction artifacts remain protected roots in this release.

## changes

```bash
ifc-console changes preview model.ifc \
  --global-id 2abc... --pset Pset_WallCommon --property FireRating \
  --value F60 --json

ifc-console changes show sha256:CHANGESET_DIGEST
ifc-console changes approve sha256:CHANGESET_DIGEST --by bim-manager --json
ifc-console changes commit model.ifc sha256:CHANGESET_DIGEST \
  --approval sha256:APPROVAL_DIGEST --json
ifc-console changes receipt sha256:COMMIT_DIGEST --json
ifc-console changes restore model.ifc sha256:COMMIT_DIGEST --confirm --json
```

`preview` never changes the model. It runs in a restricted worker and creates
a content-addressed ChangeSet containing exact before and after values. Use
`--value` for a string or `--value-json` for a JSON number, boolean, string, or
null. Repeat `--global-id` to update the same existing property on several
occurrences.

`approve` is a separate caller action and records the approving identity.
`commit` requires that approval, rechecks the source revision and checksum,
verifies a temporary IFC, creates a durable backup artifact, then replaces and
reloads the source. `restore --confirm` succeeds only while the target still
matches the recorded committed checksum. These commands modify the source IFC
in place. Candidate, backup, replacement, and rollback copying is streaming and
checksum verified. Use version control or a copied model when evaluating the
workflow.

The initial structured editor updates existing occurrence-level
`IfcPropertySingleValue` values. It does not create property sets, edit type
properties, or operate on a dirty in-memory model.

## mcp-config

```bash
ifc-console mcp-config --client claude-code|claude-desktop|cursor|vscode|codex \
                    [--transport bridge|http|stdio] [--file X] [--mode M] [--port N]
```

Prints a paste-ready snippet for the chosen client. With persistent tokens, it
works without a running server and stays valid across restarts. (`/connect` in
the console prints the same thing.)

With the default persistent-token setting, `bridge` is the default transport.
It launches `ifc-console bridge`, so the client can start before the console,
and reads the machine token itself. `http` points the client straight at the
endpoint and embeds the machine token. `stdio` is a separate, client-owned
server. When `server.persistent_token` is false, `mcp-config` defaults to
`stdio` and refuses `--transport bridge`; only `/connect` inside the running
console can hand out a bridge snippet then, embedding the current run's token.

## bridge

```bash
ifc-console bridge [--port N] [--token T]
```

A stdio MCP proxy to the console on this machine, meant to be launched by an MCP
client rather than by hand. It always starts, so the client always connects, and
it forwards to the console as soon as one is running; while there is none, tool
calls come back as `CONSOLE_NOT_RUNNING` with a hint. The token defaults to this
machine's stored one when persistence is enabled; otherwise pass `--token` from
the running console.

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
| 5 | `check` found validation failures |

## Environment variables

- `IFC_CONSOLE_HOME`: relocate `~/.ifc-console` (settings, logs, jobs, artifacts).
- `IFC_CONSOLE_<SECTION>_<KEY>`: override any setting, e.g.
  `IFC_CONSOLE_SERVER_PORT=9000`, `IFC_CONSOLE_MODE_DEFAULT=ask`. Values parse as JSON
  when possible (`true`, `42`, `"text"`).
