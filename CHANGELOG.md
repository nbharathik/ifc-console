# Changelog

## [0.2.0] - 2026-09-29

The release is about editing you can trust, and about what an AI client pays to
use the console. One package installs everything, every edit is an undoable
transaction, the viewer follows an edit without rebuilding, and a client can
list 14 tools instead of 71.

### Highlights

- **One package.** `pip install ifc-console` includes the console, MCP, SDK,
  viewer and Agent workspace. `ifc-console[agents]` adds engines over ACP, the
  system keyring and PDF support; `ifc-console[validation]` adds IDS checking.
  `ifc-console-viewer` is retired and its code lives in `ifc-console`.
- **Edits are transactions.** Every `execute_ifc_code` run that changes the model
  and every `set_properties` call is one undoable step. A run that raises is
  rolled back whole and the result says so (`rolled_back`, `verified`). `/undo`,
  `/redo` and `/changes` in the console, Undo and Redo in the viewer, `undo()` and
  `redo()` in the SDK; the last 20 steps are kept (`edit.undo_depth`).
- **`set_properties`.** Structured, all-or-nothing property edits by GlobalId,
  with `dry_run`, per-row before and after, and a change record that says which
  elements were touched and whether geometry changed.
- **The viewer follows edits in place.** An edit that touches no geometry updates
  the open properties and the tree labels without parsing the model again;
  saving and entering edit mode never rebuild it. A geometry edit rebuilds once.
- **Lean by default for LLMs.** The `lean` profile lists 14 tools plus
  `find_tools` and `call_tool`; `full` lists everything. Listing plus
  instructions cost 104.8k characters in 0.1.4; the full profile is now 67.8k
  and the lean profile 17.0k. Every tool stays callable in either profile.
- **Responsive under load.** One read/write gate replaces a single lock: queries,
  viewer requests and read-only code overlap, a large model parses without
  blocking the current one, the MCP server is up while the first model loads, and
  the sandbox releases its copy of the model when idle.
- **The console is the control plane.** `/clients` lists who connected,
  `/tools profile` chooses what they see, `/connect` shows which clients are on
  the machine and whether each points here, the feed names the client and folds
  a burst of reads into one line, and 13 commands moved under their parents.

### Breaking changes and migration

- **Packaging.** Removed the extras `viewer`, `pdf`, `graph`, `keys`,
  `documents`, `geometry`, `dev` and `docs` (`trimesh` is now a base dependency;
  `dev` and `docs` are dependency groups: `uv sync --group dev --all-extras`).
  Agent code lives under `ifc_console.agents` instead of `ifc_console_agents`.
  Removed the deprecated modules `ifc_console.chat`, `ifc_console.devkit`,
  `ifc_console.credentials`, `ifc_console.testing`, `ifc_console.mcp.tools_skills`
  and the LangGraph integration. LangGraph, `pypdf` and PyMuPDF (AGPL) are no
  longer dependencies; `pypdfium2` reads and renders PDFs. The MCP SDK floor is
  now `mcp>=1.28`.
- **Agent workspace.** Every conversation belongs to an assistant, so plain chat
  and its route `POST /api/chat/stream` are gone, and so are the custom-agent
  studio, `/agent new`, `POST /api/agents/custom` and `/api/agents/custom/delete`.
  Agent blueprint files in the console home still load. The settings
  `chat.tools` and `chat.max_tool_rounds` are removed, `GET /api/agents/workspace`
  needs a real agent, and the AI SDK message adapter (`chat_ai_sdk.js`) is
  replaced by a small `sse.js`, and `GET /api/agents/blocks` is removed.
  `Workbench.ask()` keeps its arguments, result and `on_event` events but now runs
  on the bundled `Agent`.
- **Agent panel layout.** The composer has four controls: model, Ask or Edit,
  Send or Stop, and `+`. Approval or Auto (now confirmed before Auto turns on),
  memory, keyboard shortcuts, skills and the IFC model choice live in the `+`
  menu. Models and App settings moved into a Settings dialog opened from the model
  pill, and the workspace has three tabs (Assistant, Content, Skills) behind one
  entry, the header gear.
