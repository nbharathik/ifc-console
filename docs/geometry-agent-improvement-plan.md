# Geometry Analysis and Parametric Measurement Skill Plan

Status: implemented and verified on 2026-08-31. The approved decisions in section 18 were used.

## 1. Goal

Make the general agent reliable at this workflow:

1. The user selects one object in the viewer.
2. The agent resolves the selected model and object revision.
3. The agent extracts every supported, meaningful parametric measurement from IFC definitions and geometry.
4. Every result carries units, semantic meaning, frame, method, evidence, confidence, and any disagreement.
5. The user reviews and names the measurements that matter.
6. The reviewed procedure is saved as a reusable measurement skill.
7. The skill finds genuinely similar objects, previews why they match, and deterministically extracts the same measurements from them.

"Every measurement" must mean every measurement supported by the analysis inventory, not an unsupported claim that every possible design intent can be inferred from triangles. The result must explicitly list extracted, unavailable, ambiguous, and conflicting measurements.

## 2. Current pipeline

The current path is:

```text
viewer selection
  -> get_viewer_selection
  -> general or measurement agent prompt
  -> analyze_element_geometry
       -> resolve IFC elements
       -> read swept and material profiles
       -> tessellate an analysis mesh
       -> choose the longest PCA axis
       -> slice at 30%, 50%, and 70%
       -> choose one representative section
       -> merge exact and mesh values into dimensions
  -> optional viewer measurements
  -> record endpoint matches values to dimension keys
  -> save a Markdown skill
  -> a later LLM reads the prose and chooses tools again
```

Relevant implementation areas:

- `packages/ifc-console-agents/src/ifc_console_agents/presets.py`: general and measurement agent routing.
- `packages/ifc-console-agents/src/ifc_console_agents/blocks.py`: measurement, viewer, and skill tool guidance.
- `src/ifc_console/mcp/tools_analysis.py`: public geometry tool contracts.
- `src/ifc_console/ifc/profile.py`: current full-element profile and section probe.
- `src/ifc_console/ifc/geometry.py`: tessellation, local extents, area, volume, and mesh cache integration.
- `src/ifc_console/ifc/mesh_analysis.py`: health, frames, directional extent, slicing, and ray intervals.
- `src/ifc_console/ifc/section.py`: section loops and thickness statistics.
- `packages/ifc-console-agents/src/ifc_console_agents/recording.py`: viewer measurement intent matching.
- `packages/ifc-console-agents/src/ifc_console_agents/skills.py`: Markdown skill storage.
- `packages/ifc-console-agents/src/ifc_console_agents/tools_skills.py`: agent-facing skill tools.
- `packages/ifc-console-agents/src/ifc_console_agents/panel.py`: viewer-to-skill recording endpoint.

## 3. What already works and should be preserved

- Selection is tied to the viewer model and includes a `model_id`.
- Mesh coordinates and derived results are reported in SI units.
- Analysis tessellation has explicit settings and does not silently repair source geometry.
- Mesh health, source hashes, confidence, and refusal paths already exist.
- Exact IFC profile parameters are preferred over mesh estimates.
- Multiple methods can disagree without one silently replacing the other.
- Meshes and read results are cached by model fingerprint and revision.
- Skills are project-local, reviewable Markdown and require approval for writes.
- Proposals remain separate from measurements and need human confirmation.

The implementation should extend these guarantees, not create a separate geometry stack.

## 4. Main gaps

### 4.1 Ambiguous object axes and dimension names

The current full probe uses the longest vertex PCA axis. This can be unstable for symmetric shapes, biased by tessellation density, and semantically wrong for walls and other objects whose extrusion direction is not their perceived length. Generic `width`, `height`, and `length` keys can therefore mean different things across classes.

### 4.2 Fixed sections miss shape changes

Three fixed cuts can miss end plates, tapers, openings, bends, local stiffeners, transitions, and varying profiles. Choosing the median-perimeter cut hides multimodal or changing geometry.

### 4.3 Parametric IFC coverage is narrow

The current representation walk focuses on swept-area solids and only the first operand of some Boolean forms. Mapped-item transforms, clipping and subtraction effects, revolved or swept-disk solids, tapered sweeps, type-level representation maps, curve placements, and several profile curve types need stronger coverage and explicit unsupported flags.

