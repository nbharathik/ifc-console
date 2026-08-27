# Settings

Defaults are safe for most users. Use settings when you need a different port,
larger model limit, stricter sandbox, or optional feature behavior.

## View and change settings

From the shell:

```bash
ifc-console settings list --sources
ifc-console settings get sandbox.mode
ifc-console settings set sandbox.mode strict
ifc-console settings unset sandbox.mode
```

From the running console:

```text
/settings
/settings sandbox.mode strict
```

Values are validated before they are saved. `--sources` shows which layer set
each effective value.

## Files and precedence

User data lives under `~/.ifc-console/`, or under `IFC_CONSOLE_HOME` when that
environment variable is set:

```text
~/.ifc-console/
  settings.json     settings changed by the user
  token             persistent local server token
  recents.json      recently opened models
  backups/          timestamped copies made before overwrites
  artifacts/        reports, ChangeSets, backups, and receipts
  jobs/             durable job records
  transactions/     transaction workspaces and journals
  sessions/<id>/    per-session audit records
  logs/             rotating application log
```

Settings are applied in this order, with the last value winning:

```text
defaults < user file < project file < project local file < environment < CLI
```

A project may contain `.ifc-console/settings.json` and a git-ignored
`.ifc-console/settings.local.json`.

!!! warning "Project settings are intentionally limited"
    Project files may set only `tui.theme`. They cannot change permissions,
    network settings, paths, feature exposure, resource limits, sandbox policy,
    or plugin loading. A cloned repository must not weaken your user settings.

## Common settings

| goal | setting | example |
| ---- | ------- | ------- |
| require isolation for read-only code | `sandbox.mode` | `strict` |
| use another HTTP port | `server.port` | `8390` |
| allow a model folder outside the launch folder | `files.allowed_dirs` | `["C:/models"]` |
| keep more models in memory | `workspace.max_resident` | `5` |
| allow larger viewer downloads | `viewer.max_model_mb` | `500` |
| enable IDS tooling | install the validation extra | `pip install "ifc-console[validation]"` |

Keep `files.allow_ai_save=false` unless automated persistence is a deliberate
requirement. The terminal `/save` command works regardless of that AI-only
setting.

## Session and server

| key | default | meaning |
| --- | ------- | ------- |
| `mode.default` | `ask` | startup permission mode |
| `server.port` | `8383` | HTTP port for MCP and the viewer |
| `server.persistent_token` | `true` | reuse one machine token so clients are configured once |
| `server.token_in_config_snippets` | `false` | include the token in generated direct-HTTP configurations |
| `tui.theme` | `blue` | `light`, `dark`, `modern`, or `blue` for the console, viewer, and chat |

## Generated code and sandbox

| key | default | meaning |
| --- | ------- | ------- |
| `exec.timeout_seconds` | `30` | maximum time for one code run |
| `exec.output_char_limit` | `40000` | output limit per field before truncation |
| `exec.allow_system_access` | `false` | allow system-class code in `edit` mode |
| `exec.system_modules_extra` | `[]` | additional imports classified as system access |
| `sandbox.mode` | `auto` | `auto`, `strict`, or `off` |
| `sandbox.memory_mb` | `2048` | sandbox worker memory cap |
| `sandbox.max_model_mb` | `512` | largest model copied into the sandbox; `0` disables the limit |
| `sandbox.startup_timeout` | `120` | worker startup timeout in seconds |
| `sandbox.load_timeout` | `600` | model-copy load timeout in seconds |
| `sandbox.warm_on_load` | `false` | start the sandbox when a model opens |

## Files and workspace

| key | default | meaning |
| --- | ------- | ------- |
| `files.allowed_dirs` | `[]` | additional readable roots |
| `files.allow_ai_save` | `false` | let AI operations persist IFC changes |
| `files.backup_retention` | `20` | backups retained per model |
| `files.follow_symlinks` | `false` | resolve symlinks while listing files |
| `files.max_open_mb` | `4096` | reject larger models; `0` disables the limit |
| `workspace.enabled` | `true` | index allowed folders for workspace discovery |
| `workspace.max_resident` | `3` | models held in memory, including the active model |
| `workspace.max_total_mb` | `6144` | combined resident model limit; `0` disables it |
| `workspace.scan_depth` | `3` | directory levels scanned |
| `workspace.scan_cap` | `10000` | maximum files examined per scan |

## Optional features

| key | default | meaning |
| --- | ------- | ------- |
| `viewer.enabled_default` | `false` | start the viewer automatically |
| `viewer.max_model_mb` | `200` | largest model sent to the browser |
| `chat.enabled_default` | `false` | start browser chat automatically |
| `chat.provider` | `openai` | `openai`, `anthropic`, `openrouter`, or `local` |
| `chat.model` | empty | initial provider model ID |
| `chat.base_url` | empty | provider URL override, commonly for a local server |
| `chat.tools` | `true` | allow the chat model to call ifc-console tools |
| `chat.max_tool_rounds` | `8` | maximum tool rounds per answer |
| `chat.local_only` | `false` | reject provider URLs outside this machine |
| `chat.timeout_s` | `300` | provider response timeout |
| `knowledge.enabled` | `true` | expose offline knowledge tools |
| `knowledge.autobuild` | `true` | build the index in the background |
| `knowledge.schemas` | all supported schemas | schemas included in the index |
| `knowledge.max_results` | `10` | default search result count |
| `plugins.enabled` | `false` | allow trusted operation plugins to load |
| `plugins.allow` | `[]` | exact plugin entry-point allowlist |

## Retention and logging

| key | default | meaning |
| --- | ------- | ------- |
| `recents.max` | `20` | recent models retained |
| `sessions.retention` | `50` | audit sessions retained |
| `automation.jobs_retention` | `200` | completed job records retained |
| `automation.artifact_retention_days` | `30` | minimum age for unreferenced cleanup candidates |
| `automation.validation_timeout_s` | `1800` | validation worker timeout |
| `automation.transaction_timeout_s` | `600` | preview, commit, and verification timeout |
| `automation.transaction_lock_timeout_s` | `15` | wait for another process editing the same IFC |
| `logging.level` | `info` | `debug`, `info`, `warning`, or `error` |
| `logging.file_enabled` | `true` | write the rotating application log |

Any setting can be overridden for one process with an environment variable.
For example, `IFC_CONSOLE_EXEC_TIMEOUT_SECONDS=60` sets
`exec.timeout_seconds`. Values are parsed as JSON when possible.
