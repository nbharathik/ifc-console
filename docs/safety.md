# Safety model

ifc-console assumes the LLM is helpful but fallible, and sometimes exposed to
hostile input. The goal: **you can hand an LLM your model and know exactly what
it can and cannot do to it.**

## The mode switch

One binary switch, owned by you:

| | `ask` (default) | `edit` |
| --- | --- | --- |
| structured queries | yes | yes |
| generate/show code | yes | yes |
| run mutating code | blocked with an error | yes |
| save to disk | blocked with an error | yes |
| swap the loaded model | yes (visible in your terminal; refuses if unsaved changes) | yes |
| file on disk written without a backup | never | never |

The mode is set at launch (`--mode`) or in the console (`/mode`). Switching to
`edit` asks you to confirm. **There is no MCP tool to change the mode.** In
`ask` mode a blocked operation returns `ASK_MODE_BLOCKED` with a hint telling
the AI to ask you. That is all it can do.

There is no per-operation approval prompt in ifc-console, on purpose. Modern AI
clients (Claude Code, Cursor, ...) already prompt for tool calls, and
duplicating that here would mean answering every change twice. ifc-console enforces
the one thing the client cannot: whether the model file can change at all.

## How a code run is gated

`execute_ifc_code` is the power tool. Each call travels this pipeline:

1. **Classify** (AST analysis): the code is parsed and marked QUERY, EDIT, or
   SYSTEM (imports of os/network modules, file access, exec/eval). The
   classifier is biased toward false positives: anything ambiguous counts as
   EDIT.
2. **Gate** (policy matrix): QUERY runs in both modes. EDIT is blocked in ask
   and runs in edit. SYSTEM also needs `exec.allow_system_access`, and never
   runs in ask.
3. **Guard** (runtime): guarded runs use a curated namespace: import allowlist,
   a write-blocking `open`, a raising `ifc_api` proxy, and a model object that
   rejects mutation methods. Guards catch what the classifier missed.
4. **Verify** (canary): the model's max entity id is compared before and after
   every guarded run. If guarded code still grew the model, the session is
   marked **tainted**, the event is audited, and the status bar tells you to
   `/reload` a pristine copy.

## Files, backups, audit

- **Allowed directories.** The LLM can only list/open/save models inside the
  launch directory, the loaded model's directory, and any `--allow-dir` you add.
  Everything else returns `PATH_NOT_ALLOWED`.
- **Atomic saves with backups.** Every save writes to a temp file and renames it
  into place. Any file being replaced is first copied to `~/.ifc-console/backups/`
  with a timestamp (retention configurable). If the backup fails, the save does
  not happen.
- **Audit log.** Each session appends JSONL records (tool calls, executed code
  with the model-stated intent, mode changes with who made them, saves, taints)
  under `~/.ifc-console/sessions/<id>/`. Inspect with `/audit` or
  `ifc-console sessions show <id>`.
- **Network surface.** The server binds to 127.0.0.1 only, and every HTTP and
  WebSocket request needs the bearer token. The token is persistent per machine
  (stored owner-readable in `~/.ifc-console/token`) so clients are configured once.
  `ifc-console token rotate` invalidates it instantly; `server.persistent_token
  false` switches to a fresh token per run.
- **Port squatting.** Clients pin `http://127.0.0.1:<port>/mcp`, so if another
  local program listened on your port, clients would send it their requests,
  token included. ifc-console refuses to start on an occupied port and identifies
  the occupant (so does `doctor`). If it happens, move the port and rotate the
  token.

## What this does not guarantee

In-process guards stop accidents and default behavior, **not a determined
adversary**. CPython cannot truly sandbox itself, and a creative enough payload
can escape object-graph confinement. The test suite documents a known bypass on
purpose. So:

- Treat `ask` as a strong safety rail against mistakes, not a security boundary
  against malicious prompt-injected code.
- The disk-integrity promise is stronger than the in-memory one: the suite
  asserts the file on disk stays byte-identical under a battery of bypass
  attempts.
- A subprocess executor with real isolation is the first item on the hardening
  roadmap.

Also: a run that exceeds `exec.timeout_seconds` cannot be killed mid-C-call. The
session pauses ("poisoned") and `/reload` recovers it.