### 4.4 The result contract is too flat

`dimensions` keeps one selected value per name. It does not fully encode frame, direction, position along the object, valid domain, uncertainty, alternative evidence, component, or why a value won. This makes it hard for an LLM or a saved skill to replay the measurement safely.

### 4.5 Thickness is not a reusable semantic measurement yet

One section distribution or one user-drawn distance is insufficient for many layered, hollow, ribbed, curved, or variable-thickness objects. Material intervals, void intervals, local position, direction, repeated modes, and variation need to be represented separately.

### 4.6 Recorded skills are prose-driven

The current recorder uses a fixed 5 mm or 2 percent value match and generates prose. It analyzes at most five measured GlobalIds, records world-axis hints, and later depends on the LLM to reinterpret the procedure. There is no validated executable measurement specification, applicability score, dry-run result, or replay regression check.

### 4.7 Similar means only a broad class or human selector

An `IfcWall` or `IfcMember` class match is too broad. Type, profile family, representation kind, material composition, topology, normalized proportions, and geometry signature should all contribute to similarity.

### 4.8 The general agent has too much low-level orchestration work

The agent must decide when to call the full probe, mesh health, a directional extent, a section, a local thickness ray, and visual verification. This costs tool rounds and makes results depend too much on prompt interpretation. A high-level analysis operation should make the safe default path deterministic and keep low-level tools for investigation.

### 4.9 Large results can overwhelm the model

Full profile records, section outlines, health data, and many elements can exceed useful context. The agent needs a compact summary first, with bounded evidence available on demand.

## 5. Target architecture

```text
selected object + selected model revision
  -> semantic object resolver
  -> exact IFC representation extractor
  -> one cached analysis tessellation
  -> geometry health gate
  -> stable semantic frames
  -> component and topology analysis
  -> adaptive sections and thickness sampling
  -> normalized measurement inventory
  -> source reconciliation and coverage report
  -> compact agent response + expandable evidence
  -> reviewed measurement skill draft
  -> applicability and similarity preview
  -> deterministic batch skill execution
  -> result table, deviations, optional proposals
```

The LLM should decide the user's intent and explain the evidence. Deterministic code should resolve frames, calculate measurements, validate skills, match similar objects, and replay extraction rules.

## 6. Versioned analysis contract

Add an additive version 2 contract to `analyze_element_geometry`. Keep the existing fields during migration so current callers, reports, and skills continue to work.

Recommended inputs:

- `detail`: `compact`, `standard`, or `full`.
- `measurement_set`: `standard`, `profile`, `envelope`, `fabrication`, or an explicit list of measurement ids.
- `frame`: `semantic`, `placement`, `principal`, or `world`, with `semantic` as the default.
- `station_strategy`: `auto`, `fixed`, or `none`.
- `stations`: explicit fractions only when `station_strategy=fixed`.
- `precision`: `standard` or `high`, mapped to documented tessellation and sampling budgets.
- `include_alternatives`, `include_sections`, and `include_outline`: bounded evidence controls.
- Existing `selector`, `global_ids`, paging, `physical_only`, and `model` inputs remain.

Recommended output shape:

```json
{
  "analysis_version": "2.0",
  "model_revision": {"model_id": "...", "fingerprint": "...", "revision": 0},
  "elements": [
    {
      "object": {"global_id": "...", "class": "IfcWall", "type": "...", "name": "..."},
      "geometry_family": "constant_profile_extrusion",
      "frames": {
        "placement": {},
        "semantic": {
          "longitudinal": [1, 0, 0],
          "transverse": [0, 1, 0],
          "vertical": [0, 0, 1],
          "source": "ifc_extrusion_and_placement",
          "confidence": "high"
        }
      },
      "measurements": [
        {
          "id": "envelope.overall_length",
          "label": "Overall length",
          "quantity_kind": "length",
          "value_si": 4.0,
          "value_file": 4000.0,
          "si_unit": "m",
          "source": "profile_parameter",
          "method": "ifc_representation",
          "frame": "semantic",
          "direction": "longitudinal",
          "station": null,
          "component": "whole_object",
          "confidence": "high",
          "uncertainty_si": 0.0,
          "flags": [],
          "alternatives": []
        }
      ],
      "coverage": {
        "requested": [],
        "extracted": [],
        "unavailable": [],
        "ambiguous": [],
        "conflicting": []
      },
      "geometry_signature": {},
      "flags": []
    }
  ]
}
```

