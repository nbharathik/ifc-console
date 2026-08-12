# Safety

ifc-console treats AI output and IFC text as untrusted. The central rule is:
**the terminal user controls whether the active model may change.**

```text
ask mode -> inspect and analyze only
edit mode -> change memory -> user reviews -> /save or /reload
```

## Ask and edit

| action | `ask` (default) | `edit` |
| ------ | --------------- | ------ |
| inspect, validate, and calculate | allowed | allowed |
| preview a structured change | allowed | allowed |
| run model-changing Python | blocked | allowed |
| commit an approved ChangeSet | blocked | allowed |
| AI tool saves an IFC file | blocked | blocked by default |
| user runs `/save` | allowed | allowed |

Switch with `/mode`. Moving to `edit` requires confirmation, and no AI tool can
change the mode. Blocked operations return `ASK_MODE_BLOCKED` with a hint.

AI saving is a separate user opt-in: `files.allow_ai_save=true`. Client-level
permission prompts still apply on top of ifc-console policy.

## Generated code

`execute_ifc_code` passes through five controls:

```text
parse and classify -> capability check -> sandbox when eligible
        -> runtime guards -> mutation canary
```

1. Ambiguous code is treated as mutating.
2. The current mode and capabilities decide whether it may run.
3. On CPython 3.12+, eligible read-only code uses a restricted process.
4. Import, file, and model guards apply at runtime.
5. Unexpected mutation is detected after guarded execution.

In `sandbox.mode=auto`, unavailable isolation falls back to guarded in-process
execution and reports `sandboxed: false`. `strict` refuses that fallback.
Python 3.10 and 3.11 do not expose the raw-thread audit event required by the
complete boundary, so isolation is treated as unavailable on those versions.

Mutating code always runs in the main process because it must reach the live
model. See [Code sandbox](sandbox.md).

## Files, saves, and audit

- **Allowed roots:** AI tools can access only the launch folder, model folder,
  and directories explicitly added by the user. Other paths return
  `PATH_NOT_ALLOWED`.
- **Memory first:** edits stay in memory until `/save`; `/reload` discards them.
- **Safe replacement:** every overwrite creates a timestamped backup, writes a
  temporary file, then replaces the target atomically. A failed backup stops
  the save.
- **Audit:** calls, mode changes, mutations, saves, and taint events are written
  under `~/.ifc-console/sessions/<id>/` with secret redaction and a hash chain.

Use `/audit`, `ifc-console sessions show <id>`, or
`ifc-console sessions verify <id>`. Local verification detects modified or
reordered records; it is not an external append-only audit system.

## Local server boundary

- HTTP and WebSocket services bind to `127.0.0.1`.
- Session APIs require a bearer token.
- Host and Origin checks reject non-loopback requests.
- Viewer tokens use a URL fragment and are removed from the address bar.
- The stdio bridge verifies the listener before sending its token.

Rotate an exposed token with `ifc-console token rotate`. These controls do not
isolate applications running as the same OS user.

## Untrusted model text

Names, descriptions, headers, and property values come from the IFC author.
They may contain text designed to manipulate an AI assistant.

- `ask` mode remains read-only regardless of model text.
- Server instructions label IFC content as data, not commands.
- Instruction-shaped tool output is flagged.
- Every operation is visible and audited.

Review unfamiliar models in `ask` mode. Treat claims that "the model approved"
an action as suspicious.

## Structured changes and workflows

AI tools may preview and inspect a revision-bound ChangeSet. They cannot
approve, commit, restore, or change the mode; those are direct SDK or CLI
actions.

Version 1 workflows are read-only. They allow validation and selector queries,
but no Python, shell commands, plugins, network calls, or mutations.

## Limits

The sandbox is a containment process, not a virtual machine. It cannot defend
against Python or operating-system vulnerabilities, and ordinary files inside
an allowed model folder may be readable.

In-process guards reduce accidents but are not a security boundary against
deliberately malicious Python. Treat `edit` mode with untrusted prompts like
running an untrusted script. If an in-process call times out or taints the
session, use `/reload`.
