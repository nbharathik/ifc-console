# CLI

Run `ifc-console` with no subcommand for the interactive console. Use
subcommands for servers, CI, automation, and administration.

```text
ifc-console                         interactive console
ifc-console serve ...               headless MCP server
ifc-console doctor ...              diagnose setup
ifc-console check MODEL ...         validate one model
ifc-console run MANIFEST ...        plan or run a workflow
ifc-console jobs|batch|workflows ... durable automation
ifc-console changes ...             preview and commit structured edits
ifc-console settings|token|sessions ... administration
```

Run `ifc-console <command> --help` for the exact options supported by your
installed version.

## Common commands

| goal | command |
| ---- | ------- |
| open the console | `ifc-console` |
| diagnose setup | `ifc-console doctor --file model.ifc` |
| validate for CI | `ifc-console check model.ifc` |
| print client setup | `ifc-console mcp-config --client codex` |
| inspect settings | `ifc-console settings list --sources` |
| plan a workflow | `ifc-console run workflow.yaml --plan` |

## Startup flags

These flags work with the interactive command and `serve` where applicable:

| flag | effect |
| ---- | ------ |
| `--file PATH` | open a model at startup |
| `--mode ask\|edit` | set the starting mode |
| `--port N` | set the HTTP port; default `8383` |
| `--viewer` | enable the optional browser viewer |
| `--chat` | enable the browser chat panel |
| `--allow-dir PATH` | add a readable root; repeatable |
| `--log-level LEVEL` | `debug`, `info`, `warning`, or `error` |
| `--no-tui` | run a headless HTTP server |

## Headless servers

```bash
ifc-console serve --stdio --file model.ifc --mode ask
ifc-console serve --http --file model.ifc --viewer
```

- `--stdio` is a private client-owned MCP process with logs on stderr.
- `--http` is the same server used by `--no-tui`; it prints its URL and token.

For normal interactive use, keep the console and connect clients through the
default bridge.

## Diagnose and validate

### `doctor`

```bash
ifc-console doctor
ifc-console doctor --file model.ifc --json
```

Checks Python, dependencies, settings, port availability, viewer assets, and
optionally model parsing. A core-only install reports viewer assets as
`optional`, not failed.

### `check`

```bash
ifc-console check model.ifc
ifc-console check model.ifc --ids requirements.ids \
  --format sarif --output report.sarif
```

Runs schema validation and optional IDS checks. Formats are `text`, `json`,
`sarif`, and `junit`. Add `--express-rules` for slower EXPRESS where-rules.

## Client setup

```bash
ifc-console mcp-config --client claude-code|claude-desktop|cursor|vscode|codex
```

The default transport is `bridge`: the client starts a small stdio proxy to the
shared console. Alternatives are `--transport http` and `--transport stdio`.
Use `/connect <client>` for the same output inside the console.

`ifc-console bridge [--port N]` is normally launched by the client, not by
hand. It stays connected while the shared console starts or restarts.

```text
client -> bridge -> running console -> active model
```

See [Connecting clients](clients.md).

## Durable automation

```text
job          one isolated validation, commit, or restore
batch        the same read-only operation across many IFC files
workflow     validation and query batches connected by dependencies
artifact     checksum-verified durable output
```

### Jobs and artifacts

```bash
ifc-console jobs validate model.ifc --ids requirements.ids --output-dir reports
ifc-console jobs list
ifc-console jobs show job-0123456789abcdef
ifc-console jobs cancel job-0123456789abcdef

ifc-console artifacts list
ifc-console artifacts export sha256:DIGEST report.sarif
ifc-console artifacts pin sha256:DIGEST
ifc-console artifacts gc --older-than-days 30 --json
ifc-console artifacts gc --older-than-days 30 --apply --confirm
```

Jobs store progress and verified JSON/SARIF outputs under the ifc-console home.
Artifact collection is a dry run unless both `--apply` and `--confirm` are
present. Recent, referenced, pinned, and transaction artifacts are protected.

Use `transactions list|show` to inspect commit and restore recovery journals.

### Batches

```bash
ifc-console batch validate models/*.ifc --concurrency 4
ifc-console batch query models/*.ifc --selector IfcWall --format jsonl
ifc-console batch list
ifc-console batch resume batch-0123456789abcdef
```

Batches capture input hashes before starting. Resume refuses changed sources,
reuses verified completed work, and retries unfinished children. Validation
findings return exit code 5; execution failures return 1.

### Workflows

```bash
ifc-console workflows schema > workflow-v1.schema.json
ifc-console run workflow.yaml --plan --json
ifc-console run workflow.yaml --output-dir reports
ifc-console workflows list
ifc-console workflows watch workflow-0123456789abcdef
ifc-console workflows resume workflow-0123456789abcdef
```

`--plan` validates the graph, resolves and hashes inputs, and schedules nothing.
Runs and resumes reject changed sources. See [Automation workflows](workflows.md).

## Structured changes

Structured edits follow one path:

```text
preview -> inspect -> approve -> commit -> optional restore
```

```bash
ifc-console changes preview model.ifc \
  --global-id 2abc... --pset Pset_WallCommon \
  --property FireRating --value F60

ifc-console changes approve sha256:CHANGESET --by bim-manager
ifc-console changes commit model.ifc sha256:CHANGESET \
  --approval sha256:APPROVAL
ifc-console changes restore model.ifc sha256:COMMIT --confirm
```

Classification uses the same flow:

```bash
ifc-console changes classify model.ifc --global-id 2abc... \
  --system "Company Classification" --identification WALL-EXT \
  --name "External wall"
```

Preview never changes the model. Commit rechecks the source, validates a
candidate, creates a backup, and replaces the file under a lock. Test the flow
on a copied or version-controlled model.

## Administration

| command | use |
| ------- | --- |
| `settings list|get|set|unset|path` | inspect or change user settings |
| `token show|rotate|path` | manage the persistent local bearer token |
| `recents list|clear` | manage recently opened models |
| `sessions list|show|verify|clear` | inspect and verify audit sessions |
| `plugins list|doctor` | inspect trusted operation plugins |
| `knowledge build|status|search` | manage the offline reference index |

Examples:

```bash
ifc-console settings set sandbox.mode strict
ifc-console token rotate
ifc-console sessions verify SESSION_ID
ifc-console plugins doctor --json
```

## Exit codes

| code | meaning |
| ---- | ------- |
| `0` | success |
| `1` | runtime or automation failure |
| `2` | environment, dependency, or policy problem |
| `3` | invalid command usage or no terminal |
| `4` | file missing or unreadable |
| `5` | validation ran but findings failed the check |

## Environment variables

- `IFC_CONSOLE_HOME` changes the settings, logs, jobs, and artifacts directory.
- `IFC_CONSOLE_<SECTION>_<KEY>` overrides a setting, for example
  `IFC_CONSOLE_SERVER_PORT=9000` or `IFC_CONSOLE_MODE_DEFAULT=ask`.

Values are parsed as JSON when possible.