Contract rules:

- Measurement ids are stable and namespaced, for example `envelope.overall_height`, `profile.web_thickness`, `section.area`, `material.layer.2.thickness`, and `opening.clear_width`.
- A result never changes the meaning of a measurement id based on object class.
- Every numeric result states unit, method, source, frame, and confidence.
- Exact values and measured estimates remain separate alternatives even when one is selected as preferred.
- `uncertainty_si` is zero only for directly represented exact parameters with no modifying transform or Boolean ambiguity.
- Unavailable and ambiguous results are returned, not silently omitted.
- Existing `dimensions`, `box`, `cross_section`, and `flags` are derived compatibility views until all callers migrate.

## 7. Geometry improvements

### 7.1 Build stable semantic frames

Resolve axes in this precedence order:

1. IFC directrix, extrusion, revolution, or swept-solid directions.
2. Element placement and class-aware semantics.
3. Representation-map axes and applied transforms.
4. Area-weighted oriented bounding box or surface-weighted principal axes.
5. Vertex PCA only as a flagged fallback.

Return axis ambiguity, handedness, transform scale, and the source of the chosen frame. Canonicalize axis signs so equivalent rotated instances produce the same intrinsic result. Do not silently force a semantic frame when eigenvalues or representation directions are ambiguous.

### 7.2 Expand exact IFC representation extraction

Create a representation inventory before mesh analysis. Support and test:

- Parameterized, arbitrary closed, centerline, composite, and derived profiles.
- Line, polyline, indexed polycurve, circle, ellipse, trimmed curve, composite curve, and supported arc segments.
- Extruded and tapered area solids.
- Revolved area solids.
- Swept-disk solids and supported fixed-reference sweeps.
- Mapped representations with transform, rotation, uniform scale, and an explicit refusal for unsafe nonuniform scale.
- Boolean clipping, subtraction, and union provenance. Values from a base operand must be marked as pre-Boolean unless the modification is proven irrelevant.
- CSG and BRep presence, with exact attributes where available and mesh fallback where not.
- Type-level material profile sets and layer sets, including offsets and ordering.
- Openings and void relationships as semantic features rather than unexplained mesh holes.

The extractor should return a representation tree with bounded depth and count, plus concise flags for unsupported branches. It should not ship raw IFC entities through the tool contract.

### 7.3 Analyze components and topology

Report connected components, closed shells, boundary loops, cavities, through-holes, and disconnected accessories. Distinguish:

- one material body with an internal void;
- multiple disjoint solids in one product;
- overlapping solids;
- an open sheet or surface model;
- geometry modified by openings or Boolean operations.

Measurements must identify whether they apply to the whole object or a component. Volume, thickness, and section area must retain current refusal behavior when topology is unsafe.

### 7.4 Replace fixed cuts with adaptive sectioning

For `station_strategy=auto`:

1. Seed a bounded set of stations away from exact ends.
2. Compute compact section descriptors: area, perimeter, bounds, loop count, hole count, thickness modes, and outline signature.
3. Subdivide where adjacent descriptors change beyond scale-aware tolerance.
4. Cluster equivalent sections into constant-profile regions.
5. Return dominant, minimum, maximum, and transition sections with their station ranges.

This exposes constant, tapered, stepped, and locally modified geometry instead of selecting one median section. Budgets must cap station count and total section segments.

### 7.5 Improve thickness extraction

Use multiple evidence paths:

- Exact profile or material layer thickness.
- Parallel-face distance for planar plate regions.
- Section-based local thickness distributions.
- A bounded ray grid across representative regions, using the existing safe interval pairing.
- Curved-shell normal sampling where local normals are reliable.

Return thickness modes with spatial domains, variation, sample count, confidence, and component. Keep material intervals and void intervals separate. Never reduce a ribbed or hollow object to one unlabeled median thickness.

### 7.6 Add a broader parametric inventory

The standard inventory should include supported values from these groups:

