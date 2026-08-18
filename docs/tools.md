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
| `search_ifc_knowledge` | search entities, property sets, properties, types, APIs, and recipes |
| `get_knowledge_record` | read one result by its returned key |
| `get_api_docs` | get an exact `ifcopenshell.api` signature or search API names |

`search_ifc_knowledge` accepts plain words, an optional kind and schema, and a
result limit. See [Knowledge index](knowledge.md).

## Analysis

| tool | key inputs | result |
| ---- | ---------- | ------ |
| `validate_model` | `express_rules=false`, `max_issues=200` | schema validity and grouped issues |
| `validate_ids` | IDS path, failure limit | buildingSMART IDS results; needs the validation extra |
| `compute_quantities` | selector, grouping, quantity names | stored `Qto_*` totals in model units |
| `detect_clashes` | two selectors, tolerance, precision, optional model IDs | overlap or clearance pairs |
| `get_georeferencing` | none | CRS, map conversion, and north directions |
| `export_csv` | selector, path, fields, properties | audited CSV report inside an allowed directory |

Clash precision choices:

- `sampled` (default) checks triangle meshes and estimates overlap;
- `fast` uses bounding boxes and may report false positives;
- clearance always measures bounding-box gaps.

Openings, spaces, grids, annotations, and virtual elements are skipped unless
`physical_only=false`. Cross-model checks use `model` and `other_model`.

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

Previewing changes does not modify the model. AI-visible tools cannot approve,
commit, restore, or change the mode. Those actions remain direct SDK and CLI
operations.

Property creation requires `create_missing=true`. Set `nominal_type` when the
exact IFC value type cannot be inferred.

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

## Viewer tools

These tools exist only while the optional viewer is enabled and a browser tab
is connected.

| tool | use |
| ---- | --- |
| `get_viewer_selection` | read the user's selected elements |
| `highlight_elements` | color, isolate, clear, or frame up to 500 elements |
| `apply_color_theme` | paint labeled groups and show a legend |
| `get_viewer_screenshot` | capture a preset or current view as JPEG or PNG |

All viewer tools are visual and allowed in either mode. See [3D viewer](viewer.md).

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
| `VIEWER_ERROR` / `VIEWER_NOT_CONNECTED` / `VIEWER_TIMEOUT` | viewer is unavailable or failed to answer |
| `WORKFLOW_CANCELLED` / `WORKFLOW_NOT_FOUND` / `WORKFLOW_NOT_RESUMABLE` / `WORKFLOW_DEPENDENCY_FAILED` / `WORKFLOW_STEP_FAILED` / `WORKFLOW_SUPERVISOR_FAILED` / `WORKFLOW_INPUT_EMPTY` / `WORKFLOW_INPUT_LIMIT` / `WORKFLOW_INTERRUPTED` / `WORKFLOW_TIMEOUT` / `WORKFLOW_MANIFEST_INVALID` / `WORKFLOW_MANIFEST_TOO_LARGE` / `WORKFLOW_PATH_INVALID` / `WORKFLOW_SERVICE_CLOSED` / `WORKFLOW_STORE_CORRUPT` / `WORKFLOW_STORE_FAILED` / `WORKFLOW_SOURCE_CHANGED` | workflow is invalid, stale, unavailable, or failed |
| `WORKSPACE_BUDGET` / `WORKSPACE_DISABLED` | workspace indexing is disabled or over budget |

## Resources and prompts

Resources: `ifc://model/summary`, `ifc://model/spatial-tree`,
`ifc://session/audit`, and `ifc://element/{global_id}`.

Prompts: `model_audit`, `qto_report`, `explain_element`, `find_unclassified`,
`validate_against_ids`, and `selector_help`.
