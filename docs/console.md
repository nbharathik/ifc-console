# The console

Running `ifc-console` with no subcommand opens the interactive console. It is
shaped like a coding-agent CLI: one scrolling surface, a command prompt at the
bottom, and modal cards when something needs your decision.

```
+----------------------------------------------------------------------+
| model.ifc · IFC4 · 2.1 MB   MODE: ASK                                | <- status
| MCP http://127.0.0.1:8383/mcp   viewer: 1 tab                        | <- server bar
|                                                                      |
| 14:02:11  ok  get_ifc_project_info  212ms                            | <- live feed
| 14:02:19  ok  query_elements  48ms                                   |
| mode changed to edit (by tui)                                        |
| ...                                                                  |
| +------------------------------------------------------------------+ |
| | ask   the AI can query; changing the model is blocked            | | <- completion
| | edit  the AI can change and save the model; backups are automatic| |    menu
| +------------------------------------------------------------------+ |
| > /mode _                                                            | <- prompt
+----------------------------------------------------------------------+
```

## The three regions

- **Status bars** (top): loaded model, schema, size, session mode (green ask,
  red edit), unsaved-changes marker, the MCP endpoint, and viewer tab count.
  Always current.
- **Feed** (middle): everything that happens, in order. Server lifecycle, every
  MCP tool call with duration and result, viewer selections, mode changes, and
  slash-command output. This is also the server's activity log; there is no
  separate log window. `ctrl+l` or `/clear` clears it, ++page-up++ /
  ++page-down++ scroll it. File logging to `~/.ifc-console/logs/` continues
  regardless.
- **Prompt** (bottom): type slash commands. A completion menu opens above the
  prompt as you type. ++up++ / ++down++ recall history when it is closed,
  ++escape++ closes it or clears the line.

## The completion menu

Everything is picked in place. Type `/` and every command appears with a
one-line description. Keep typing to narrow the list (`/mo` leaves `/mode`
and `/models`).

- ++tab++ inserts the highlighted entry without running it.
- ++up++ / ++down++ (or the mouse wheel) move the highlight.
- ++enter++ picks the highlighted entry. A command with argument values (like
  `/mode`) advances to those values; picking a value (like `edit`) runs the
  line. A command with nothing left to choose (like `/status`) runs at once.
- Clicking an entry is the same as highlighting it and pressing ++enter++.
- ++escape++ closes the menu; the next keystroke or ++tab++ reopens it.

The menu knows each command's values: `/mode` offers `ask` and `edit`;
`/viewer` offers open, `off`, `url`; `/copy` and `/connect` list their targets;
`/settings` lists every key with its value; `/file` lists recent models and
nearby IFC files, filtered as you type.

!!! tip "Where do prompts go?"
    The console is not a chat. You talk to the LLM in your MCP client (Claude
    Code, Cursor, ...), or in the optional browser panel that `/chat` opens.
    The console is where you control the session those conversations run
    against.

## Commands

| command | what it does |
| ------- | ------------ |
| `/help [command]` | list all commands, or explain one with examples |
| `/file [path\|filter]` | open a model: no argument opens the picker, a path opens that file, a word filters the list |
| `/workspace [dir]` | browse a whole folder and check several files at once (`dir` sets the root) |
| `/models` | list loaded models and attached files |
| `/attach <path>` | load a file alongside the active model (extra IFC, or an IDS for the AI) |
| `/detach <id>` | release an attached model or file |
| `/use <id>` | make a loaded model the active one |
| `/recent` | list recently opened models |
| `/mode [ask\|edit]` | show or change what the AI may do (switching to edit asks to confirm) |
| `/theme [dark\|light\|auto]` | switch the console theme (persists; open viewer tabs follow) |
| `/viewer [off\|url]` | open the 3D viewer (its 4 MCP tools register live); `off` closes tabs and removes them, `url` prints the link |
| `/chat [solo\|off\|provider]` | open the 3D view with the chat panel beside it; `solo` leaves the 3D view out |
| `/connect [client\|all]` | shared-console bridge setup for claude-code, claude-desktop, cursor, vscode, codex |
| `/copy [client\|url\|viewer\|token]` | copy a complete client setup, MCP URL, viewer URL, or token |
| `/sandbox [auto\|strict\|off\|restart]` | show or change where AI-generated code runs, and what it may touch |
| `/status` | session summary |
| `/info` | entity counts for the active model |
| `/kb [query]` | search the offline IFC reference (schema, property sets, API, recipes) |
| `/save [path]` | save in place, or save-as to a new path |
| `/reload` | reload from disk, discarding unsaved changes (also recovers a stuck session) |
| `/port <n>` | move the MCP server to another port |
| `/audit [n]` | show the last n audit records |
| `/settings [key value]` | inspect or change settings |
| `/clear` | clear the feed |
| `/quit` (alias `/exit`) | exit (asks about unsaved changes) |