- Envelope: semantic overall length, width, height, diagonal, oriented box, centroid, and support points.
- Mass geometry: volume, surface area, projected areas, footprint, component volumes, and volume reliability.
- Profile: overall profile bounds, wall, web and flange thicknesses, radii, slopes, offsets, profile area, perimeter, centroid, and second moments when derivable.
- Longitudinal form: directrix or axis length, straightness, curvature, bend radius, taper rate, twist, and station ranges where supported.
- Openings and voids: count, clear dimensions, area, position in the semantic frame, and through or blind status when provable.
- Material: layer and profile thicknesses, sequence, offsets, and total material thickness.
- Topology: component, shell, hole, boundary, and manifold counts as non-dimensional parameters.
- Stored values: matching QTO or property values as comparison evidence, never as geometry-derived truth.

Advanced fabrication properties should be optional because they cost more and are not meaningful for every IFC class.

### 7.7 Use scale-aware tolerances

Replace global fixed tolerances with a policy derived from:

- file unit scale;
- object envelope;
- tessellation deflection;
- floating point resolution at georeferenced coordinates;
- mesh feature size;
- user or recipe tolerance.

Record the effective absolute and relative tolerances in evidence. The viewer recorder should use the same policy when matching a hand measurement to an extracted parameter.

### 7.8 Reconcile evidence without hiding conflicts

Define source precedence by measurement, not one global order. In general:

1. Direct exact representation parameter that remains valid after transforms and Booleans.
2. Exact material profile or layer parameter for material-specific measurements.
3. Stored QTO as declared evidence.
4. High-confidence mesh measurement.
5. Viewer hand measurement.
6. Visual estimate, which is never an exact dimension.

Keep all alternatives. Select a preferred value only when its semantic meaning matches the requested measurement. Report absolute and relative deltas, tolerance, and a conflict status.

## 8. Tool surface improvements

### 8.1 Keep low-level tools

Retain `inspect_element_mesh`, `measure_directional_extent`, `slice_element_mesh`, and `measure_local_thickness` for investigation and skill fallbacks. Align their frame, source, tolerance, and confidence fields with the version 2 measurement record.

### 8.2 Make `analyze_element_geometry` the safe high-level default

It should perform representation extraction, one mesh health pass, semantic frame resolution, adaptive analysis, source reconciliation, and compact reporting in one operation. The agent should not need to assemble the common pipeline from six tool calls.

### 8.3 Add deterministic skill execution

Add `apply_measurement_skill` with:

- `name`;
- exactly one of `selector` or `global_ids`;
- `model`;
- `dry_run=true` by default;
- paging and maximum match limits;
- optional `include_evidence` and `minimum_confidence`.

It should parse and validate the structured skill spec, verify applicability, execute its measurement rules in a batch, and return one row per target with deviations and skipped reasons. It must not propose or write IFC properties unless a separate, explicit proposal call follows user confirmation.

### 8.4 Add explainable similarity search

Add `find_similar_elements` or make an equivalent preview part of `apply_measurement_skill`. Matching should be staged:

1. Hard filters: model, physical class family, representation compatibility, and required profile family.
2. Strong signals: type, material profile, layer composition, mapped representation source, and normalized topology.
3. Geometric signals: normalized proportions, component count, section signature, and intrinsic mesh signature.
4. Return score, match reasons, mismatch reasons, and the threshold used.

Exact type matches should not be required when geometry is equivalent. Same-class objects should not pass solely because their class matches.

### 8.5 Control result size

- `compact` returns the preferred inventory, coverage, and flags.
- `standard` also returns alternatives and representative section summaries.
- `full` includes bounded representation trees, samples, and outlines.
- Page element results.
- Bound arrays and return counts for omitted evidence.
- Preserve mesh hashes and analysis options so a later detailed request can be tied to the same source.

## 9. Structured measurement skills

Keep one human-readable Markdown file per skill. Add a versioned, validated `measurement-spec` JSON block inside the Markdown body and add simple front-matter fields such as `kind` and `schema_version`.

Example shape:

