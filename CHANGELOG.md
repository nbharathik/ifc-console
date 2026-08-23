# Changelog

## Unreleased

- A second console session no longer dead-ends when its port is held by
  another ifc-console with the same token: it moves itself to the next free
  port automatically (that sibling keeps serving the pinned MCP clients),
  at startup and again when /chat, /viewer, or /agent need the server.
- `/agent` opens an arrow-key picker of the active agents; `/agent measurement`
  opens one directly; `/agent list` keeps the text listing.
- Ship the basic agents inside ifc-console itself: `measurement` (recipe-driven
  measurement with citations and ChangeSet proposals) and `docs` (project
  document/image Q&A)
  install with the console, appear in `/agent` and the chat panel out of the
  box, and run standalone via `ifc-console agents run <name>` or through the
  SDK (`ifc_console.agents.builtin`). The panel gains an agent selector,
  per-agent conversations with server-side threads, and starter prompts.
- Add a shared reference ledger for the built-in agents under
  `.ifc-console/agents/references/`: browser uploads, `ifc-console agents
  files`, and direct folder copies are tracked and indexed locally. Add
  `list_project_documents` and `get_project_reference_image` so agents can
  inspect provenance and load image pixels as vision input.
- Remove the unfinished extension catalog, installer, scaffold, settings, and
  `/extensions` surface. External agent extensions are not supported yet;
  built-in agents and trusted operation plugins remain separate concepts.
- Store provider API keys in the system keyring via `ifc-console keys
  set/list/delete` (new `ifc-console[keys]` extra). Resolution order
  everywhere: pasted key, then keyring, then environment variable.
- Add vision to the agent path: `AgentImage` (with `from_file`), image parts
  on prompts via `Agent.run(images=...)`, native image content in both
  provider adapters, and automatic handling of image-bearing tool results
  (the transcript keeps a count, the pixels follow as vision input).
  `get_viewer_screenshot` now works through the SDK and the bundled Agent,
  not only through MCP clients.
- Add `get_viewer_measurements`: the distances the user measures in the 3D
  viewer (M key) now cross the websocket and are readable by the model.
- Publish the reusable corpus surface: `ifc_console.knowledge` exports
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
