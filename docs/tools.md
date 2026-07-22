# MCP tools reference

Every tool returns one JSON envelope:

```json
{ "ok": true, "data": { ... }, "meta": { "mode": "ask", "model": "x.ifc",
  "schema": "IFC4", "dirty": false, "fingerprint": "ab12..." } }
```

Failures are never protocol errors. They come back as data the LLM can act on:

```json
{ "ok": false, "error": { "code": "ASK_MODE_BLOCKED",
  "message": "...", "hint": "Ask the user to run /mode edit..." },
  "meta": { ... } }
```

Oversized results are truncated with `meta.truncated: true` and a note telling
the model to narrow the query.

## Query tools

### get_session_status

No arguments. Server version, loaded model (name, path, schema, size), mode,
dirty flag, and viewer state (enabled, connected tab count, URL). The place to
orient.

### get_ifc_project_info

No arguments. Schema, project name/description, units, counts of
sites/buildings/storeys/spaces, entity counts for common classes, top materials
and classifications, authoring-tool header info.

### get_spatial_structure

| arg | type | default |
| --- | ---- | ------- |
| `root_global_id` | str or null | project root |
| `depth` | int 1-20 | 10 |
| `include_element_counts` | bool | true |

The containment tree (Project > Site > Building > Storey > Space) with direct
element counts per node.

### query_elements

| arg | type | default |
| --- | ---- | ------- |
| `query` | selector string | required |
| `limit` / `offset` | int | 50 / 0 |
| `fields` | list | name, storey, type_name |
| `order_by` | class, name, storey | class |

IfcOpenShell selector syntax, one summary row per element. Examples:
`IfcWall` · `IfcWall, IfcSlab` · `IfcWall, material=concrete` ·
`IfcWall, Pset_WallCommon.FireRating=F30` · `IfcElement, Name=/W.*1/`.
Syntax errors return `INVALID_QUERY` plus a cheat sheet in `data`.

### get_element

Up to 50 GlobalIds. `include` selects sections from attributes, psets, qtos,
materials, type, container, openings, decomposition. Missing ids are reported,
not fatal.

### get_psets

Up to 100 GlobalIds. `psets_only` / `qtos_only` filters. Lighter than
`get_element` when only property sets matter.

### get_schema_docs

`entity` (e.g. `IfcWall`) and optional `attribute`. Official schema
documentation: definition, attribute table with types and optionality,
supertype chain, predefined types. Works with no model loaded (defaults to
IFC4).

## Execution

### execute_ifc_code

| arg | type | notes |
| --- | ---- | ----- |
| `code` | str | Python source; no `bpy`, this is not Blender |
| `description` | str <= 200 chars | one-line intent, shown in the terminal and audit log |

Runs Python against the loaded model with `ifc`, `ifcopenshell`, `ifc_api`,
`element_util`, `selector_util`, `unit_util`, `query(sel)`, and `get_ifc_file()`
pre-injected. stdout is captured; a final bare expression is returned like a
REPL. Gating per the [safety model](safety.md). Output fields: `stdout`,
`result`, `classification`, `mutated`, `duration_ms`. Errors:
`ASK_MODE_BLOCKED`, `EXEC_BLOCKED`, `EXEC_ERROR` (with traceback),
`EXEC_TIMEOUT`.

## Files

### list_ifc_files

Optional `dir`, `recursive`, `limit`. IFC files in the allowed directories plus
recents: path, size, mtime, schema (peeked cheaply), recent flag.

### open_ifc_file

`path`. Replaces the loaded model (the user sees the switch in their terminal).
Works in both modes: opening is reading, not editing. Refuses if the current
model has unsaved changes (`UNSAVED_CHANGES`) and enforces allowed directories.

### save_ifc_file

Optional `output_path` (save-as) and `overwrite`. Atomic write, automatic
timestamped backup of anything replaced, clears the dirty flag. Refused in ask
mode (`ASK_MODE_BLOCKED`). Output: `path`, `size_bytes`, `backup_path`,
`fingerprint`.

## Viewer (optional category)

The viewer is not part of the core surface. These three tools are **registered
only while the viewer is enabled** (`--viewer` at launch or `/viewer` in the
console). Enable it mid-session and they join the tool list live; `/viewer off`
removes them (clients pick up the change on their next tool refresh). Sessions
without the viewer, including all stdio sessions, expose exactly the 11 core
tools above.

When registered, all three still need a connected browser tab, or they return
`VIEWER_NOT_CONNECTED` with a hint on how to start one.

### get_viewer_selection

No arguments. GlobalIds the user click-selected, one summary row each,
`selected_at` timestamp. The human's way of pointing at things.

### highlight_elements

| arg | type | default |
| --- | ---- | ------- |
| `global_ids` | list <= 500 | [] |
| `color` | `#rrggbb` | `#ff3b30` |
| `isolate` | bool | false |
| `fit` | bool | true |
| `clear` | bool | false |

Colors elements in the user's browser. `isolate` hides everything else; `clear`
resets. Allowed in every mode (visual only).

### get_viewer_screenshot

| arg | type | default |
| --- | ---- | ------- |
| `view` | top, bottom, front, back, left, right, iso, current | current |
| `fit` | all, selection, highlighted | none |
| `max_size` | int 64-2048 | 800 |
| `format` | jpeg, png | jpeg |
| `quality` | int 1-100 | 85 |

Captures the canvas and returns it inline as MCP image content plus a text note.
Errors: `VIEWER_TIMEOUT` (10 s), `VIEWER_ERROR`, `RESULT_TOO_LARGE`.

## Error codes

| code | meaning |
| ---- | ------- |
| `NO_MODEL_LOADED` | open a model first (`list_ifc_files` + `open_ifc_file`, or /file) |
| `FILE_NOT_FOUND` / `FILE_EXISTS` | path problems on open/save |
| `PATH_NOT_ALLOWED` | outside the allowed directories |
| `UNSAVED_CHANGES` | refusing to drop unsaved work |
| `INVALID_INPUT` / `INVALID_QUERY` | bad arguments; query errors include a cheat sheet |
| `ASK_MODE_BLOCKED` | mutation or save attempted in ask mode; the AI should ask for /mode edit |
| `EXEC_BLOCKED` / `EXEC_ERROR` / `EXEC_TIMEOUT` | code run gated, failed, or timed out |
| `MODEL_BUSY` | session paused after a timeout; user reloads |
| `VIEWER_NOT_CONNECTED` / `VIEWER_TIMEOUT` / `VIEWER_ERROR` | viewer tools without a healthy tab |
| `RESULT_TOO_LARGE` | narrow the request |
| `INTERNAL_ERROR` | a bug; the audit log has details |