```json
{
  "schema_version": 2,
  "kind": "parametric_measurement",
  "applicability": {
    "ifc_classes": ["IfcMember"],
    "profile_families": ["i_shape"],
    "hard_requirements": ["constant_or_piecewise_profile"],
    "similarity_threshold": 0.85
  },
  "measurements": [
    {
      "output": "profile.web_thickness",
      "preferred_sources": ["profile_parameter", "mesh_section"],
      "fallbacks": ["adaptive_section.thickness_modes"],
      "frame": "semantic",
      "minimum_confidence": "medium",
      "tolerance": {"absolute_si": 0.001, "relative": 0.02}
    }
  ],
  "verification": {
    "cross_check": "second_source_when_available",
    "on_conflict": "report_and_refuse_property_proposal"
  },
  "outputs": []
}
```

The surrounding Markdown should explain when to use the skill, what the outputs mean, the exemplar, checks, limitations, and optional proposal mapping. The executable block is authoritative for replay, but never a source of model facts.

### 9.1 Skill lifecycle

Use a reviewable lifecycle:

1. **Draft**: capture selected object, current model revision, viewer measurements, labels, and full geometry analysis.
2. **Infer**: map measurements to stable measurement ids and semantic frames. Preserve unresolved rows for user naming instead of guessing.
3. **Preview applicability**: show candidate similar objects and match reasons.
4. **Validate**: replay on the exemplar and a bounded sample of candidates. Cross-check alternative sources and show deviations.
5. **Save**: write the reviewed Markdown plus machine spec with approval.
6. **Apply**: default to dry-run, show all matched, skipped, ambiguous, and failed objects.
7. **Propose**: only after a separate user confirmation.

### 9.2 Improve recording from the viewer

- Analyze every referenced element up to a documented limit. Do not silently analyze only the first five.
- Pin analysis to the measurement's `model_id`, fingerprint, and revision.
- Transform anchors into the object's semantic or local frame for intent inference. Do not replay world coordinates.
- Preserve snap kind, face or edge identity when available, direction, section state, and measurement label.
- Match by semantic direction, feature relationship, and value tolerance rather than value alone.
- Treat a distance between two objects as a relationship skill with two roles, not an object dimension.
- Treat area, path, angle, clearance, and element-size records as distinct rule types.
- Mark unresolved intent and require review before it becomes executable.
- Store the exemplar geometry signature and expected invariants for regression validation, not as a universal expected value.

### 9.3 Backward compatibility

- Existing prose-only skills continue to load and can still guide the LLM.
- Only structured version 2 skills can use deterministic `apply_measurement_skill`.
- Add a migration preview that suggests a machine spec for an old skill without overwriting it.
- Preserve the existing name, description, applies-to fields, size limits, write approval, and audit records.

## 10. General agent behavior

Update the general and measurement instructions to use this order:

1. If the user refers to "this" or "selected", call `get_viewer_selection` and pin all later calls to its `model_id`.
2. Load a matching skill before inventing a method.
3. For one or a few objects, call the high-level geometry analyzer in compact or standard mode.
4. Inspect detailed sections, local thickness, health, or screenshots only when the high-level result flags ambiguity or the user asks for evidence.
5. Present extracted, unavailable, ambiguous, and conflicting measurements separately.
6. If the user wants repetition, create a skill draft and preview similar targets before saving or applying.
7. Never call same-class objects similar without match evidence.
8. Never write properties as part of skill application. Proposals remain a separate confirmed action.

Add worked examples for:

- analyze one selected wall;
- analyze a rotated structural profile;
- learn web and flange thickness from one selected member;
- apply the skill to geometrically similar members of different IFC types;
- refuse a hollow-object thickness result when the mesh is invalid;
- report a tapered object as a range and station function instead of one value.

## 11. Viewer and workspace UX

Add a selected-object geometry analysis view with:

- grouped measurements: envelope, profile, material, sections, openings, and topology;
- exact versus measured source badges;
- confidence, tolerance, and conflict indicators;
- semantic-axis triad and representative section overlays;
- station slider for variable profiles;
- alternatives and source deltas;
- explicit unavailable and ambiguous lists.

Add a measurement skill review flow with:

- editable skill name and measurement labels;
- resolved and unresolved intents;
- applicability rules;
- similar-object preview with match reasons;
- exemplar replay result;
- dry-run batch table;
- save, revise, and cancel actions;
- a separate propose-values action after review.

The first implementation can render structured tables in the agent workspace. Geometry overlays can follow once the server contract is stable.

## 12. Performance plan

