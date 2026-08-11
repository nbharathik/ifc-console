# CLI reference

```
ifc-console [flags]                     interactive console (default)
ifc-console serve --stdio|--http        run without the console
ifc-console bridge                      stdio proxy to a running console
ifc-console check MODEL [...]           validate a model for CI (schema + IDS)
ifc-console run MANIFEST [--plan]       plan or run a versioned workflow
ifc-console jobs <subcommand>            run and inspect durable automation jobs
ifc-console batch <subcommand>           validate or query many IFCs with resume support
ifc-console workflows <subcommand>       print the schema or manage workflow runs
ifc-console transactions <subcommand>    inspect commit/restore recovery journals
ifc-console artifacts <subcommand>       inspect and export durable outputs
ifc-console changes <subcommand>         preview, approve, commit, or restore edits
ifc-console doctor [--file X] [--json]  diagnose the environment
ifc-console mcp-config [...]            print client wiring snippets
ifc-console settings <subcommand>       inspect and edit user settings
ifc-console plugins list|doctor         inspect trusted operation plugins
ifc-console token show|rotate|path      manage the persistent server token
ifc-console recents list|clear          recently opened models
ifc-console sessions list|show|verify|clear  audit-log sessions
ifc-console --version
```

## Run flags

Accepted by the bare command and by `serve`:

| flag | effect |
| ---- | ------ |
| `--file PATH` | load this model at startup (optional; `/file` works any time) |
| `--mode ask\|edit` | session mode (default from settings, normally ask = AI is query-only) |
| `--port N` | HTTP port (default 8383) |
| `--viewer` | enable the built-in 3D web viewer at startup (`/viewer` works any time) |
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

Checks ifc-console, Python, ifcopenshell, mcp, textual, uvicorn, settings
readability, port availability, and whether the bundled viewer assets are
complete. With `--file` it also parses the model and reports schema, product
count, and parse time. `--json` is available for machines. Exit code 0 only if
everything essential is ok.

## jobs and artifacts

```bash
ifc-console jobs validate model.ifc --ids requirements.ids --output-dir reports
ifc-console jobs validate model.ifc --expected-revision HASH:REV --json
ifc-console jobs commit model.ifc sha256:CHANGESET --approval sha256:APPROVAL
ifc-console jobs restore model.ifc sha256:COMMIT --confirm
ifc-console jobs list --json
ifc-console jobs show job-0123456789abcdef
ifc-console jobs cancel job-0123456789abcdef

ifc-console transactions list
ifc-console transactions show txn-0123456789abcdef --json

ifc-console artifacts list
ifc-console artifacts show sha256:DIGEST --json
ifc-console artifacts export sha256:DIGEST report.sarif
ifc-console artifacts pin sha256:DIGEST
ifc-console artifacts unpin sha256:DIGEST
ifc-console artifacts gc --older-than-days 30 --json
ifc-console artifacts gc --older-than-days 30 --apply --confirm
```

`jobs validate` runs schema and optional IDS validation in a restricted worker
process. `jobs commit` and `jobs restore` run the existing ChangeSet workflows
through the same durable supervisor and stream transaction phases. Progress is
written to stderr, so `--json` keeps stdout machine-safe. Validation exits 0
when it passes, 5 when checks complete but findings fail the model, 1 when a
job fails or is cancelled, and 2 for a structured input or policy error.

Every completed validation job publishes checksum-verified JSON and SARIF
artifacts under the IFC-Console home. `--output-dir` exports both. Job records
include the workspace, model revision, source hashes, progress events, worker
controls, result summary, and artifact references.

Commit and restore records additionally expose `phase`, `cancellable`, and
`transaction_id`. Cancellation is accepted during candidate preparation but
rejected after `commit_point`; shutdown uses an awaited cleanup path where
available. A fsynced journal records backup, target, and receipt identities so
restart recovery can either accept the durable receipt or restore verified
bytes. `transactions show` is read-only and is the first diagnostic step for
`TRANSACTION_RECOVERY_REQUIRED`.

Artifact export streams and verifies content instead of loading the complete
file into memory. Garbage collection is reference-aware and is a dry run unless
both `--apply` and `--confirm` are present. Recent artifacts, artifacts named by
retained job records, explicit pins, and transaction history are retained.
Use the JSON plan to review the exact candidate IDs and byte count before an
apply. Transaction artifacts remain protected roots in this release.

## batch validation

```bash
ifc-console batch validate models/*.ifc --concurrency 4 --json
ifc-console batch validate a.ifc b.ifc --ids requirements.ids \
  --failure-policy fail_fast --output-dir reports
ifc-console batch query models/*.ifc --selector IfcWall --format jsonl \
  --field name --field storey --output-dir query-results
ifc-console batch list --json
ifc-console batch show batch-0123456789abcdef
ifc-console batch cancel batch-0123456789abcdef
ifc-console batch resume batch-0123456789abcdef --json
```

