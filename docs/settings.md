# Settings

## Where things live

Everything is under `~/.ifc-console/` (override with `IFC_CONSOLE_HOME`):

```
~/.ifc-console/
  settings.json     your settings (only the keys you changed)
  token             the persistent server token (ifc-console token rotate renews it)
  recents.json      recently opened models
  backups/          timestamped copies made before every overwrite
  artifacts/        content-addressed reports, ChangeSets, backups, and receipts
  jobs/             durable automation job records
  transactions/     temporary isolated transaction workspaces
  sessions/<id>/    audit JSONL per session
  logs/             rotating application log
```

Projects may add `.ifc-console/settings.json` (and `.ifc-console/settings.local.json`
for personal, git-ignored values) next to their models.

## Precedence

```
defaults < user file < project file < project local file < env vars < CLI flags
```

`ifc-console settings list --sources` shows which layer set every key.

!!! info "Project files are sandboxed"
    A cloned repository must not weaken your safety settings, so project-level
    files may only set `tui.theme`. Anything else in a project file is ignored
    with a warning. Security, networking, feature exposure, logging verbosity,
    and resource budgets can only come from your own user file, environment, or
    explicit command-line flags. In particular, a cloned project cannot change
    `server.port`, enable the viewer, raise execution or model-size limits,
    enable system access, weaken the sandbox, widen allowed directories, or
    enable plugins.

## All keys

| key | default | meaning |
| --- | ------- | ------- |
| `mode.default` | `ask` | session mode at launch (`ask` = AI queries only, `edit` = AI may change the model) |
| `server.port` | `8383` | HTTP port for MCP + viewer |
| `server.persistent_token` | `true` | one token per machine (configure clients once); false = fresh token per run |
| `server.token_in_config_snippets` | `false` | include the token in printed snippets; opt in only when the destination is trusted |
| `exec.timeout_seconds` | `30` | wall clock limit per execute_ifc_code run |
| `exec.output_char_limit` | `40000` | per-field output cap before truncation; maximum `1000000` keeps sandbox replies within the protocol frame |
| `exec.allow_system_access` | `false` | permit SYSTEM-class code in edit mode |
| `exec.system_modules_extra` | `[]` | extra module names to treat as SYSTEM |
| `sandbox.mode` | `auto` | where read-only code runs: `auto` (sandbox, fall back if it cannot), `strict` (refuse instead of falling back), `off` |
| `sandbox.memory_mb` | `2048` | memory cap for the sandbox worker |
| `sandbox.max_model_mb` | `512` | models above this are not copied into the sandbox |
| `sandbox.startup_timeout` | `120` | seconds to wait for the worker to start |
| `sandbox.load_timeout` | `600` | seconds to wait for the worker to read the model |
| `sandbox.warm_on_load` | `false` | start the worker when a model loads, so the first code run is not the slow one |
| `files.allowed_dirs` | `[]` | standing allowed directories |
| `files.backup_retention` | `20` | backups kept per model file |
| `files.follow_symlinks` | `false` | resolve symlinks when listing |
| `files.max_open_mb` | `4096` | refuse to open larger files instead of risking OOM (0 disables) |
| `workspace.enabled` | `true` | index the allowed folders for `/workspace` and `find_files` (indexing only, nothing is loaded) |
| `workspace.max_resident` | `3` | models held in memory at once, active included; `1` restores strict single-model |
| `workspace.max_total_mb` | `6144` | combined size budget across resident models (0 disables) |
| `workspace.scan_depth` | `3` | folder levels the index walks |
| `workspace.scan_cap` | `10000` | files examined per scan |
| `chat.enabled_default` | `false` | start with the browser chat panel on |
| `chat.provider` | `openai` | openai, anthropic, openrouter, or local |
| `chat.model` | empty | model the panel opens with |
| `chat.base_url` | empty | override the provider URL (a local vLLM, say) |
| `chat.tools` | `true` | lend the chat model the ifc-console tools |
| `chat.max_tool_rounds` | `8` | tool rounds one chat answer may take |
| `chat.local_only` | `false` | refuse any chat provider that is not on this machine |
| `knowledge.enabled` | `true` | expose the offline knowledge tools |
| `knowledge.autobuild` | `true` | build the reference index in the background on first use |
| `knowledge.schemas` | `["IFC2X3","IFC4","IFC4X3"]` | schemas to index; fewer means a smaller, faster index |
| `knowledge.max_results` | `10` | default number of search hits |
| `plugins.enabled` | `false` | allow loading trusted Python operation plugins |
| `plugins.allow` | `[]` | exact entry-point names permitted to load |
| `viewer.enabled_default` | `false` | start with the built-in viewer on |
| `viewer.max_model_mb` | `200` | refuse to serve larger models to the viewer |
| `recents.max` | `20` | recents list length |
| `sessions.retention` | `50` | audit sessions kept |
| `automation.jobs_retention` | `200` | completed durable job records kept |
| `automation.artifact_retention_days` | `30` | minimum age for unreachable artifact cleanup candidates |
| `automation.validation_timeout_s` | `1800` | hard timeout for an isolated validation worker |
| `automation.transaction_timeout_s` | `600` | hard timeout for preview, apply, and verification workers |
| `automation.transaction_lock_timeout_s` | `15` | seconds to wait for another process editing the same IFC |
| `logging.level` | `info` | log verbosity |
| `logging.file_enabled` | `true` | write `~/.ifc-console/logs/ifc-console.log` |
| `tui.theme` | `dark` | console and viewer theme; `/theme dark|light|auto` sets it |

## Changing settings

```bash
ifc-console settings set exec.timeout_seconds 60      # from the shell
```

```
/settings exec.timeout_seconds 60                   # from the console
```

Both validate before writing and report the file they wrote to. Environment
variables override files per run: `IFC_CONSOLE_EXEC_TIMEOUT_SECONDS=60`.