- Reuse the existing revision-aware mesh cache.
- Run exact representation extraction before mesh work and skip expensive derivations not requested by the measurement set.
- Within one analysis call, compute mesh health, references, frames, section seeds, and hashes once.
- Cache intrinsic analysis by model revision, representation or normalized mesh signature, and analysis options.
- Analyze mapped or repeated geometry once in intrinsic coordinates, then apply each instance transform.
- Batch targets in one model-worker job and group equivalent representations.
- Use adaptive station and ray budgets based on object complexity, with hard global caps.
- Return compact results to the LLM while keeping bounded detailed evidence available through a follow-up call.
- Record timing, tessellation count, triangle count, station count, cache hits, and skipped work in debug metadata.

Performance acceptance targets should be established from a baseline before optimization. Proposed initial targets on the existing test fixture hardware:

- No repeated tessellation when compact analysis is followed by a detailed analysis of the same revision and profile.
- A repeated mapped geometry batch performs intrinsic analysis once per unique representation.
- Compact response size remains bounded per element.
- Automatic sampling always respects station, ray, triangle, time, and output limits.

## 13. Implementation phases

### Phase 0: Baseline and contract tests

- Capture current outputs and timing for representative fixtures.
- Add the versioned measurement record models and schema tests.
- Define stable measurement ids and semantic names.
- Add compatibility tests for current `dimensions` consumers.
- No algorithm change in this phase.

Deliverable: approved version 2 contract and baseline report.

### Phase 1: Semantic frames and representation inventory

- Add the representation tree and transform handling.
- Add exact source validity and pre-Boolean flags.
- Implement semantic frame resolution with ambiguity evidence.
- Populate the normalized measurement inventory from existing exact and mesh results.
- Keep legacy fields derived from the inventory.

Deliverable: selected rotated and mapped objects report stable, unambiguous semantic measurements.

### Phase 2: Adaptive geometry analysis

- Add component and topology descriptors.
- Implement adaptive section discovery and profile-region clustering.
- Add multi-method thickness modes and spatial domains.
- Add optional advanced profile and longitudinal properties.
- Implement scale-aware tolerances and source reconciliation.

Deliverable: tapered, hollow, curved, stepped, layered, and composite fixtures produce explicit ranges, regions, or refusals.

### Phase 3: High-level tool and agent routing

- Extend `analyze_element_geometry` inputs and outputs.
- Align low-level tool evidence fields.
- Add compact, standard, and full response levels.
- Update tool descriptions, capability blocks, general role, measurement role, examples, and golden contracts.

Deliverable: the general agent handles a selected-object analysis through one primary tool call and uses detailed tools only for flagged cases.

### Phase 4: Structured skill draft, validation, and replay

- Extend skill metadata and parse validated measurement specs.
- Upgrade viewer recording to semantic intent inference and model-revision pinning.
- Add deterministic `apply_measurement_skill` with dry-run default.
- Add explainable applicability and similarity matching.
- Add exemplar and candidate replay validation.
- Keep old prose skills working.

Deliverable: one reviewed object can produce a skill that extracts the same semantic parameters from a previewed similar-object set without LLM calculation.

### Phase 5: Workspace review experience

- Add grouped geometry results and conflict display.
- Add the skill review and similar-object preview flow.
- Add section and semantic-frame overlays after the API stabilizes.
- Keep all write and proposal actions explicit.

Deliverable: the user can inspect, correct, save, dry-run, and apply a skill from the workspace.

### Phase 6: Optimization, evaluation, and documentation

- Add intrinsic repeated-geometry caching and batching.
- Add timing and cache instrumentation.
- Run algorithm, API, agent, UI, and regression suites.
- Document measurement semantics, limits, confidence, skills, and migration.
- Update changelog only when implementation is complete.

Deliverable: measured performance targets, passing regression suite, and end-user documentation.

## 14. Proposed file changes during implementation

Core geometry:

- Extend `src/ifc_console/ifc/profile.py` or split its orchestration into a focused `geometry_analysis.py` while keeping compatibility imports.
- Add `src/ifc_console/ifc/representation.py` for exact representation inventory and transforms.
- Add `src/ifc_console/ifc/similarity.py` for normalized signatures and explainable matching.
- Extend `src/ifc_console/ifc/geometry.py`, `mesh_analysis.py`, and `section.py` rather than duplicating their mesh primitives.
- Extend `src/ifc_console/ifc/report.py` for the normalized measurement inventory.

