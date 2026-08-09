# MCP tools reference

Every tool returns one JSON envelope:

```json
{ "ok": true, "data": { ... }, "meta": { "mode": "ask", "model": "x.ifc",
  "schema": "IFC4", "dirty": false, "fingerprint": "ab12..." } }
```

The envelope arrives twice: as machine-readable `structuredContent` (every
tool except `get_viewer_screenshot`, which returns image content, advertises
the envelope as its `outputSchema`) and as the same JSON in the text block for
clients that only read text. Cached heavy reads carry `meta.cached: true`.

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
dirty flag, and viewer state (enabled, connected tab count, URL).

### orient

No arguments. One-call orientation: session status, the project summary, and
the spatial tree to depth 2, in a single round-trip. The recommended first
call on a fresh connection.

### describe_capabilities

No arguments. The live tool list with one-line purposes, the current mode and
what it permits, viewer state, and worked examples. The tool surface changes
at runtime (the viewer category comes and goes), so this is the ground truth.

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

| arg | type | default |
| --- | ---- | ------- |
| `entity` | e.g. `IfcWall` | none |
| `attribute` | attribute of that entity | none |
| `pset` | e.g. `Pset_WallCommon` | none |
| `property` | e.g. `FireRating` | none |
| `schema` | IFC2X3, IFC4, IFC4X3 | the loaded model, else IFC4 |

Official schema documentation, three ways. `entity` returns the definition, the
attribute table with types and optionality, the supertype chain, the predefined
types, and the property sets that apply to it. `pset` returns one property set:
every property with its data type, enumerated values, and the entities it
applies to. `property` is the reverse lookup, naming the property sets that
define a property. Name at least one of the three. Works with no model loaded.

## Knowledge tools

Offline reference built from the installed ifcopenshell. No network. See
[Knowledge index](knowledge.md).

### search_ifc_knowledge

| arg | type | default |
| --- | ---- | ------- |
| `query` | plain words | required |
| `kind` | entity, pset, property, type, api, recipe | all |
| `schema` | IFC2X3, IFC4, IFC4X3 | the loaded model |
| `limit` | int 1-50 | 10 |

Ranked hits across the IFC schema, property sets, every `ifcopenshell.api`
function, and the verified recipe cookbook. Each hit carries a `key`, a
summary, and a snippet. Fails with `KNOWLEDGE_NOT_READY` while the index is
still building.

### get_knowledge_record

`key` from a search hit (e.g. `api:pset.add_pset`, `recipe:rename-elements`).
Returns the full text.

### get_api_docs

`function` as `module.function` (e.g. `pset.add_pset`), or `search` in plain
words, or neither to list the API modules. Returns the exact call signature and
docstring. An unknown name comes back with the closest matches in the hint.

## Analysis tools

### validate_model

| arg | type | default |
| --- | ---- | ------- |
| `express_rules` | bool | false |
| `max_issues` | int 1-2000 | 200 |

Schema validation via the bundled IfcOpenShell validator: pass/fail, issue
counts by class and severity, and the first `max_issues` issues with entity
ids. `express_rules` adds the EXPRESS where-rules (much slower on big models).

### validate_ids

| arg | type | default |
| --- | ---- | ------- |
| `ids_path` | path to an IDS XML file | required |
| `max_failures` | int 1-500 | 50 |

Checks the model against a buildingSMART IDS (Information Delivery
Specification): per-specification pass/fail with the failing GlobalIds and
the reason each requirement failed. Needs the optional `ifctester` package
(`pip install 'ifc-console[validation]'`); without it the error hint explains
the install.

### compute_quantities

| arg | type | default |
| --- | ---- | ------- |
| `selector` | selector string | required |
| `aggregate_by` | class, type, storey, material, none | class |
| `quantities` | list of quantity names | all numeric |

Quantity takeoff from stored quantity sets (`Qto_*`): per-group sums and
grand totals with the model's units. Elements without stored quantities are
counted and reported; a geometry fallback is planned as an opt-in step.

### detect_clashes

| arg | type | default |
| --- | ---- | ------- |
| `set_a` | selector string | required |
| `set_b` | selector string | set_a against itself |
| `mode` | overlap, clearance | overlap |
| `tolerance` | metres, 0 to 10 | 0.01 |
| `precision` | sampled, fast, exact (legacy alias) | sampled |
| `physical_only` | bool | true |
| `max_elements` | int 1-5000 | 1000 |
| `max_results` | int 1-1000 | 200 |
| `model` | model id for `set_a` | active model |
| `other_model` | model id for `set_b` | same as `model` |

Geometric clash detection between two element sets, optionally across two open
models for federated coordination. `overlap` reports solids that share space,
each with the shared volume in cubic metres, the deepest penetration, and the
centre of the overlap. `clearance` reports pairs that do not touch but sit
closer than `tolerance`, measured between bounding boxes; touching pairs are
included at gap 0.