Unique prefixes work: `/stat` runs `/status`. Unknown commands suggest the
closest match.

Two names changed in 0.1.4 so they stop looking alike. `/open` became part of
`/file`, and `/model` (entity counts) became `/info`, which no longer reads
like a typo for `/models` (the loaded-model list). Both old names still work
and print where they went.

`/connect <client>` displays that client's complete setup and copies it to the
system clipboard automatically. For example, `/connect codex` copies the full
TOML entry. `/connect all` prints every setup without replacing the clipboard;
use `/copy codex`, `/copy cursor`, `/copy vscode`, `/copy claude-desktop`, or
`/copy claude-code` to copy one again. Bare `/copy` and the older `/copy cmd`
both mean Claude Code.

The setup omits the active IFC path on purpose. Client config is a one-time
connection to the console; `/file` controls which model that session serves.

## Opening files

`/file ` (with the space) completes files in the menu: recently opened models
from the folder where the console was launched, then every `.ifc`, `.ifczip`,
and `.ifcxml` there and in its immediate subdirectories, newest first. Recent
models and `--allow-dir` files outside that launch folder are omitted. Type any
part of a name to filter, pick, done. For files the menu does not know, `/file
<path>` takes any absolute or relative path, and a bare path in the prompt works
too.

Bare `/file` shows the same list in a full-height picker, useful when there are
many models to scan.

Start ifc-console in your project folder and both show exactly the models you care
about, no paths to type.

## Working with a folder (optional)

One model at a time is the default and the normal way to work. When a job needs
more than that, `/workspace` opens a panel over the whole folder:

```
+--------------------------------------------------------------------+
| Workspace                                                          |
| One model is the default. Check extra files to load them alongside |
| it: IFC models attach read-only, an IDS is handed to the LLM.      |
| filter: _                                                          |
| +----------------------------------------------------------------+ |
| | [X] IFC  ABC-XYZ-ZZ-XX-M3-A-0001.ifc  ARC · 128 MB · IFC4      | |
| | [X] IFC  ABC-XYZ-ZZ-XX-M3-S-0001.ifc  STR · 94 MB · IFC4       | |
| | [X] IDS  employer-requirements.ids    12 specs                 | |
| +----------------------------------------------------------------+ |
| 3 of 27 indexed · checked: 2 model(s), 1 companion file(s)         |
|       [ Check all ] [ Clear checks ] [ Open selection ] [ Cancel ] |
| Tab check/uncheck · Enter open selection · Up/Down move            |
| Ctrl+A check all · Ctrl+D clear · Ctrl+U other types · Esc cancel  |
+--------------------------------------------------------------------+
```

- The panel indexes files, it never loads them. Kind comes from the file
  header, not the extension, so a mislabelled file is still recognized.
- Only kinds ifc-console can use are listed; ++ctrl+u++ shows everything else.
- ++tab++ (or a mouse click) checks or unchecks the highlighted row and moves
  down, so a run of files is a run of Tabs. The **Check all** and **Clear
  checks** buttons make larger selections easier; ++ctrl+a++ and ++ctrl+d++ are
  their shortcuts.
- ++enter++ opens what is checked; ++ctrl+o++ and the **Open selection** button
  do the same thing. With nothing checked, opening takes the highlighted file,
  exactly like `/file`, so filter then ++enter++ is the quick single-file path.
- Applying gives you one **active** model plus read-only companions. `/models`
  lists them, `/use <id>` moves the active focus, `/detach <id>` frees one.

Only the active model can be changed or saved. Attached models are read-only,
and the AI gets `MODEL_READ_ONLY` if it tries otherwise. Attached IDS and BCF
files are not loaded at all: their paths are handed to the AI so it can call
`validate_ids` without you pasting anything.

Adding a folder to the allowed roots stays your decision. The AI can search
the workspace and open files inside it, never widen it.

`workspace.max_resident` (default 3) caps how many models are held at once;
set it to 1 to restore strict single-model behaviour. `workspace.max_total_mb`
caps their combined size. A model with unsaved changes is never evicted.

## The mode switch

`ask` (the default) lets the AI query the model. Every attempt to change or
save it fails with an error telling it to ask you. `edit` lets it change and
save; every save makes a timestamped backup first. Switching to `edit` asks you
to confirm (++y++ / ++n++); `/mode ask` locks the model again instantly. The AI
has no tool to change the mode.

Per-operation permission prompts are your AI client's job (Claude Code, Cursor,
and friends all have them). ifc-console enforces the one thing they cannot:
whether the model file can change at all. The feed and the audit log record
every mutating run with the AI's stated intent.

## Quitting

Drag over text in the feed and press ++ctrl+c++ to copy the selection. With no
selection, ++ctrl+c++ is the standard terminal exit (also `/quit`, or
++ctrl+q++). With unsaved changes you get three choices: save and quit,
discard and quit, or cancel.