Public tools and agent behavior:

- Extend `src/ifc_console/mcp/tools_analysis.py`.
- Extend `packages/ifc-console-agents/src/ifc_console_agents/blocks.py` and `presets.py`.
- Extend `packages/ifc-console-agents/src/ifc_console_agents/recording.py`, `skills.py`, and `tools_skills.py`.
- Update the recording route and workspace UI in `panel.py` and `static/chat*.js` or a dedicated geometry module.

Tests and docs:

- Extend `tests/unit/test_profile_analysis.py`, `test_mesh_analysis.py`, `test_geometry_probe.py`, and analysis tool integration tests.
- Extend agent skill, block, panel, and UI tests under `packages/ifc-console-agents/tests`.
- Add focused fixtures rather than relying only on large real-world files.
- Update API and SDK golden contracts after the schema is approved.
- Update `docs/tools.md`, `docs/agents.md`, `docs/viewer.md`, and SDK documentation during the final implementation phase.

## 15. Required test matrix

Algorithm fixtures:

- translated, rotated, mirrored, and georeferenced boxes;
- symmetric objects with ambiguous PCA axes;
- constant, tapered, stepped, twisted, and curved sweeps;
- rectangular, circular, hollow, I, T, U, Z, L, C, asymmetric, arbitrary, composite, and derived profiles;
- mapped items with rotations and scale;
- Boolean cuts and openings;
- layered walls and slabs;
- multiple disconnected solids;
- hollow solids with internal voids;
- open, non-manifold, duplicate-face, degenerate, and inconsistent-winding meshes;
- objects that exceed triangle, station, ray, and output budgets.

Invariants:

- intrinsic measurements are invariant under translation and rotation;
- unit conversion is consistent across metre, millimetre, and other supported units;
- semantic ids retain the same meaning across classes;
- exact and mesh sources are never conflated;
- invalid topology cannot produce a high-confidence volume or paired thickness;
- mapped equivalent geometry has the same intrinsic signature;
- adaptive sections discover known transitions within tolerance;
- repeated analysis on one revision is deterministic.

Skill tests:

- draft from a selected single object;
- replay on the exemplar;
- similar-object preview with explainable accept and reject cases;
- rotated and scaled valid instances;
- class match with geometry mismatch is rejected;
- unresolved viewer measurement requires review;
- relationship measurement preserves both object roles;
- old prose skill remains readable;
- malformed or unsupported machine spec is refused safely;
- dry-run never writes a property;
- proposal requires a separate confirmation.

Agent evaluations:

- the general agent pins the selected `model_id`;
- it uses one high-level call for the normal selected-object request;
- it loads a matching skill before inventing a method;
- it asks for detailed evidence only on ambiguity or request;
- it reports unavailable and conflicting measurements;
- it does not call all same-class objects similar;
- it does not claim visual estimates are exact;
- it does not save or propose without user approval.

## 16. Acceptance criteria

The improvement is complete when:

1. Selecting one supported object returns a stable measurement inventory with semantic ids, units, frame, method, source, confidence, and coverage.
2. Rotation, translation, mapping, and file unit changes do not alter intrinsic results beyond recorded tolerance.
3. Variable geometry is reported by regions, ranges, or functions rather than collapsed to one misleading number.
4. Invalid or ambiguous geometry produces explicit flags or refusals.
5. Exact IFC parameters and mesh measurements remain independently visible and conflicts are quantified.
6. A reviewed skill contains a validated machine spec and readable instructions.
7. Applying a skill is deterministic, batch-capable, dry-run by default, and explains why each target matched or was skipped.
8. The exemplar skill replays within tolerance and at least one dissimilar same-class fixture is rejected.
9. The general agent uses the selected model and the high-level safe path without unnecessary tool calls.
10. Existing geometry tools, prose skills, reports, and SDK callers remain compatible during migration.

## 17. Recommended scope for the first implementation

Implement these first because they directly improve the stated use case:

