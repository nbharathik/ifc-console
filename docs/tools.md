---
description: Every MCP tool by group, the response envelope, selectors, and error codes.
---

# MCP tools

`/tools ai` in the console shows the live catalog with exact input schemas.
Start a session with `orient`, then use the narrowest tool that fits.

## Response format

```json
{"ok": true, "data": {}, "meta": {"mode": "ask", "model": "x.ifc"}}
```

```json
{"ok": false, "error": {"code": "ASK_MODE_BLOCKED", "message": "...", "hint": "..."}}
```

Every error carries a recovery `hint`. Large results set `meta.truncated`;
narrow or paginate the request.

## Tool profiles

A client that lists every tool pays for every description in every session.
The `lean` profile lists 14 tools and lets the model search for the rest;
`full` lists everything. A profile only changes what `tools/list` shows: every
tool stays callable by name, and mode and permissions apply the same way.

| Tool | Purpose |
| :--- | :--- |
| `find_tools` | search the live catalog in plain words; returns matches with their argument shapes |
| `call_tool` | run a tool `find_tools` returned; tools that delete or overwrite are not run this way |

Pick a profile per client with a header (`X-IFC-Console-Tools: lean`), a path
(`/mcp/lean`), or the `mcp.tool_profile` setting (default `full`). The bridge
takes `--tools lean`. Both discovery tools are hidden in `full`, where they
would only repeat what is listed.

## Session and model

| Tool | Purpose |
| :--- | :--- |
| `orient` | status, project summary, and spatial tree in one call |
| `get_session_status` | version, model, mode, dirty state, viewer state |
| `describe_capabilities` | live operations, permissions, and examples |
| `get_ifc_project_info` | schema, units, counts, materials, header |
| `get_spatial_structure` | Project to Site to Building to Storey to Space |
| `search_elements` | names, GlobalIds, text, or simple selectors |
| `query_elements` | paged selector results with chosen fields |
| `get_element` | attributes, properties, type, materials, container |
| `get_psets` | property and quantity sets |
| `get_schema_docs` | IFC entity, pset, or property definitions |

Selectors follow IfcOpenShell syntax:

```text
IfcWall
IfcWall, IfcSlab
IfcWall, material=concrete
IfcWall, Pset_WallCommon.FireRating=F30
IfcElement, Name=/W.*1/
```

## Knowledge

| Tool | Purpose |
| :--- | :--- |
| `search_ifc_knowledge` | offline search across the schema, psets, the ifcopenshell API, and project documents |
| `get_knowledge_record` | one result by key |
| `get_api_docs` | the exact signature of an `ifcopenshell.api` call |
| `list_project_documents` | indexed documents and images |
| `get_project_document_page` | one PDF page as an image |
| `get_project_reference_image` | one indexed project image as vision input |
| `lookup_table_rows` | rows of an indexed table, filtered or ranked by nearest numeric match |
| `get_measurement_recipe` | the project method and citation for one property |

## Analysis and measurement

| Tool | Purpose |
| :--- | :--- |
| `validate_model` | schema validation with grouped issues |
| `validate_ids` | buildingSMART IDS validation; needs `[validation]` |
| `check_model_health` | duplicates, orphans, placement, and storey defects |
| `audit_element_properties` | expected versus present properties per element |
| `assess_model_quality` | a 0 to 100 scorecard with ordered improvements |
| `compute_quantities` | stored or geometry-derived quantities by group |
| `detect_clashes` | overlaps and clearances between two sets |
| `compare_models` | changes between two open revisions |
| `query_spatial` | inside, above, below, within distance, within box |
| `get_element_geometry` | bounding box, axes, footprint, volume |
| `analyze_element_geometry` | full parametric inventory of one or a few elements |
| `measure_elements` | length, width, thickness, area, volume |
| `measure_distance` | distance between two elements |
| `measure_local_thickness` | material and void intervals through a point |
| `measure_directional_extent` | outside-to-outside mesh extent along any 3D direction |
| `inspect_element_mesh` | watertightness, winding, components, and other raw-mesh health |
| `slice_element_mesh` | cut one mesh with a plane: area, perimeter, thickness, outline |
| `get_georeferencing` | CRS, map conversion, and north |
| `export_csv` | CSV inside an allowed directory |
| `export_measurement_report` | a Markdown report saved as an artifact |

