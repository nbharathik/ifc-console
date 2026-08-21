# Changelog

## Unreleased

- Add vision to the agent path: `AgentImage` (with `from_file`), image parts
  on prompts via `Agent.run(images=...)`, native image content in both
  provider adapters, and automatic handling of image-bearing tool results
  (the transcript keeps a count, the pixels follow as vision input).
  `get_viewer_screenshot` now works through the SDK and the bundled Agent,
  not only through MCP clients.
- Add `get_viewer_measurements`: the distances the user measures in the 3D
  viewer (M key) now cross the websocket and are readable by the model.
- Publish the extension corpus surface: `ifc_console.knowledge` exports
  `Record`, `build`, `Store`, and `ProjectKnowledge` as stable names, and
  the versioning policy names `ifc_console.testing` and the deprecation
  window.
- Add a measurement toolkit: `get_element_geometry` (mesh-derived bounding
  boxes, placement-axis extents, footprint, volume, confidence),
  `measure_elements` (explicit stored_qto, layer_sum, and geometry_extent
  methods with file-unit and SI values), `measure_distance` (centroid, box
  gap, closest-surface), and an opt-in `source="derived"` geometry fallback
  for `compute_quantities`.
- Add a per-project knowledge corpus: `ifc-console knowledge ingest` and
  `Workbench.ingest_docs()` index markdown, text, PDF (via the new
  `ifc-console[pdf]` extra), and referenced images into a hash-keyed index
  beside the models, searchable through `search_ifc_knowledge` with the new
  `corpus` parameter and cited with document and page provenance. Document
  text is data, never instructions; instruction-shaped chunks are flagged.
- Add measurement recipes: YAML files under `.ifc-console/recipes/` resolved
  by the new `get_measurement_recipe` tool (most specific match wins, with
  the source citation and ready-to-use measure_elements arguments) and
  indexed beside the project documents.
- Add the extension store: `ifc-console extensions search/list/install/
  uninstall` over a static catalog with isolated per-agent environments
  (uv tool), an install record, a TUI `/extensions` command, and
  `ifc-console extensions new` scaffolding a complete agent project. The
  first extension, `ifc-agent-measure`, lives in `packages/`.
- Agent SDK: `Agent.run(response_model=...)` validates the final answer into
  typed data with one retry, rounds of read-only tool calls execute
  concurrently (opt out via `AgentLimits.parallel_read_only`),
  `Toolset.describe()` renders a prompt-ready tool summary,
  `ifc_console.testing` ships the scripted model and thread-store fakes, and
  `Workbench.open`/`LocalRuntime.open` accept `project_dir` for the project
  folder convention.
- Add a framework-neutral agent SDK with exact tool selection, live session
  settings, optional LangChain/LangGraph projection, embedded viewer/MCP web
  surfaces, and runtime agent construction.
- Add human-friendly element search by name or GlobalId and a focused property
  agent example with host-owned ChangeSet approval and durable commit.
- Keep agent threads provider-replayable when a run exhausts its tool-call
  budget, and always pair tool_call_started with tool_call_finished events.
- Add a minimal terminal quickstart agent example.

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