1. Versioned measurement inventory and stable semantic ids.
2. Semantic frames from IFC representation and placement, with explicit ambiguity fallback.
3. Compact high-level selected-object analysis.
4. Structured measurement skill spec in the existing Markdown file.
5. Deterministic skill replay with dry-run and explainable type, profile, and geometry matching.
6. Fix viewer recording so all bounded referenced objects are analyzed against the correct model revision.
7. Add rotated, mapped, tapered, hollow, and same-class-different-geometry tests.

Then add deeper adaptive sampling, advanced fabrication properties, and 3D overlays. This sequence gives the user a trustworthy select, analyze, save, and repeat loop before expanding the long tail of geometry algorithms.

## 18. Approved implementation decisions

The implementation used these approved decisions:

1. Extend `analyze_element_geometry` additively instead of replacing it with a new incompatible tool.
2. Keep skills as Markdown and embed a validated versioned JSON measurement spec in the same file.
3. Make deterministic skill application dry-run by default.
4. Use semantic measurement ids and frames instead of class-dependent `width`, `height`, and `length` meanings.
5. Require explainable similarity preview before applying a newly recorded skill to a broad selector.
6. Deliver the core select, analyze, save, and replay loop before advanced geometry overlays.

## 19. Implementation record

All six implementation phases were completed as one coordinated change while
preserving the additive version 1 compatibility views.

- Geometry analysis now emits contract version 2.0 with semantic frames, stable
  measurement ids, source and method, units, uncertainty, confidence,
  alternatives, coverage, topology, representation provenance, and model
  revision evidence.
- Exact IFC extraction covers mapped representations, applied transforms,
  Booleans, material profiles and layers, revolved and disk sweeps, and tapered
  start/end profiles. Approximate and pre-Boolean evidence is explicitly
  downgraded instead of presented as exact.
- Mesh fallback analysis uses deterministic semantic or stable intrinsic frames,
  scale-aware tolerances, adaptive sections, profile regions, multi-mode
  thickness evidence, bounded precision budgets, and explicit ambiguity or
  refusal states.
- Repeated mapped definitions reuse intrinsic solid and profile analysis while
  occurrence transforms remain independent. The model mesh cache and high-level
  read cache expose hits, misses, tessellation work, timing, evictions, and
  skipped work.
- Measurement skills keep readable Markdown and embed a validated version 2
  `measurement-spec`. Deterministic application is read-only, paged, explainable,
  revision-aware, geometry-aware, and dry-run by default. Same-class or
  same-GlobalId identity alone cannot bypass geometry comparison.
- Legacy prose skills have a non-mutating migration preview that deliberately
  produces unresolved rules for review. Relationship recordings retain ordered
  object roles and local anchor evidence; unsupported relationship execution is
  refused safely until a dedicated relationship operator exists.
- The general and measurement agents use selection-pinned high-level analysis,
  skill-first routing, explicit coverage categories, and separate proposal
  actions. The workspace provides grouped measurements, a semantic L/T/V frame
  visualization, adaptive section station browsing, source badges, alternative
  deltas, conflict evidence, and stale or partial result protection.
- Direct renderer overlays remain a viewer-owned extension point. The workspace
  visualization consumes the stable analysis contract without coupling the
  agent package to Three.js internals.

The regression matrix covers exact and mesh sources, rotated and symmetric
sections, tapered and approximate profiles, mapped and Boolean inventory,
revision and cache reuse, structured skill validation and migration, similarity
acceptance and rejection, stale UI state, response bounds, and backward
compatibility. Repository-wide Python, JavaScript, lint, contract, and diff
checks are recorded in the completion report for this implementation.

Final verification on 2026-08-31:

- 1,438 Python test cases collected: 1,436 passed, 1 platform-specific test
  skipped, and 1 known guard-bypass test xfailed.
- All 168 JavaScript UI and viewer tests passed.
- Core and agent API/SDK golden contracts were regenerated and all 4 contract
  checks passed. The catalogs contain 62 core tools and 67 agent tools.
- `ruff check .` and `git diff --check` passed. The latter reports only existing
  CRLF conversion notices on Windows.
- One-object fixture payloads measured 14,893 bytes in compact mode, 14,918
  bytes in standard mode, and 16,650 bytes in full mode with outlines enabled.
- Standard precision uses 17 adaptive stations and 2,000 thickness rays per
  section; high precision uses 33 stations and 4,000 rays with a tighter
  scale-aware tolerance while reusing the same analysis mesh.
