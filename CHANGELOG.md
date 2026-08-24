# Changelog

## Unreleased

- Declutter the panel chrome. The header is one row (assistant, provider chip,
  actions); the persistent stage rail, the reach sentence, and the description
  subtitle are gone. Progress now appears inside the message making it, as a
  single live line, and the tool calls fold into a "Used N tools" disclosure
  once the run finishes. The conversation and composer sit in a centred reading
  column.
- The sidebar no longer opens on pointer hover; it opened whenever the cursor
  crossed the left edge on its way past. It opens on click, on the pin, and on
  keyboard focus.
- Fix workspace tabs collapsing into circles when their labels wrapped inside a
  pill radius. They no longer wrap, and the tab bar scrolls if it must.
- Fix the reach line running off the right edge of the panel instead of
  truncating.

- Add a `general` assistant that holds every capability block, and make it the
  default. `measurement`, `docs`, and the new `review` are presets of the same
  machinery: a role prompt plus a narrower block set, declared as data in
  `ifc_console.agents.presets`. Specialising an agent means writing standing
  instructions or picking blocks, not writing a new agent.
- Add the agent workspace: `GET /api/agents/workspace` returns one payload
  describing an agent exactly as it would run (prompt, blocks, every tool with
  its stage, reachable stages, examples, write policy, limits, files), built
  from the same composition the agent runs with so it cannot drift. The panel
  renders it as a side panel with how-it-works, tools, files, and per-agent
  settings tabs, which keeps that material out of the conversation.
- Rework the panel shell into three columns: a sidebar that leads with building
  an assistant and groups conversations by recency, the conversation, and the
  workspace. Files now have a real home with images and documents listed and
  one-click attachment. Per-agent settings live in the workspace; provider,
  model, and key stay in one general settings dialog.
- Extract `chat_sidebar.js` and `chat_workspace.js` as pure, unit-tested
  models, joining `chat_flow.js`, `chat_markdown.js`, and `chat_history.js`.
- The agent builder can start from any shipped assistant instead of an empty
  checklist.
- Fix a stuck workspace panel: an entrance animation with `both` fill on an
  initially `display:none` element keeps the keyframe's start state forever,
  leaving the panel 12px off its anchor. Entrances now fill `forwards`, the
  panel is unhidden before the class lands, and its reveal is opacity only.
- Stop animating layout width. The rail and the stage dashes now change size by
  transform, which is what kept the rail stuck at its old width when its grid
  track resized underneath it.
- The stage rail responds to pointer proximity: each dash reads its own
  distance from the cursor and scales accordingly, skipped under reduced
  motion.
- Clamp the panel with `overflow: hidden` and anchor the workspace overlay to
  the whole grid, so a docked panel can never widen the viewer around it.
- Built-in agents keep their declared order in `/agent` and the panel, so the
  general assistant is listed first rather than alphabetically.
- `ifc-console agents blocks` lists the capability blocks every agent is built
  from.

- Add `ifc-console dev`, a one-command harness for the browser panel. `--check`
  boots a generated demo project (IFC, PDF manual, drawing, measurement recipe)
  on an isolated console home, walks every panel feature through the real HTTP
  routes with an offline rehearsal model, prints a table, and exits without
  opening a browser tab. Without `--check` it serves the panel and opens at
  most one tab, only when a terminal asked for it.
- Add the offline `rehearsal` provider used by `ifc-console dev`. It speaks the
  normalized provider event vocabulary, walks the real multi-round tool loop,
  and is registered only under `dev` or `IFC_CONSOLE_DEV=1`.
- The test suite can no longer launch a browser: `/viewer`, `/chat`, and
  `/agent` command tests recorded their URLs instead of opening tabs. A full
  `pytest` run previously opened one Chrome tab per command test.
- Add `node --test tests/ui` over the panel's pure modules (markdown rendering,
  the context-flow reducer, the local conversation archive). No npm install and
  no browser; `pytest` runs the same suite and fails when a pure panel module
  has no test.
- Assemble every agent from one shared list of capability blocks
  (`ifc_console.agents.blocks`). Adds `spatial`, `quantities`, `validation`,
  `clash`, `ai-audit`, and `code` beside the existing blocks, and composition
  now degrades instead of failing when a block's tools are unavailable, telling
  the model in its prompt what it cannot do.
- Mark everything an agent writes. Values land in `IfcConsole_AI_Measurements`
  or `IfcConsole_AI_Properties`, each accompanied by an `AI_Provenance` record
  naming the agent, model, method, source document, unit, confidence, and
  ChangeSet id. Add `measure__propose_property_value` for document-defined
  properties and the read-only `list_ai_authored_properties` tool, so the whole
  AI-assisted layer is findable and separable by prefix.
- Standing instructions written in the panel now become part of the agent's
  system prompt instead of a suffix on one message, and changing them rebuilds
  the thread.
- Rework the chat panel shell: a collapsible sidebar listing agents and saved
  conversations, a context panel naming the blocks an agent is made of, a stage
  rail (Scope, Evidence, Method, Verify, Propose) driven by the tools that
  actually ran, proposal cards carrying their provenance, an instructions
  shortcut in the composer, and deletion for custom agents.
- Report missing agent dependencies before they fail. `/api/agents/capabilities`
  and a panel banner name what is missing and print the exact repair command for
  the running interpreter, whether it is a uv tool, a venv, or a system Python.
- A rejected viewer token now says the link has no valid access token, forgets
  the stale remembered token so the next fresh link works, and points at
  `/viewer` for a new URL.

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