`analyze_element_geometry` is the default for one or a few objects. Pass the
`model_id` from `get_viewer_selection` when the selection came from the viewer.

## Changes

| Tool | Purpose |
| :--- | :--- |
| `preview_property_change` | preview one value on selected elements |
| `preview_property_changes` | preview up to 16 values as one ChangeSet |
| `preview_classification_assignment` | preview a classification |
| `get_change_set` | inspect a ChangeSet |
| `list_ai_authored_properties` | values written under `IfcConsole_AI_` |
| `set_properties` | set property values by GlobalId, all or nothing; `dry_run` previews |
| `execute_ifc_code` | run Python against the model; mutations need edit mode |

Previews never modify the model. AI tools cannot approve, commit, save the
opened file, or change the mode. Generated code sees `ifc`, `ifcopenshell`,
`ifc_api`, `query(selector)`, and the usual utilities; read-only code runs in
the [sandbox](safety.md#generated-code).

### Edits are undoable steps

Each `set_properties` call and each mutating `execute_ifc_code` run is one
step. If it fails, everything it changed is rolled back and the result says so
(`rolled_back`, `verified`). A step that changes nothing leaves the model
clean. A successful step reports what it touched in `data.change`: counts, the
GlobalIds, and whether geometry changed. The user steps back and forward with
`/undo` and `/redo`; the last `edit.undo_depth` steps (default 20) are kept.
There is no tool for undo, and generated code that calls `ifc.undo()` is
refused.

## Files and models

| Tool | Purpose |
| :--- | :--- |
| `list_ifc_files` / `find_files` | allowed IFC and companion files |
| `open_ifc_file` | replace the active model |
| `save_ifc_file` | save; in edit mode this writes the working copy |
| `list_models` / `attach` / `detach` | resident models and attachments |
| `set_active_model` | move writable focus to a resident model |

## Jobs

| Tool | Purpose |
| :--- | :--- |
| `submit_validation_job` | run validation outside the client connection |
| `get_job` / `list_jobs` / `cancel_job` | inspect or cancel jobs |
| `list_artifacts` / `get_artifact` | verified output metadata |

## Viewer

| Tool | Purpose |
| :--- | :--- |
| `open_viewer` | turn the viewer on and open it |
| `get_viewer_selection` | selected elements, per model |
| `get_viewer_measurements` | everything measured so far |
| `highlight_elements` | color, isolate, and frame elements |
| `apply_color_theme` | labeled groups with a legend |
| `control_viewer` | views, camera, sections, focus, measurements, saved views |
| `get_viewer_screenshot` | an image of the scene |

## Skills

Registered by the built-in agents extension. Skills are Markdown procedures in
`.ifc-console/agents/skills/`.

| Tool | Purpose |
| :--- | :--- |
| `list_agent_skills` / `get_agent_skill` | list or read skills |
| `apply_measurement_skill` | dry-run a structured skill on selected elements |
| `save_agent_skill` | create or update a skill with host approval |

## Error codes

| Group | Codes |
| :--- | :--- |
| policy | `ASK_MODE_BLOCKED`, `CAPABILITY_DENIED`, `AI_SAVE_DISABLED`, `CONSOLE_NOT_RUNNING`, `CONSOLE_AUTH_FAILED`, `EXTRA_NOT_INSTALLED` |
| input | `INVALID_INPUT`, `INVALID_QUERY`, `INVALID_GEOMETRY`, `INVALID_IFC`, `INVALID_OUTPUT`, `NOT_FOUND`, `NO_MATCH`, `RESULT_TOO_LARGE`, `TOO_MANY_ELEMENTS`, `PROPERTY_NOT_FOUND` |
| files | `FILE_NOT_FOUND`, `FILE_EXISTS`, `PATH_NOT_ALLOWED`, `MODEL_TOO_LARGE`, `NO_MODEL_LOADED`, `MODEL_NOT_FOUND`, `MODEL_BUSY`, `UNSAVED_CHANGES`, `MODEL_READ_ONLY`, `SOURCE_CHANGED` |
| workspace | `WORKSPACE_BUDGET`, `WORKSPACE_DISABLED`, `STORE_BUSY` |
| code | `EXEC_BLOCKED`, `EXEC_ERROR`, `EXEC_TIMEOUT`, `SANDBOX_UNAVAILABLE` |
| undo | `NOTHING_TO_UNDO`, `NOTHING_TO_REDO`, `UNDO_FAILED` |
| geometry | `NO_GEOMETRY`, `GEOMETRY_ANALYSIS_FAILED`, `FRAME_UNAVAILABLE` |
| knowledge | `KNOWLEDGE_DISABLED`, `KNOWLEDGE_NOT_READY` |
| changes | `APPROVAL_REQUIRED`, `APPROVAL_MISMATCH`, `APPROVAL_NOT_FOUND`, `CHANGESET_INVALID`, `CHANGESET_NOT_FOUND`, `REVISION_CONFLICT`, `COMMIT_FAILED`, `COMMIT_NOT_FOUND`, `RESTORE_CONFLICT`, `RESTORE_NOT_FOUND`, `VALIDATION_FAILED` |
| transactions | `TRANSACTION_INTERRUPTED`, `TRANSACTION_JOURNAL_BUSY`, `TRANSACTION_JOURNAL_CORRUPT`, `TRANSACTION_JOURNAL_INVALID`, `TRANSACTION_RECOVERY_REQUIRED` |
| viewer | `VIEWER_NOT_CONNECTED`, `VIEWER_TIMEOUT`, `VIEWER_UNAVAILABLE`, `VIEWER_BUSY`, `VIEWER_ERROR` |
| jobs | `JOB_NOT_FOUND`, `JOB_CANCELLED`, `JOB_NOT_CANCELLABLE`, `JOB_TIMEOUT`, `JOB_SPEC_INVALID`, `JOB_RESULT_INVALID`, `JOB_WORKER_FAILED`, `JOB_SERVICE_CLOSED` |
| batches | `BATCH_NOT_FOUND`, `BATCH_CANCELLED`, `BATCH_CHILD_FAILED`, `BATCH_INTERRUPTED`, `BATCH_NOT_RESUMABLE`, `BATCH_SERVICE_CLOSED`, `BATCH_SOURCE_CHANGED`, `BATCH_STORE_FAILED`, `BATCH_SUPERVISOR_FAILED`, `BATCH_TIMEOUT` |
| workflows | `WORKFLOW_NOT_FOUND`, `WORKFLOW_CANCELLED`, `WORKFLOW_DEPENDENCY_FAILED`, `WORKFLOW_STEP_FAILED`, `WORKFLOW_INPUT_EMPTY`, `WORKFLOW_INPUT_LIMIT`, `WORKFLOW_INTERRUPTED`, `WORKFLOW_INVALID`, `WORKFLOW_MANIFEST_INVALID`, `WORKFLOW_MANIFEST_TOO_LARGE`, `WORKFLOW_NOT_RESUMABLE`, `WORKFLOW_PATH_INVALID`, `WORKFLOW_SERVICE_CLOSED`, `WORKFLOW_SOURCE_CHANGED`, `WORKFLOW_STORE_CORRUPT`, `WORKFLOW_STORE_FAILED`, `WORKFLOW_SUPERVISOR_FAILED`, `WORKFLOW_TIMEOUT` |
| artifacts | `ARTIFACT_NOT_FOUND`, `ARTIFACT_CORRUPT`, `ARTIFACT_EXPORT_FAILED`, `ARTIFACT_GC_CONFLICT`, `ARTIFACT_GC_FAILED`, `ARTIFACT_STORE_BUSY`, `ARTIFACT_STORE_CORRUPT` |
| other | `CHAT_FAILED`, `TOOL_SOURCE_FAILED`, `INTERNAL_ERROR` |

## Resources and prompts

Resources: `ifc://model/summary`, `ifc://model/spatial-tree`,
`ifc://session/audit`, `ifc://element/{global_id}`.

Prompts: `model_audit`, `qto_report`, `explain_element`,
`find_unclassified`, `validate_against_ids`, `selector_help`.