Openings, spaces, annotations, grids and virtual elements are skipped by
default: they share space with real elements by design. Set
`physical_only=false` to include them.

`precision="sampled"` runs a bounded point-in-solid occupancy test and estimates
shared volume. It is more selective than bounding boxes, but remains approximate
and can miss thin intersections. `precision="exact"` is accepted as a legacy
alias and reports `precision: "sampled"`; it does not claim exact geometry.
`precision="fast"` stops at bounding boxes, which is quicker on very large sets
but reports every box overlap, including pairs that do not really touch.
Clearance is always a bounding-box measurement and reports
`precision: "bounding_box"`.

The response carries a `global_ids` list, so the usual next call is
`highlight_elements` with it to see the clashes in the viewer.

!!! note "Why this is computed here"
    IfcOpenShell's own `tree.clash_*` functions need an OpenCASCADE build, and
    the wheels ifc-console installs do not ship one (`ifcopenshell.geom.has_occ`
    is false). Overlap is therefore computed from world-space triangle meshes,
    which needs no extra dependency.

### get_georeferencing

No arguments. Coordinate reference system, map conversion parameters, true
and grid north. Answers "where is this model really".

### export_csv

| arg | type | default |
| --- | ---- | ------- |
| `selector` | selector string | required |
| `path` | target path ending in `.csv` | required |
| `fields` | list | name, storey, type_name |
| `properties` | dotted pset columns, e.g. `Pset_WallCommon.FireRating` | [] |
| `limit` | int 1-100000 | 10000 |
| `overwrite` | bool | false |

Writes query results to a CSV file. Allowed in ask mode: writing a report
file is not editing the model. The path must lie inside the allowed
directories, and every write is audited (`artifact_write`).

## Structured change previews

### preview_property_change

Builds an immutable, revision-bound ChangeSet without modifying model bytes.
Pass `global_ids`, `pset_name`, `property_name`, and a scalar `value`.
`create_missing=false` updates only an existing occurrence
`IfcPropertySingleValue`. Set it explicitly to true to preview creating a
missing occurrence property or property set. `nominal_type` is optional for
creation and accepts IFC value types such as `IfcLabel` or
`IfcLengthMeasure`; common non-null scalars can be inferred.

### preview_classification_assignment

Pass `global_ids`, `classification_name`, `identification`, and
`reference_name`. The preview assigns one direct occurrence classification
reference, reusing the exact system/reference or declaring their candidate
creation. Duplicate systems/references and existing direct assignments fail
closed.

### get_change_set

Reads either kind of verified ChangeSet by its content-addressed ID. Agent
tools can preview and inspect changes, but cannot approve, commit, restore, or
change the execution mode. Those authority-bearing actions remain direct
caller SDK/CLI workflows.

## Execution

### execute_ifc_code

| arg | type | notes |
| --- | ---- | ----- |
| `code` | str | Python source; no `bpy`, this is not Blender |
| `description` | str <= 200 chars | one-line intent, shown in the terminal and audit log |

Runs Python against the loaded model with `ifc`, `ifcopenshell`, `ifc_api`,
`element_util`, `selector_util`, `unit_util`, `query(sel)`, and `get_ifc_file()`
pre-injected. stdout is captured; a final bare expression is returned like a
REPL. Gating per the [safety model](safety.md).

Read-only runs execute in an isolated process with no network, no subprocesses,
and no file access outside the model directories; mutating runs stay in-process
so the edit reaches the live model. `sandboxed` in the output says which path
ran, and `note` explains any fallback. See [Code sandbox](sandbox.md).

Output fields: `stdout`, `result`, `classification`, `mutated`, `sandboxed`,
`duration_ms`, and `note` when there is something to say. Errors:
`ASK_MODE_BLOCKED`, `EXEC_BLOCKED` (guard or sandbox policy), `EXEC_ERROR`
(with traceback), `EXEC_TIMEOUT`, `SANDBOX_UNAVAILABLE` (only when
`sandbox.mode` is `strict`).

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

## Workspace (more than one file)

One active model is the default. These tools cover the optional second mode:
finding files in the user's folders, and holding extra models and companion
files alongside the active one. **Only the active model is writable**:
`save_ifc_file` and `execute_ifc_code` take no `model` argument, so
`set_active_model` is how you choose what a write affects.

Every read tool above (`get_ifc_project_info`, `get_spatial_structure`,
`query_elements`, `get_element`, `get_psets`, `validate_model`, `validate_ids`,
`compute_quantities`, `detect_clashes`, `get_georeferencing`) takes an optional
`model` argument naming a `model_id`. Omit it and you read the active model, as
always. When a read targets another model the envelope carries
`meta.read_from`; `meta.model` keeps naming the active one.