- **MCP results.** A result is one text block of single-line JSON. No tool
  publishes an `outputSchema` or `_meta`, and `destructiveHint` is not sent for
  read-only tools. The declared data shapes remain in the operation registry and
  the SDK. Server instructions are under 3,000 characters; the catalogue workflow
  moved to the `catalogue_parameters` prompt.
- **Revisions.** Saving and entering edit mode no longer change a model's
  fingerprint or revision. The revision moves when an edit is kept, undone or
  redone, and a reload starts a new identity (`load_nonce`). A save reports the
  load-time `fingerprint` and the new `content_sha256`.
- **Failed edits.** A failed edit leaves no partial changes. `partial_changes_possible`
  appears only when a rollback could not be verified or the run timed out.
- **Console commands.** `/attach`, `/detach`, `/use`, `/info` moved under
  `/models`, `/recent` and `/workspace` under `/file`, `/copy` and `/port` under
  `/connect`, `/theme`, `/sandbox` and `/kb` under `/settings`, `/audit` under
  `/status`, `/workflows` under `/agent`. The old names still work.
- **New error codes:** `INVALID_IFC`, `TOOL_SOURCE_FAILED`, `WORKFLOW_INVALID`,
  `NOTHING_TO_UNDO`, `NOTHING_TO_REDO`, `UNDO_FAILED`.
- **New settings:** `edit.undo_depth`, `mcp.tool_profile`, `sandbox.idle_stop_s`,
  `tui.feed`. `harness.engines.<name>.forward_keys` limits which provider keys an
  engine receives.

### Security

- An agent engine receives only the provider keys it needs, not every key.
  Engine adapters fetched through `npx` are pinned to exact versions.
- Auto approval covers ifc-console operations only. An engine's own shell or file
  request is asked about or denied.
- "Always allow" is decided by the server, keyed by conversation, tool and
  capability set, and is never offered for code or process tools. Stop now
  interrupts the engine instead of only closing the stream.
- The assistant can write the working copy only while the session is on that
  copy; a user's save-as elsewhere ends that permission. There is one save path.
- Provider key lookups no longer run on the event loop.
- PyMuPDF (AGPL) is gone from an Apache-2.0 product.

### Performance and size

- The lock file holds 110 packages instead of 134, and a development
  environment installs 85 instead of 123; the main wheel is about 2.3 MB. A plain
  `pip install ifc-console` pulls 49 packages on Windows, the same count as 0.1.4
  with its viewer package; `[agents]` adds 8 and `[validation]` 17.
- Tool listing plus instructions: 104.8k characters to 67.8k (full) or 17.0k
  (lean). Results are compact JSON, which also fits more rows under the output
  limit before a result has to be paged.
- A cached read no longer queues behind a running edit or sandboxed run: while a
  5 second run was in flight it waited 5.7 to 8.6 seconds in 0.1.4 and now
  answers in under 3 ms (p95, three test models). The read cache is a bounded
  LRU, loading hashes the file once, and the audit log keeps one file handle.
  An edit that sets one property takes 14 to 23 ms and is one undoable step.
- A large model parses outside the lifecycle gate, so the current model keeps
  answering while another loads, and a call that arrives while the first model
  loads waits up to 20 seconds instead of failing.
- The viewer serves a saved model from disk and an edited one from memory, and
  no longer rebuilds after a save or an edit that changed no shapes.
- The code is easier to work in: `ifc_console.cli` is a package of ten modules,
  the viewer's `app.js` is 2,200 lines shorter with 15 leaf modules, and
  `scripts/check_file_sizes.py` keeps a file from growing past its ceiling.

### Added

- **Editing.** `set_properties`, undo and redo, change records (`data.change` on
  every edit and on `model_mutated` events), and `verify_rollback`, which checks
  a rollback against IfcOpenShell's own log because it swallows some errors.
