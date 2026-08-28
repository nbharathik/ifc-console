# MCP tools

Use `/tools ai` for the live catalog and exact input schemas. It reflects the
current mode, optional dependencies, plugins, models, and viewer state. Start a
new session with `orient`, then use the narrowest operation that fits.

## Response format

Operations return one envelope:

```json
{"ok": true, "data": {}, "meta": {"mode": "ask", "model": "x.ifc"}}
```

```json
{"ok": false, "error": {"code": "ASK_MODE_BLOCKED", "message": "...", "hint": "..."}}
```

Large results may set `meta.truncated=true`; narrow or paginate the request.
`get_viewer_screenshot` returns image content in addition to structured data.

## Session and model queries

| tool | purpose |
| ---- | ------- |
| `get_session_status` | version, model, mode, dirty state, and viewer state |
| `orient` | status, project summary, and shallow spatial tree |
| `describe_capabilities` | live operations, permissions, availability, and examples |
| `get_ifc_project_info` | schema, units, counts, materials, classifications, and header |
| `get_spatial_structure` | Project to Site to Building to Storey to Space tree |
| `search_elements` | search names, GlobalIds, text, or simple selectors |
| `query_elements` | paged selector results with chosen fields and ordering |
| `get_element` | attributes, properties, type, materials, and container |
| `get_psets` | property and quantity sets |
| `get_schema_docs` | IFC entity, pset, or property definitions |

Use `search_elements` for user-facing names and GlobalIds. Use
`query_elements` for deliberate IfcOpenShell selectors:

```text
IfcWall
IfcWall, IfcSlab
IfcWall, material=concrete
IfcWall, Pset_WallCommon.FireRating=F30
IfcElement, Name=/W.*1/
```

The default page is 50 rows. IFC read operations accept an optional resident
`model` ID; omit it to use the active model.

## Knowledge and project evidence

The built-in IFC reference and deterministic project index belong to
`ifc-console`. Installing `ifc-console-agents` adds PDF text extraction and
page rendering for agents; Markdown and plain-text indexing needs no LLM.

| tool | purpose |
| ---- | ------- |
| `search_ifc_knowledge` | search IFC references, APIs, recipes, and project documents |
| `get_knowledge_record` | read one result by key |
| `list_project_documents` | list indexed documents and images with hashes |
| `get_project_reference_image` | load one indexed image as vision input |
| `get_project_document_page` | render one PDF page as vision input |
| `get_api_docs` | inspect an `ifcopenshell.api` signature or search API names |
| `get_measurement_recipe` | resolve the project method and citation for one property |

