# MCP tools

Use `/tools ai` for the live tool list and exact input schemas. It reflects the
current mode, plugins, and viewer state. This page explains what each built-in
tool is for.

Start a new session with `orient`, then use narrower tools as needed.

## Response format

Every tool returns the same envelope:

```json
{"ok": true, "data": {}, "meta": {"mode": "ask", "model": "x.ifc"}}
```

Failures return data rather than protocol errors:

```json
{"ok": false, "error": {"code": "ASK_MODE_BLOCKED", "message": "...", "hint": "..."}}
```

Large results may set `meta.truncated=true`. Narrow the query or use pagination.
`get_viewer_screenshot` is the only tool that returns image content instead of
the normal structured envelope.

## Model queries

| tool | main input | result |
| ---- | ---------- | ------ |
| `get_session_status` | none | version, model, mode, dirty state, and viewer status |
| `orient` | none | status, project summary, and a shallow spatial tree |
| `describe_capabilities` | none | live tools, permissions, and examples |
| `get_ifc_project_info` | optional `model` | schema, units, project counts, materials, classifications, and header data |
| `get_spatial_structure` | root, depth, counts | Project to Site to Building to Storey to Space tree |
| `search_elements` | name, GlobalId, text, or simple selector | human-friendly element matches |
| `query_elements` | selector, limit, offset, fields, order | paged element summaries |
| `get_element` | up to 50 GlobalIds | attributes, properties, quantities, materials, type, and container |
| `get_psets` | up to 100 GlobalIds | property and quantity sets only |
| `get_schema_docs` | entity, pset, or property | schema definitions; works without an open model |

Use `search_elements` when the user says a name such as `Wall-1` or supplies a
GlobalId. It performs case-insensitive text matching; a bare IFC class or input
containing `=` is treated as a selector. Use `query_elements` when the agent
already has deliberate IfcOpenShell selector syntax:

```text
IfcWall
IfcWall, IfcSlab
IfcWall, material=concrete
IfcWall, Pset_WallCommon.FireRating=F30
IfcElement, Name=/W.*1/
```

The default page size is 50. Syntax errors return `INVALID_QUERY` with a short
selector guide.

Model-reading tools such as project info, spatial structure, element queries,
validation, quantities, clashes, and georeferencing accept an optional resident
`model` ID. Omit it to use the active model.

## Knowledge

The knowledge index is local and built from the installed IfcOpenShell package.

| tool | use |
| ---- | --- |
| `search_ifc_knowledge` | search entities, property sets, properties, types, APIs, recipes, and ingested project documents |
| `get_knowledge_record` | read one result by its returned key |
| `list_project_documents` | list indexed project documents and images with provenance and hashes |
| `get_project_reference_image` | load one indexed project image as native vision input |
| `get_project_document_page` | render one indexed PDF page as native vision input for diagrams, scans, and layout |
| `get_api_docs` | get an exact `ifcopenshell.api` signature or search API names |
| `get_measurement_recipe` | the project's measurement method for one class and property, with its citation |