`batch validate` captures the path, size, timestamp, and SHA-256 identity of
every IFC and IDS input before scheduling work. It submits at most
`--concurrency` isolated validation jobs at once. `continue` processes every
input; `fail_fast` cancels or skips remaining inputs after the first
operational failure. Validation findings are successful execution with exit
code 5, while worker, source, or batch failures return 1.

Batch state and every child job/artifact reference are persisted under the
IFC-Console home. Cancellation produces a resumable record. Resume first
re-hashes all captured inputs, refuses changed files, verifies completed
artifacts, reuses valid successes, and retries only unfinished children. Each
terminal run creates a content-addressed aggregate JSON manifest. The manifest
references all child reports, so reference-aware artifact cleanup retains the
complete result graph. This release supports validation batches; streaming
query batches use the same captured-source and resume rules. Query output is
written as JSONL or CSV in the isolated child worker and ingested as a file, so
the supervisor does not deserialize the complete result set.

## workflow automation

```bash
ifc-console workflows schema > workflow-v1.schema.json
ifc-console run workflow.yaml --plan --json
ifc-console run workflow.yaml --output-dir reports --json
ifc-console workflows list
ifc-console workflows show workflow-0123456789abcdef
ifc-console workflows watch workflow-0123456789abcdef
ifc-console workflows cancel workflow-0123456789abcdef
ifc-console workflows resume workflow-0123456789abcdef --output-dir reports
```

`run --plan` validates the complete version 1 JSON/YAML DAG, resolves relative
IFC/IDS paths, captures hashes, and prints a stable plan without scheduling any
work. A run persists before it schedules ready validation or selector-query
steps through the bounded batch service. Step and workflow concurrency are
independent, and dependencies, `continue`/`fail_fast`, output names, progress,
cancellation, dead-process recovery, and resume are durable.

Resume refuses changed sources and reuses only complete steps whose batch and
artifacts still verify. A terminal workflow manifest references every step
manifest and result. Exit code 5 represents validation findings; operational
failure returns 1. See [Automation workflows](workflows.md) for the schema and
security boundaries.

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

Pass `--create-missing` to explicitly preview a missing occurrence property or
property set. Scalar values infer `IfcLabel`, `IfcBoolean`, `IfcInteger`, or
`IfcReal`; use `--nominal-type IfcLengthMeasure` (or another schema value type)
when the IFC domain type matters. Null creation is refused because it cannot
persist a nominal IFC type. Existing values keep their current IFC type.

Classification assignment uses the same ChangeSet and approval path:

```bash
ifc-console changes classify model.ifc --global-id 2abc... \
  --system "Company Classification" --identification WALL-EXT \
  --name "External wall" --json
```

The preview reuses an exact system/reference when present, creates missing
classification metadata only in the candidate, and rejects duplicate or
already assigned direct occurrence references.

`approve` is a separate caller action and records the approving identity.
`commit` requires that approval, rechecks the source revision and checksum,
verifies a temporary IFC, creates a durable backup artifact, then replaces and
reloads the source. `restore --confirm` succeeds only while the target still
matches the recorded committed checksum. These commands modify the source IFC
in place. Candidate, backup, replacement, and rollback copying is streaming and
checksum verified. The `commit` and `restore` convenience commands submit and
wait for the same durable records shown by `jobs list`; use `jobs commit` or
`jobs restore` to see every phase while it runs. Use version control or a copied
model when evaluating the workflow.

The structured editor updates or creates occurrence-level
`IfcPropertySingleValue` values and assigns direct occurrence classification
references. It does not edit inherited type properties, remove classifications,
or operate on a dirty in-memory model.

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
endpoint and prints a `<TOKEN>` placeholder by default. Set
`server.token_in_config_snippets=true` in user settings only when the destination
is trusted and a paste-ready HTTP configuration is required. `stdio` is a
separate, client-owned server. When `server.persistent_token` is false,
`mcp-config` defaults to `stdio` and refuses `--transport bridge`.

## bridge

```bash
ifc-console bridge [--port N] [--token T]
```

A stdio MCP proxy to the console on this machine, meant to be launched by an MCP
client rather than by hand. It always starts, so the client always connects, and
it forwards to the console as soon as one is running; while there is none, tool
calls come back as `CONSOLE_NOT_RUNNING` with a hint. The token defaults to this
machine's stored one when persistence is enabled; otherwise pass `--token` from
the running console. Before sending that token, the bridge challenges the
loopback listener with a fresh nonce and verifies a port-bound HMAC identity
proof. A different application occupying the port does not receive the token.

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
ifc-console sessions verify <id>       # verify sequence and SHA-256 chain
ifc-console sessions clear             # remove all but the active session
```

## plugins

```bash
ifc-console plugins list [--json]       # metadata only; imports no plugin code
ifc-console plugins doctor [--json]     # load and validate allowed plugins
```

Plugins are disabled and deny-by-default. See [Operation plugins](plugins.md)
for the package contract, allowlist setup, and security boundary.

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