`search_ifc_knowledge` accepts `corpus="builtin"`, `"project"`, or `"all"`.
See [Knowledge](knowledge.md) and [Measurement recipes](agents.md#measurement-recipes).

## Analysis and measurement

| tool | purpose |
| ---- | ------- |
| `validate_model` | schema validation and grouped issues |
| `validate_ids` | buildingSMART IDS validation; requires `[validation]` |
| `compute_quantities` | stored or geometry-derived quantities by group |
| `detect_clashes` | sampled mesh or bounding-box overlap and clearance |
| `get_element_geometry` | SI bounding box, axes, footprint, volume, and confidence |
| `inspect_element_mesh` | raw mesh topology and health evidence |
| `analyze_element_geometry` | IFC profile parameters cross-checked with mesh sections |
| `measure_elements` | stored quantity, layer sum, or geometry extent |
| `measure_directional_extent` | support extent along a world, local, or principal direction |
| `measure_local_thickness` | ordered material and void intervals through one point |
| `slice_element_mesh` | arbitrary mesh cut with outline, area, and reconstruction frame |
| `measure_distance` | centroid, box gap, and sampled surface distance |
| `get_georeferencing` | CRS, map conversion, and north directions |
| `export_csv` | audited CSV inside an allowed directory |
| `export_measurement_report` | audited Markdown report registered as an artifact |

`analyze_element_geometry` is the broad single-element probe. It reports exact
profile values where available, measured mesh values, their sources, and
mismatch flags. Dedicated measurement tools are better for repeatable batches.

Mesh analysis uses the opt-in analysis tessellation profile and records its
settings and source hash. It does not repair the source mesh. Invalid,
non-manifold, or inconsistently wound geometry may return observable evidence
without claiming reliable volume or material intervals.

`inspect_element_mesh`, `slice_element_mesh`, and `measure_local_thickness`
support `backend="auto"`. Install `ifc-console[geometry]` for Trimesh health
predicates; built-in NumPy checks remain available.

Clash precision is `sampled` by default. `fast` uses bounding boxes and can
produce false positives. Clearance always reports its bounding-box method.

## Whole-model insight

| tool | purpose |
| ---- | ------- |
| `compare_models` | changes between two open revisions |
| `query_spatial` | geometric relations to an element or box |
| `check_model_health` | duplicate IDs, orphaning, geometry, placement, storey, and type findings |

`compare_models` pairs by GlobalId, then falls back to class, type, and name.
It reports added, removed, moved, geometry, property, type, and container
changes. Attach the newer file, then pass its ID as `other_model`.

`query_spatial` supports `inside`, `crosses`, `above`, `below`,
`within_distance`, and `within_box`. Results include method and confidence;
mesh-dependent confidence drops when the target is not watertight.

`check_model_health` complements `validate_model`: it finds modeling defects
that may still satisfy the IFC schema. Geometry checks are skipped, never
silently sampled, when the model exceeds `max_elements`.

## Jobs and artifacts

Validation jobs run outside the client connection:

```text
submit_validation_job -> get_job -> artifact metadata
                              \-> cancel_job
```

| tool | purpose |
| ---- | ------- |
| `submit_validation_job` | start schema and optional IDS validation |
| `get_job` / `list_jobs` | inspect durable job state and output |
| `cancel_job` | request cancellation before a commit point |
| `list_artifacts` / `get_artifact` | inspect verified output metadata |

Jobs require a clean model because workers verify the file on disk. Export
artifact bytes with the SDK or `ifc-console artifacts export`.

## Structured change previews

```text
AI operation: preview -> inspect ChangeSet
Host SDK/CLI: approve -> commit -> optional restore
```

| tool | purpose |
| ---- | ------- |
| `preview_property_change` | preview one property value on selected elements |
| `preview_property_changes` | preview up to 16 property values as one atomic ChangeSet |
| `preview_classification_assignment` | preview a direct classification assignment |
| `get_change_set` | inspect a revision-bound ChangeSet |
| `list_ai_authored_properties` | inventory values under `IfcConsole_AI_` |

Preview does not modify the model. AI-visible operations cannot approve,
commit, restore, save, or change mode. Agent proposal tools bundle each value
with its per-property provenance record in one ChangeSet. See
[AI-marked proposals](agents.md#ai-marked-proposals).

## Generated Python

`execute_ifc_code` accepts `code` and an audited `description`. The environment
provides `ifc`, `ifcopenshell`, `ifc_api`, common utilities,
`query(selector)`, and `get_ifc_file()`.

Read-only code is allowed in `ask` or `edit`; mutations require `edit`.
Eligible reads use the generated-code sandbox. Results include stdout, final
expression, classification, mutation state, sandbox state, and duration. See
[Safety](safety.md) and [Code sandbox](sandbox.md).

## Files and workspace

| tool | purpose |
| ---- | ------- |
| `list_ifc_files` | allowed IFC files and recents |
| `open_ifc_file` | replace the active model, refusing unsaved changes |
| `save_ifc_file` | atomic save with backup and AI-save policy |
| `find_files` | find supported files without opening them |
| `list_models` | resident models, companions, and memory budget |
| `attach` / `detach` | add or release IFC, IDS, BCF, or CSV attachments |
| `set_active_model` | move writable focus to a resident model |

Only the active model is writable. Paths must remain inside the launch
directory, model directory, or an explicitly allowed root.

## Agent skills

Skills are Markdown procedures in `.ifc-console/agents/skills/`. These tools
are registered by the optional `ifc-console-agents` distribution through the
`ifc_console.extensions` entry-point group.

| tool | purpose |
| ---- | ------- |
| `list_agent_skills` | names, descriptions, and applicability |
| `get_agent_skill` | one complete procedure |
| `save_agent_skill` | create or update a skill with host approval |

## Viewer operations

Viewer operations ship with `ifc-console` and remain discoverable before a
browser tab is connected:

| tool | purpose |
| ---- | ------- |
| `open_viewer` | enable and open the local browser surface |
| `get_viewer_selection` | selected elements by model |
| `get_viewer_measurements` | measurements from user or agent |
| `highlight_elements` | color, isolate, clear, or frame elements |
| `apply_color_theme` | paint labeled groups and show a legend |
| `get_viewer_screenshot` | capture a preset or current view |
| `control_viewer` | orient, section, select, focus, measure, and save viewpoints |

The handlers report states such as `ready`, `call_open_viewer`,
`waiting_for_viewer_tab`, and `unavailable_on_transport`. Missing assets mean
the main package installation is incomplete; there is no viewer extra to add.
See [3D viewer](viewer.md).

## Error codes

Every error includes a recovery `hint`. The stable registry is grouped below
for clients that branch on `code`:

- Policy and session: `AI_SAVE_DISABLED`, `ASK_MODE_BLOCKED`,
  `CAPABILITY_DENIED`, `CONSOLE_AUTH_FAILED`, `CONSOLE_NOT_RUNNING`.
- Approval and changes: `APPROVAL_MISMATCH`, `APPROVAL_NOT_FOUND`,
  `APPROVAL_REQUIRED`, `CHANGESET_INVALID`, `CHANGESET_NOT_FOUND`,
  `COMMIT_FAILED`, `COMMIT_NOT_FOUND`, `RESTORE_CONFLICT`,
  `RESTORE_NOT_FOUND`, `REVISION_CONFLICT`.
- Input and queries: `INVALID_INPUT`, `INVALID_OUTPUT`, `INVALID_QUERY`,
  `NOT_FOUND`, `NO_MATCH`, `PROPERTY_NOT_FOUND`, `RESULT_TOO_LARGE`,
  `TOO_MANY_ELEMENTS`.
- Models and files: `FILE_EXISTS`, `FILE_NOT_FOUND`, `MODEL_BUSY`,
  `MODEL_NOT_FOUND`, `MODEL_READ_ONLY`, `MODEL_TOO_LARGE`,
  `NO_MODEL_LOADED`, `PATH_NOT_ALLOWED`, `SOURCE_CHANGED`,
  `UNSAVED_CHANGES`.
- Runtime: `CHAT_FAILED`, `EXEC_BLOCKED`, `EXEC_ERROR`, `EXEC_TIMEOUT`,
  `EXTRA_NOT_INSTALLED`, `INTERNAL_ERROR`, `SANDBOX_UNAVAILABLE`,
  `STORE_BUSY`, `VALIDATION_FAILED`.
- Geometry: `FRAME_UNAVAILABLE`, `GEOMETRY_ANALYSIS_FAILED`,
  `INVALID_GEOMETRY`, `NO_GEOMETRY`.
- Knowledge and workspace: `KNOWLEDGE_DISABLED`, `KNOWLEDGE_NOT_READY`,
  `WORKSPACE_BUDGET`, `WORKSPACE_DISABLED`.
- Viewer: `VIEWER_BUSY`, `VIEWER_ERROR`, `VIEWER_NOT_CONNECTED`,
  `VIEWER_TIMEOUT`, `VIEWER_UNAVAILABLE`.
- Artifacts: `ARTIFACT_CORRUPT`, `ARTIFACT_EXPORT_FAILED`,
  `ARTIFACT_GC_CONFLICT`, `ARTIFACT_GC_FAILED`, `ARTIFACT_NOT_FOUND`,
  `ARTIFACT_STORE_BUSY`, `ARTIFACT_STORE_CORRUPT`.
- Transactions: `TRANSACTION_INTERRUPTED`, `TRANSACTION_JOURNAL_BUSY`,
  `TRANSACTION_JOURNAL_CORRUPT`, `TRANSACTION_JOURNAL_INVALID`,
  `TRANSACTION_RECOVERY_REQUIRED`.
- Jobs: `JOB_CANCELLED`, `JOB_NOT_CANCELLABLE`, `JOB_NOT_FOUND`,
  `JOB_RESULT_INVALID`, `JOB_SERVICE_CLOSED`, `JOB_SPEC_INVALID`,
  `JOB_TIMEOUT`, `JOB_WORKER_FAILED`.
- Batches: `BATCH_CANCELLED`, `BATCH_CHILD_FAILED`, `BATCH_INTERRUPTED`,
  `BATCH_NOT_FOUND`, `BATCH_NOT_RESUMABLE`, `BATCH_SERVICE_CLOSED`,
  `BATCH_SOURCE_CHANGED`, `BATCH_STORE_FAILED`, `BATCH_SUPERVISOR_FAILED`,
  `BATCH_TIMEOUT`.
- Workflows: `WORKFLOW_CANCELLED`, `WORKFLOW_DEPENDENCY_FAILED`,
  `WORKFLOW_INPUT_EMPTY`, `WORKFLOW_INPUT_LIMIT`, `WORKFLOW_INTERRUPTED`,
  `WORKFLOW_MANIFEST_INVALID`, `WORKFLOW_MANIFEST_TOO_LARGE`,
  `WORKFLOW_NOT_FOUND`, `WORKFLOW_NOT_RESUMABLE`, `WORKFLOW_PATH_INVALID`,
  `WORKFLOW_SERVICE_CLOSED`, `WORKFLOW_SOURCE_CHANGED`,
  `WORKFLOW_STEP_FAILED`, `WORKFLOW_STORE_CORRUPT`, `WORKFLOW_STORE_FAILED`,
  `WORKFLOW_SUPERVISOR_FAILED`, `WORKFLOW_TIMEOUT`.

## Resources and prompts

Resources: `ifc://model/summary`, `ifc://model/spatial-tree`,
`ifc://session/audit`, and `ifc://element/{global_id}`.

Prompts: `model_audit`, `qto_report`, `explain_element`, `find_unclassified`,
`validate_against_ids`, and `selector_help`.