- **MCP.** Tool profiles chosen by the header `X-IFC-Console-Tools`, the path
  `/mcp/<profile>`, or `mcp.tool_profile`; `find_tools` (BM25 search over the
  live catalog) and `call_tool`, which refuses tools that delete or overwrite;
  `ifc-console bridge --tools lean`; `mcp-config --tools`.
- **Console.** `/undo`, `/redo`, `/changes`, `/clients`, `/tools profile`, client
  detection in `/connect`, `tui.feed = compact | verbose`.
- **Agent engines.** OpenCode, Codex, Claude, Goose, Gemini or any command that
  speaks the Agent Client Protocol can run the Agent workspace's loop, listed as
  a provider named `harness:<name>` (see `docs/engines.md`). A scripted engine
  rehearses the path offline.
- **`execute_ifc_code` toolkit.** Any installed package imports (a denied set is
  refused by capability), geometry helpers (`np`, `geom`, `shape_util` and more)
  load on first use, and the description lists what this installation has.
  Mutating runs get their own timeout (`exec.edit_timeout_seconds`).
- **Edit mode works in a copy.** Entering edit mode snapshots the file; every
  save, reload and download touches the copy; `save_ifc_file` can name only the
  copy. Save, Download and a change count sit in the viewer and the panel.
- **Analysis tools.** `compare_models`, `query_spatial`, `check_model_health`,
  `audit_element_properties`, `assess_model_quality`, `analyze_element_geometry`,
  `measure_*`, `export_measurement_report`, `lookup_table_rows`, and dotted
  property projection in `query_elements`.
- **Viewer.** Camera and section control in the model's own axes, measurements
  (distance, angle, area, element size, clearance) with snapping to real
  features, orthographic projection, focus tabs, units and precision, silhouette
  edges, and demand-driven rendering with a bounded parsed-model cache.
- **Agent workspace.** One assistant with skills, workflows and project
  documents; preconfigured workflows with reviewer gates; recorded skills from
  viewer measurements; AI-marked property sets with provenance; a memory pill;
  approvals as one row; conversation state that survives reloads.
- **Python SDK.** `set_properties`, `undo`, `redo`, project documents and
  knowledge, response models, concurrent read-only tool rounds, and a quickstart
  agent example.

### Fixed

- Every derived volume on a georeferenced model was wrong: signed tetrahedra were
  summed over absolute world coordinates and float64 cancelled the result. The
  mesh is centred first.
- A mutating run that was cancelled or timed out left the model flagged clean, so
  the next open silently discarded the edit.
- A viewer measurement could be taken through a wall or a section cut, and an
  assistant's edit deleted the user's measurements. Measurements are anchored to
  GlobalIds and replayed across rebuilds.
- An oversized result became `ok: true` with its rows gone; results now keep the
  rows that fit and name the offset to resume from.
- Finishing one conversation could deny another's pending approval, and a spent
  tool budget discarded a whole run instead of answering.
- Pressing `c` in the viewer with the Agent panel available threw a
  `ReferenceError` instead of opening the panel.
- A second viewer tab erased the first tab's selection; commands and screenshots
  ran against a half-built scene; `show_all`, `select` and `isolate` did not do
  what the buttons do; the area tool counted a rectangle as six points.
- Edit-mode proposals failed in the sandbox because working copies live under
  the console home; `ifc.createIfc*(...)` was refused as SYSTEM code.
- `sandbox` runs no longer read a stale copy when an edit starts while a run is
  queued, and a sandbox load re-checks that the model did not change under it.

## [0.1.4] - 2026-08-12

- Make the Three.js/web-ifc viewer and browser chat bundle an optional
  `ifc-console[viewer]` installation, with a viewer-free core wheel.
- Reorganize and simplify the documentation, with a shorter onboarding path,
  clearer safety guidance, grouped settings, and task-based navigation.
- Fail closed on Python 3.10 and 3.11 when complete generated-code isolation is
  requested, because those runtimes cannot audit raw thread creation; `auto`
  reports its guarded fallback and `strict` refuses it.

The changelog can be found on the
[GitHub Releases page](https://github.com/nbharathik/ifc-console/releases).