`search_ifc_knowledge` accepts plain words, an optional kind and schema, a
result limit, and a `corpus` (`builtin`, `project`, or `all`). See
[Knowledge index](knowledge.md) for the project document corpus and
[Building agent applications](agents.md#measurement-recipes) for recipes.

## Analysis

| tool | key inputs | result |
| ---- | ---------- | ------ |
| `validate_model` | `express_rules=false`, `max_issues=200` | schema validity and grouped issues |
| `validate_ids` | IDS path, failure limit | buildingSMART IDS results; needs the validation extra |
| `compute_quantities` | selector, grouping, quantity names, `source` | stored `Qto_*` totals in model units; `source="derived"` fills missing values from geometry |
| `detect_clashes` | two selectors, tolerance, precision, optional model IDs | overlap or clearance pairs |
| `get_element_geometry` | selector or GlobalIds | per-element mesh geometry in SI metres: bounding box, placement-axis extents, footprint, volume, confidence |
| `analyze_element_geometry` | selector or GlobalIds, stations, `include_outline` | the full probe for a few elements: exact profile parameters from the IFC definition plus measured mesh cross sections (width, height, wall thickness distribution, perimeter, area), merged into `dimensions` with a named source per value |
| `measure_elements` | selector or GlobalIds, method, method params | one metric per element by an explicit method (`stored_qto`, `layer_sum`, `geometry_extent`), in file units and SI |
| `measure_distance` | two selectors or GlobalId lists | centroid distance, bounding-box gap, and closest-surface distance of the nearest pair |
| `get_georeferencing` | none | CRS, map conversion, and north directions |
| `export_csv` | selector, path, fields, properties | audited CSV report inside an allowed directory |
| `export_measurement_report` | selector or GlobalIds, path, title, notes | audited markdown measurement report, registered as an artifact |

`analyze_element_geometry` is the "measure everything about this object" tool.
It reads parameterized profiles (I, U, Z, C, T, L, rectangle, circle, hollow
variants) and arbitrary profile outlines from the file, slices the triangle
mesh across the element's long axis, and reports both sides: agreement is the
cross-check, disagreement over 5% raises a `mismatch:*` flag, and a wall-style
element whose extrusion axis is not its long axis is flagged
`profile_plane_differs` instead of being force-compared. Thickness comes as a
distribution (median and quartiles) plus a two-group split when flange and web
plates differ, matching the `t_f`/`t_w` convention of profile drawings.

Clash precision choices:

- `sampled` (default) checks triangle meshes and estimates overlap;
- `fast` uses bounding boxes and may report false positives;
- clearance always measures bounding-box gaps.

Openings, spaces, grids, annotations, and virtual elements are skipped unless
`physical_only=false`. Cross-model checks use `model` and `other_model`.

## Model insight

Whole-model questions rather than per-element ones. All three are reads and work
in `ask` mode.

| tool | key inputs | result |
| ---- | ---------- | ------ |
| `compare_models` | `other_model`, optional selector and tolerances | what changed between two open revisions, grouped by change kind and class |
| `query_spatial` | relation, target GlobalId or box, candidate selector | elements standing in a geometric relation to that target |
| `check_model_health` | optional `checks`, `max_findings`, `max_elements` | data-quality findings grouped by check, with severities and GlobalIds |

### `compare_models`

`model` is the older baseline, `other_model` the newer revision, so a reported
move is where the element went. Attach the second file first:

```text
attach path=... -> list_models -> compare_models other_model=<model_id>
```

Elements are paired by GlobalId. When the two files barely share ids, which is
what happens when an exporter regenerates them, the diff falls back to matching
on class, type, and name and reports `matcher="signature"` with a note. Change
kinds are `added`, `removed`, `moved`, `geometry_changed`, `property_changed`,
`type_changed`, and `container_changed`; one element can be in several at once.
Positions and volumes come from the triangle meshes in SI metres, so a move is a
real move rather than an edited placement, and `move_tolerance` (metres) and
`volume_tolerance` (a fraction) set what counts. Property comparison uses each
element's own property sets, not the values it inherits from its type, so a type
swap is reported once instead of once per property. `global_ids` groups the
change set per kind, ready for `apply_color_theme`.

### `query_spatial`

Answers containment that the selector's `location=` facet cannot, because most
exporters contain elements in the storey rather than the space:

| relation | question | method |
| -------- | -------- | ------ |
| `inside` | what is in this space or room | point-in-solid on the candidate's centroid and box corners |
| `crosses` | what this duct or pipe passes through | sampled solid occupancy, with `enclosed` marking what only sits inside |
| `above`, `below` | what sits directly over or under it | plan overlap of bounding boxes, nearest gap first |
| `within_distance` | what is within N metres | closest-point surface distance |
| `within_box` | what falls inside these bounds | bounding-box containment, exact bounds or the target's own box |

Every result names the `method` it used and a `confidence`. Point-in-solid tests
are unreliable on meshes that are not closed, so the `target` block reports
`watertight` and the result drops to low confidence when it is false. `distance`
is the reach for `within_distance` and the gap cap for `above` and `below`.

### `check_model_health`

Checks, in report order: `duplicate_global_ids`, `orphaned_elements`,
`degenerate_solids`, `placement_outliers`, `duplicate_placements`,
`model_extent`, `empty_storeys`, `unused_types`. Each finding carries a
severity, a count, examples, and `global_ids` for `highlight_elements`.

This is not `validate_model`. That one runs the schema: attribute types,
cardinality, uniqueness, and where-rules. A file can satisfy all of it and still
have elements in no spatial container, representations with no solid, a beam
forty kilometres from site, the same wall modelled twice, or an extent that
contradicts its declared length unit.

The four geometry checks tessellate the model. Above `max_elements` they are
skipped and say so in `checks`, rather than silently sampling. Pass `checks` to
run only the cheap ones.

## Jobs and artifacts

Use jobs for validation that should not block a client connection.

```text
submit_validation_job -> get_job -> artifact metadata
                              \-> cancel_job
```

| tool | use |
| ---- | --- |
| `submit_validation_job` | start schema and optional IDS validation in a restricted worker |
| `get_job` | read state, phase, progress, events, failures, and artifacts |
| `list_jobs` | list recent durable jobs |
| `cancel_job` | request cancellation before a transaction commit point |
| `list_artifacts` | list content-addressed output metadata |
| `get_artifact` | verify one artifact's metadata |

Validation jobs require a clean model because the worker verifies the file on
disk. MCP returns artifact metadata only; export bytes through the SDK or
`ifc-console artifacts export`.

## Structured change previews

```text
AI tool: preview -> inspect ChangeSet
Human SDK/CLI: approve -> commit -> optional restore
```

| tool | use |
| ---- | --- |
| `preview_property_change` | preview one property value across selected elements |
| `preview_classification_assignment` | preview a direct classification assignment |
| `get_change_set` | inspect a stored revision-bound ChangeSet |
| `list_ai_authored_properties` | inventory every AI-authored value in the model |

Previewing changes does not modify the model. AI-visible tools cannot approve,
commit, restore, or change the mode. Those actions remain direct SDK and CLI
operations.

Property creation requires `create_missing=true`. Set `nominal_type` when the
exact IFC value type cannot be inferred.

Values written on an agent's behalf go only into the reserved `IfcConsole_AI_`
property sets and carry an `AI_Provenance` record naming the agent, model,
method, and source document. `list_ai_authored_properties` returns that
inventory, so the AI-assisted layer stays separable from the authored model by
prefix match. See [Marking what the model wrote](agents.md#marking-what-the-model-wrote).

## Generated Python

### `execute_ifc_code`

Inputs:

- `code`: Python source;
- `description`: a short intent recorded in the activity feed and audit log.

The environment provides `ifc`, `ifcopenshell`, `ifc_api`, common IfcOpenShell
utilities, `query(selector)`, and `get_ifc_file()`.

Query code runs in `ask` or `edit`. Mutating code requires `edit`. Eligible
read-only code uses the sandbox; the response reports `sandboxed` and explains
any fallback. See [Safety](safety.md) and [Code sandbox](sandbox.md).

The result includes stdout, the final expression, classification, mutation
state, sandbox state, duration, and any note.

## Files and workspace

| tool | use |
| ---- | --- |
| `list_ifc_files` | list IFC files in allowed directories and recents |
| `open_ifc_file` | replace the active model; refuses unsaved changes |
| `save_ifc_file` | atomic save with backup; requires edit mode and AI-save opt-in |
| `find_files` | search supported files without opening them |
| `list_models` | list resident models, companion files, and memory budget |
| `attach` | add a read-only IFC or companion IDS, BCF, or CSV |
| `detach` | release an attachment; refuses dirty models |
| `set_active_model` | move the writable focus to another resident model |

Only the active model is writable. Attached IFC files remain read-only.
`detect_clashes` can compare two resident models, and `validate_ids` accepts an
attached IDS alias.

Paths must stay inside the launch directory, model directory, or an explicitly
allowed root.

## Skill tools

Skills are reusable procedures saved as markdown in
`.ifc-console/agents/skills/`, one file per skill with a small front-matter
header (`name`, `description`, `applies_to`). Agents check them at the start
of a task and follow the one that matches instead of rediscovering a method;
after solving a novel task, an agent can offer to record the method. The
files are plain markdown, so they can also be written and reviewed by hand,
and they appear in the panel's Skills tab.

| tool | use |
| ---- | --- |
| `list_agent_skills` | the saved skills with descriptions and applicability |
| `get_agent_skill` | one skill's full markdown procedure |
| `save_agent_skill` | record a new skill, or update one with `overwrite=true` |

`save_agent_skill` carries the `file:write` capability, so agent surfaces ask
the user for approval before a skill lands on disk.

## Viewer tools

These tools exist only while the optional viewer is enabled and a browser tab
is connected. One launcher is always registered so an agent can get there
itself:

| tool | use |
| ---- | --- |
| `open_viewer` | always on: enable the viewer and open it in the local browser, so the tools below appear |
| `get_viewer_selection` | read the user's selected elements |
| `get_viewer_measurements` | read every measurement taken, by the user or by you |
| `highlight_elements` | color, isolate, clear, or frame up to 500 elements |
| `apply_color_theme` | paint labeled groups and show a legend |
| `get_viewer_screenshot` | capture a preset or current view as JPEG or PNG |
| `control_viewer` | section, orient, select, isolate, hide, focus tabs, measure, and save viewpoints |

All viewer tools are visual and allowed in either mode. `control_viewer`
measures against the geometry on screen, which answers questions the schema
does not: a rotated wall's real thickness, the clear distance between two
elements, the area inside an outline. Its `focus` action opens elements alone
in a named tab under the viewer's top bar, so one object can be analyzed with
the user watching the same thing; `unfocus` closes tabs. See
[3D viewer](viewer.md).

## Error codes

Every failure includes a `hint`. The table groups codes with the same recovery.

| code | meaning |
| ---- | ------- |
| `APPROVAL_MISMATCH` / `APPROVAL_NOT_FOUND` / `APPROVAL_REQUIRED` | approval is missing or does not match the ChangeSet |
| `ARTIFACT_CORRUPT` / `ARTIFACT_NOT_FOUND` / `ARTIFACT_EXPORT_FAILED` | artifact is missing, invalid, or cannot be exported |
| `ARTIFACT_GC_CONFLICT` / `ARTIFACT_GC_FAILED` / `ARTIFACT_STORE_BUSY` / `ARTIFACT_STORE_CORRUPT` | artifact storage or collection failed safely |
| `ASK_MODE_BLOCKED` / `AI_SAVE_DISABLED` / `CAPABILITY_DENIED` | current mode, save policy, or authority does not allow the operation |
| `BATCH_NOT_FOUND` / `BATCH_NOT_RESUMABLE` / `BATCH_SERVICE_CLOSED` / `BATCH_STORE_FAILED` / `BATCH_SOURCE_CHANGED` / `BATCH_TIMEOUT` | batch is unavailable, stale, or failed |
| `CHANGESET_INVALID` / `CHANGESET_NOT_FOUND` | ChangeSet is invalid or unknown |
| `CHAT_FAILED` | provider, stream, or chat tool loop failed |
| `COMMIT_FAILED` / `COMMIT_NOT_FOUND` | commit failed or is unknown |
| `CONSOLE_AUTH_FAILED` / `CONSOLE_NOT_RUNNING` | bridge cannot authenticate to or reach the console |
| `EXEC_BLOCKED` / `EXEC_ERROR` / `EXEC_TIMEOUT` | generated code was denied, failed, or timed out |
| `EXTRA_NOT_INSTALLED` | an optional dependency is missing |
| `FILE_EXISTS` / `FILE_NOT_FOUND` | destination exists or source is missing |
| `INTERNAL_ERROR` | unexpected product error; inspect local logs |
| `INVALID_INPUT` / `INVALID_OUTPUT` / `INVALID_QUERY` | arguments, output, or selector syntax is invalid |
| `JOB_CANCELLED` / `JOB_NOT_CANCELLABLE` / `JOB_NOT_FOUND` / `JOB_RESULT_INVALID` / `JOB_SPEC_INVALID` / `JOB_SERVICE_CLOSED` / `JOB_WORKER_FAILED` / `JOB_TIMEOUT` | durable job was cancelled, unavailable, invalid, or failed |
| `KNOWLEDGE_DISABLED` / `KNOWLEDGE_NOT_READY` | knowledge search is disabled or still building |
| `MODEL_BUSY` | timed-out model worker requires `/reload` |
| `MODEL_NOT_FOUND` / `NO_MODEL_LOADED` / `MODEL_READ_ONLY` / `MODEL_TOO_LARGE` | requested model is missing, read-only, or over budget |
| `NOT_FOUND` / `PROPERTY_NOT_FOUND` / `NO_GEOMETRY` / `NO_MATCH` | requested data or usable geometry was not found |
| `PATH_NOT_ALLOWED` | path is outside allowed directories |
| `RESTORE_CONFLICT` / `RESTORE_NOT_FOUND` | restore is stale or unknown |
| `RESULT_TOO_LARGE` / `TOO_MANY_ELEMENTS` | narrow the request |
| `REVISION_CONFLICT` / `SOURCE_CHANGED` | source changed after planning |
| `SANDBOX_UNAVAILABLE` | strict sandbox worker is unavailable |
| `TRANSACTION_INTERRUPTED` / `TRANSACTION_RECOVERY_REQUIRED` / `TRANSACTION_JOURNAL_BUSY` / `TRANSACTION_JOURNAL_CORRUPT` / `TRANSACTION_JOURNAL_INVALID` | transaction stopped or its journal needs recovery |
| `UNSAVED_CHANGES` | operation would discard dirty model state |
| `VALIDATION_FAILED` | validation could not complete |
| `VIEWER_BUSY` / `VIEWER_ERROR` / `VIEWER_NOT_CONNECTED` / `VIEWER_TIMEOUT` / `VIEWER_UNAVAILABLE` | viewer is reloading the model, unavailable, off this transport, or failed to answer |
| `WORKFLOW_CANCELLED` / `WORKFLOW_NOT_FOUND` / `WORKFLOW_NOT_RESUMABLE` / `WORKFLOW_DEPENDENCY_FAILED` / `WORKFLOW_STEP_FAILED` / `WORKFLOW_SUPERVISOR_FAILED` / `WORKFLOW_INPUT_EMPTY` / `WORKFLOW_INPUT_LIMIT` / `WORKFLOW_INTERRUPTED` / `WORKFLOW_TIMEOUT` / `WORKFLOW_MANIFEST_INVALID` / `WORKFLOW_MANIFEST_TOO_LARGE` / `WORKFLOW_PATH_INVALID` / `WORKFLOW_SERVICE_CLOSED` / `WORKFLOW_STORE_CORRUPT` / `WORKFLOW_STORE_FAILED` / `WORKFLOW_SOURCE_CHANGED` | workflow is invalid, stale, unavailable, or failed |
| `WORKSPACE_BUDGET` / `WORKSPACE_DISABLED` | workspace indexing is disabled or over budget |

## Resources and prompts

Resources: `ifc://model/summary`, `ifc://model/spatial-tree`,
`ifc://session/audit`, and `ifc://element/{global_id}`.

Prompts: `model_audit`, `qto_report`, `explain_element`, `find_unclassified`,
`validate_against_ids`, and `selector_help`.
