# Settings

## Where things live

Everything is under `~/.ifc-console/` (override with `IFC_CONSOLE_HOME`):

```
~/.ifc-console/
  settings.json     your settings (only the keys you changed)
  token             the persistent server token (ifc-console token rotate renews it)
  recents.json      recently opened models
  backups/          timestamped copies made before every overwrite
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
    files may only set a safe subset: `mode.default`, `server.port`,
    `exec.timeout_seconds`, `exec.output_char_limit`, `viewer.enabled_default`,
    `viewer.max_model_mb`, `logging.level`, `tui.theme`. Anything else in a
    project file is ignored with a warning. In particular, `files.allowed_dirs`,
    `exec.allow_system_access`, and every `sandbox.*` key can only come from
    your own user file, environment, or flags.

## All keys

| key | default | meaning |
| --- | ------- | ------- |
| `mode.default` | `ask` | session mode at launch (`ask` = AI queries only, `edit` = AI may change the model) |
| `server.port` | `8383` | HTTP port for MCP + viewer |
| `server.persistent_token` | `true` | one token per machine (configure clients once); false = fresh token per run |
| `server.token_in_config_snippets` | `true` | include the token in printed snippets |
| `exec.timeout_seconds` | `30` | wall clock limit per execute_ifc_code run |
| `exec.output_char_limit` | `40000` | envelope size cap before truncation |
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
| `viewer.enabled_default` | `false` | start with the viewer on (needs the `viewer` extra) |
| `viewer.max_model_mb` | `200` | refuse to serve larger models to the viewer |
| `recents.max` | `20` | recents list length |
| `sessions.retention` | `50` | audit sessions kept |
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