`detect_clashes` goes further and takes a second `other_model`, so one call can
clash the architectural model against the structural one. Each set is read on
the worker that owns its model, so a cross-model check never reaches into
another model's thread.

### find_files

| arg | type | default |
| --- | ---- | ------- |
| `query` | str or null | null (list everything) |
| `kinds` | list of ifc, ids, bcf, csv, ifcjson | all |
| `limit` | int 1-200 | 30 |
| `refresh` | bool | false |

Ranked candidates from the allowed folders with path, kind, size, IFC schema or
IDS spec count, and a guessed discipline (ARC, STR, MEP) parsed from ISO 19650
style names. Searching by discipline word works: `architecture` finds a file
whose only clue is the role letter `A`.

This tool **opens nothing**. When two candidates score about equally it sets
`meta.ambiguous: true`; ask the user which file they meant rather than guessing
between revisions.

### list_models

No arguments. Every resident model (`model_id`, name, schema, active, writable,
dirty) plus every attached companion file, and the memory budget.

### attach

| arg | type | default |
| --- | ---- | ------- |
| `path` | str | required |
| `alias` | str or null | derived from the file name |

Adds a file alongside the active model without replacing it. An IFC becomes an
extra read-only model; an IDS, BCF, or CSV becomes a companion file whose path
the tools that read it can use. Allowed in ask mode: attaching is reading.

`model_id` values are meant to be retyped, so a readable stem is kept
(`tower-structure`) and an opaque code falls back to its discipline
(`ABC-XYZ-ZZ-XX-M3-S-0001.ifc` becomes `str`).

### detach

`id`: a `model_id` or an attachment alias. Refuses a model with unsaved changes.

### set_active_model

`model_id`. Moves the write focus. The model that was active stays resident,
read-only, and keeps any unsaved changes.

`validate_ids` accepts an attachment alias wherever it takes `ids_path`, so an
attached IDS needs no path repeated.

## Viewer (optional category)

The viewer is not part of the core surface. These four tools are **registered
only while the viewer is enabled** (`--viewer` at launch or `/viewer` in the
console). Enable it mid-session and they join the tool list live; `/viewer off`
removes them (clients pick up the change on their next tool refresh). Sessions
without the viewer, including all stdio sessions, expose exactly the 24 core
tools above.

When registered, all four still need a connected browser tab, or they return
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

### apply_color_theme

| arg | type | default |
| --- | ---- | ------- |
| `groups` | list of `{label, global_ids, color?}` (max 24) | required unless clearing |
| `title` | str <= 80 | "" |
| `clear` | bool | false |

Paints elements by group with a legend: the LLM computes the grouping (by
storey, type, material, a pset value, pass/fail), the viewer colors it, the
user reads it. Groups without an explicit `color` get colorblind-safe palette
colors; the legend always carries labels and counts, so meaning never rides
on color alone. Late-joining tabs receive the active theme automatically.

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
| `SANDBOX_UNAVAILABLE` | `sandbox.mode` is strict and this run could not be sandboxed |
| `MODEL_BUSY` | session paused after a timeout; user reloads |
| `MODEL_TOO_LARGE` | over the `files.max_open_mb` (or viewer) size budget |
| `MODEL_NOT_FOUND` | no resident model with that `model_id`; call `list_models` |
| `MODEL_READ_ONLY` | write aimed at an attached model; only the active one is writable |
| `NOT_FOUND` | a named thing (element root, schema entity, attachment) does not exist |
| `NO_MATCH` / `NO_GEOMETRY` | a clash selector matched nothing, or nothing with solid geometry |
| `TOO_MANY_ELEMENTS` | a clash selector matched more than `max_elements`; narrow it |
| `WORKSPACE_BUDGET` | over `workspace.max_resident` or `workspace.max_total_mb` |
| `WORKSPACE_DISABLED` | workspace indexing is disabled in settings |
| `CONSOLE_NOT_RUNNING` / `CONSOLE_AUTH_FAILED` | the stdio bridge cannot reach or authenticate to the console |
| `EXTRA_NOT_INSTALLED` | an optional package is needed; the hint names the install |
| `VIEWER_NOT_CONNECTED` / `VIEWER_TIMEOUT` / `VIEWER_ERROR` | viewer tools without a healthy tab |
| `RESULT_TOO_LARGE` | narrow the request |
| `INTERNAL_ERROR` | a bug; the audit log has details |

## Resources and prompts

Beyond tools, the server exposes MCP **resources**, JSON views clients can
attach as ambient context: `ifc://model/summary`, `ifc://model/spatial-tree`,
`ifc://session/audit`, and the template `ifc://element/{global_id}`.

It also ships a small **prompt library**, guided workflows that appear as
slash-command-style prompts in capable clients: `model_audit`, `qto_report`,
`explain_element`, `find_unclassified`, `validate_against_ids`, and
`selector_help`.
