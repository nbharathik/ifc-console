# Changelog

## Unreleased

### Packaging and product boundaries

- Make `ifc-console` the complete deterministic IFC product: console/TUI,
  MCP, SDK, operations, workflows, and the local Three.js/web-ifc browser
  viewer now ship together and work without an LLM or provider credentials.
- Split LLM orchestration into the optional `ifc-console-agents` distribution,
  including the agent SDK, providers/chat, built-in and custom packs, browser
  panel, devkit/testing helpers, document/vision support, and LangGraph
  integration. Agent code uses the canonical `ifc_console_agents` namespace
  and registers with core through `ifc_console.extensions`.
- Make `ifc-console-agents` batteries-included: PDF text and page rendering,
  LangGraph, and SQLite checkpoints are normal dependencies with no agent
  feature extras to discover. Keep `ifc-console[viewer]` and
  `ifc-console-viewer` as one-release compatibility no-ops/shims so existing
  install commands do not fail abruptly.
- Keep the core viewer free of dead agent UI: an installed agent extension
  contributes its browser panel declaratively, and the shell loads that
  panel's JavaScript and CSS only when it is available and opened.
- Enforce release budgets of 2.5 MB for the core wheel and 1.0 MB for the
  agent wheel, plus a 3.0 MB combined cap, alongside content allowlists and
  dependency-boundary checks.

### New capabilities

- Add `compare_models`: diff two open IFC revisions and report what was added,
  removed, moved and changed, grouped by change kind and class, with tolerances for
  what counts as a move. Comparing revisions is one of the most common questions in
  real coordination work and the product could not answer it at all.
- Add `query_spatial`: geometric relation queries the selector grammar cannot express.
  What is above or below this, what does this pass through, what is inside this space
  or this box, what is within a given distance. Every relation is computed from
  triangle meshes, so it works on the stock ifcopenshell wheel with no OpenCASCADE.
- Add `check_model_health`: the data-quality question `validate_model` does not
  answer. Duplicate GlobalIds, orphaned elements, degenerate or zero-volume solids,
  wildly misplaced geometry, unit sanity. The expensive geometry checks are skipped
  with a stated reason on a model too large to walk, rather than silently.
- Give the LLM real camera control. `control_viewer` gains `set_camera` and `fit`,
  both speaking the model's own axes in metres, and the viewer's reported context now
  carries the full camera (position, target, up, projection, field of view, distance,
  and how much world one screen pixel covers), the viewport size, and how many
  elements are hidden, isolated or ghosted. An agent that can read the camera and set
  it can compose plan views, elevations and walkthroughs out of primitives.
- Give `isolate`, `hide` and `select` an optional selector, resolved server side, so
  "isolate the doors on Level 2" is one call instead of a query followed by pasting
  five hundred GlobalIds back.

### Viewer

- Keep the top workspace row for IFC document tabs only. The agent name and
  settings stay in the agent header, IFC tabs begin at the viewer edge, and
  focused elements no longer accumulate in a second tab row; Show all is the
  single visibility reset.
- Give upward scrolling priority over a streaming answer. A small wheel or
  trackpad move now pauses bottom-following immediately, inference continues to
  render below the held viewport, and batched tokens no longer force a layout
  scroll on every event.
- Let the Agent workspace stand on its own: every IFC tab can close, the top
  brand mark returns to an Agent-only view, the active IFC has a one-click
  reopen control, and the file menu consistently lists every attached model.
- Keep parsed IFC revisions warm across model-tab switches, retain a separate
  GlobalId selection for every IFC, and send all selected model contexts to the
  Agent panel instead of replacing them with the last visible tab.
- Turn GlobalIds in Agent output into direct model navigation: a click opens the
  containing IFC and frames the element, while selecting an id and pressing I
  opens and isolates it.
- Keep the Agent panel's transcript isolate shortcut inside the panel, so pressing
  I after clicking an object in the 3D view isolates that object again.
- Draw silhouette edges, and ghost the surrounding context instead of hiding it
  outright, so an isolated element keeps the spatial reference that makes it legible.
- Let the user choose units and precision, defaulting to the file's own unit. The
  viewer previously printed metres regardless of what the model was authored in.
- Extract the measurement mathematics into `measure_math.js`, a pure module with no
  DOM and no WebGL, and cover it with real numeric tests: a known rectangle's area and
  perimeter, a 3-4-5 triangle's angles, non-planar and degenerate outlines, axis
  inference thresholds, oriented-box extents, and unit round-tripping. These functions
  produce numbers that end up in reports and in IFC properties, and until now the only
  coverage was a test that grepped the source for string literals.
- Decide instancing once from the whole model instead of promoting a geometry to an
  instanced mesh the second time it appears, and bucket merged geometry against a real
  world grid rather than a 2x2x2 split of the running bounds, with a staging budget so
  a fine grid cannot hold the whole model in CPU memory before the first flush.
- Make the hover snap probe an asynchronous readback, so pointing at the model no
  longer stalls the pipeline on a synchronous pixel read. The click path stays
  synchronous deliberately: awaiting it would let the double-click handler run before
  the second point was recorded.
- Scope the measure-mode keys to the viewport. `S`, `X`, `Y`, `Z` and Backspace used to
  fire while the user was typing in the chat composer.

### Agent panel and runtime

- Keep transient failures out of the conversation: compact, dismissible notices now
  overlay the panel header, strip developer stack locations, and explain IFC elements
  with no viewable geometry in plain language. Move the live-run Enter/Escape reminder
  behind a keyboard icon so starting and stopping a response never shifts the composer.
- Make bare `/agent` open the General assistant on the first Enter. Use
  `/agent list` when the full built-in and custom assistant catalog is wanted.
- Always offer retry, add continue after a length-capped answer, and add
  edit-and-resend.
- Move the readable proposal card to approval time and persist the decision, so the
  highest-stakes click in the product is made against what it actually describes and
  leaves a trace in a reopened conversation.
- Stop discarding the conversation when the agent or the model changes.
- Extend slash commands and at-mentions to skills, saved views and the live selection.
- Report `tool_progress`, so a call that takes ten seconds is visible while it runs.
- Resolve the delegation deadlock the audit found rather than leaving it in the tree.

### Tool surface

- Keep the launcher and all six viewer operations in the shared MCP/agent
  catalog while the browser surface is off. Codex, Claude Code, and other hosts
  can cache `tools/list`, call `open_viewer`, and continue with control and
  screenshot calls on the same connection.
- Publish tool tags and required IFC Console capabilities in MCP metadata, and
  extend `describe_capabilities` with live policy, transport, optional-package,
  and viewer-connection availability.
- Publish a per-tool `data_schema` and stop advertising 54 byte-identical Envelope
  output schemas. A bare envelope schema is the same for every tool, so publishing it
  cost every client thousands of characters and taught it nothing, and declaring one
  also made the transport send each result twice. The text block still carries the
  whole envelope, so nothing became unreachable.
- Accept the common spellings of the same argument. The surface says `query` in one
  tool, `selector` in another and `term` in a third, and renaming any of them would
  break callers, so the alternatives are accepted as aliases while the canonical name
  stays in the published schema. Writing a benchmark against these tools took three
  attempts to call them correctly with the schema in hand.
- Drop the derived titles Pydantic puts on every field, each of which restated the key
  it sat under.
- Document the whole selector grammar in the surface the model actually reads.

### Correctness

- Fix a precision defect that made every derived volume wrong on a georeferenced
  model. `mesh_volume` summed signed tetrahedra over absolute world coordinates, so
  float64 cancellation destroyed the result: measured on a national-grid offset, a
  glass pane reported -52% and a slender bar +647%. The mesh is now centred before the
  sum, which is mathematically identical and numerically stable. The wrong number had
  been flowing into `probe_element.volume`, prismatic confidence, `compute_quantities`
  GrossVolume, measurement reports, and from there into AI-authored IFC properties.
- Fix silent loss of an edit when a mutating run is cancelled or times out. The handler
  caught `Exception`, but `asyncio.CancelledError` is a `BaseException` and cancellation
  is routine (the Stop button, the run deadline). The worker finished the mutation while
  the caller unwound, so the model stayed flagged clean and the next open discarded the
  edit with no unsaved-changes prompt.
- Fix measurements that could be taken through a wall or through a section cut. Snap
  candidates were filtered only by element visibility, with no clip-plane test and no
  depth test, while the candidate net grows with zoom. In an orthographic top view a
  wall's top and bottom corners project to the same pixel, so a plan measurement could
  silently take one end at z=0 and the other at z=3.0 and report a 3 m error while the
  on-screen line looked horizontal. Candidates behind the visible surface, or clipped
  away by a section, are now rejected; anything in front still snaps, so silhouette
  corners keep working, and the snap hint states the depth when a point is not on the
  visible face.
- Stop an assistant's edit from deleting the user's measurements. Any revision bump
  rebuilds the scene, and the rebuild cleared every measurement unconditionally,
  including the server's copy, so `get_viewer_measurements` could not even recover them.
  Measurements are now anchored to GlobalIds in their element's own frame and replayed
  across a rebuild, a saved view and a reload, with anchors whose element is gone dropped
  visibly rather than silently.
- Replace preview-string truncation with real paging. A result over the character limit
  became `{preview, note}` with the rows gone while still reporting success, and the
  preview re-embedded the whole envelope as an escaped string, so the truncated envelope
  could exceed the limit it enforced and downstream front-cuts then dropped the very keys
  that signalled truncation. Measured: `query_elements(limit=400)` returned zero rows
  against a schema advertising 500. Results now keep the rows that fit and state the exact
  next offset, with the truncation signal first in the payload so a front-cut cannot lose
  it.
- Stop one conversation's teardown from denying another's approval. Pending approvals
  were one flat dictionary resolved wholesale on stream teardown, so with two tabs open,
  finishing conversation A silently denied conversation B's on-screen card and the user's
  later click returned a conflict.
- Answer instead of failing when the tool budget is spent. The loop injected a result
  telling the model to answer with what it had and then raised immediately, so the model
  never got the round to act on it: the panel showed a red error and up to eight rounds
  of paid context were discarded. A run that spends its budget now gets one tool-free
  wrap-up round and reports `stopped_reason`.
- Never persist a thread whose last assistant message carries unanswered tool calls.
  Such a thread made every later message in that conversation fail, with no repair in the
  UI short of deleting it.
- Stop a second viewer tab from erasing the first tab's selection and measurements. The
  hub adopted the newest frame unconditionally while every new tab publishes an empty
  selection, so the assistant answered "nothing is selected" and measurement reports came
  out empty while the user's dimensions were still on screen. Selection and measurements
  are now tracked per tab and read from tabs showing the active model.
- Refuse viewer commands and screenshots against a half-disposed scene. A rebuild
  disposes the scene and then parses for seconds, and commands arriving in that window
  reported "none of those elements are in this model", so the agent concluded its
  GlobalIds were wrong; a screenshot returned an empty viewport as visual evidence. The
  viewer now reports its scene state and such commands wait briefly, then fail with
  `VIEWER_BUSY`.
- Stop Enter from destroying a running answer. Pressing Enter while the agent was
  streaming killed the run and did not send the message, while the composer stayed
  enabled and still read "Enter sends". Enter now queues the message and the hint says so.
- Fix the area tool counting a rectangle as six points. Every click committed a vertex
  and the closing double-click committed two more, and the degenerate edges corrupted the
  reported area, perimeter and flatness. Outlines now collapse coincident points and
  refuse to commit fewer than three distinct ones.
- Make `show_all`, `select` and `isolate` do what the matching UI buttons do. `show_all`
  released two of the four visibility gates the viewer actually reads, so the tool
  returned `{isolated: 0}` while one element was still alone on screen. Visibility results
  are now read back from state rather than asserted, and selecting or isolating an element
  that something else was hiding reveals it instead of framing empty space.
- Hide the measurement overlay during the identity and depth pick passes, so a click over
  a snap glyph can no longer decode as the wrong element or measure the preview sprite as
  the nearest surface.

### Measurement and modelling

- Measure spaces. The derived-quantity fallback dropped `IfcSpace` along with openings and
  annotations, so a selector that explicitly asked for rooms got nothing; spaces now report
  the `Qto_SpaceBaseQuantities` vocabulary (height, gross floor area, gross volume).
- Report surface areas from the geometry probe: total surface plus top, bottom and the two
  side pairs, split in the element's own frame so a wall on a skew grid reports one side
  pair rather than two halves.
- Add dotted property projection to `query_elements`, so a caller can ask for
  `Pset_WallCommon.FireRating` as a column the way `export_csv` already could.

### Performance

- Collapse three passes over the model's placements into one at load, and cache each
  placement's world box from the bounds probe that previously discarded it, so the scene
  is batched against the whole model from the first chunk instead of a partial box.
- Add a per-session mesh cache keyed on element id. Measured on a 14,400-element model:
  probing one element cost 249 ms and probing twenty-five cost 246 ms, because every call
  built a fresh tessellation iterator, and agents probe then re-probe the same elements.

### Other

- Make measuring read like a CAD tool. Snap previews wear the CAD glyph for
  what they hit (square corner, triangle midpoint, circle face centre, diamond
  edge); a rubber band within a few degrees of a model axis sticks to it in
  that axis's color, SketchUp style, with Shift hardening the stick; committed
  measurements carry their value as a floating 3D tag; Backspace takes back
  the last click and Escape sheds points and lock before mode. Each
  measurement is one group: hovering its row in the list lights it up in 3D,
  and the row's x removes exactly that one.
- Close the UI-parity gap for agents: `control_viewer` gains `select` (frames
  elements and opens their properties for the user) and `hide` (the complement
  of isolate), so everything the person can do to the viewport, the LLM can
  do too.
- Make element references in answers live. Any IFC GlobalId an assistant
  writes renders as a chip; clicked beside the viewer it selects and frames
  that element, on the standalone chat page it copies the id.
- Make the agent pipeline context-aware. Composition injects a session context
  section into every agent that holds the skills block: the open model and
  mode, plus an index of the project's saved skills (one line each, capped),
  harvested from one cheap read at compose time, so round one is never spent
  discovering what the host already knows. Panel messages sent while elements
  are selected in the 3D viewer carry those GlobalIds with the prompt, so
  "this wall" resolves without a get_viewer_selection round. The `ifc-console
  dev` scenario now seeds a demo skill and the rehearsal checks assert the
  whole loop.
- Fix a TypeError that killed any agent-panel tool call whose own argument was
  named `name` (`control_viewer` focus and saved views, `get_agent_skill`,
  `save_agent_skill`): the argument collided with the workbench call's first
  parameter. The operation name is positional-only now, and a regression test
  drives the exact panel toolset path.
- Make measurement markers a constant on-screen size. The marker radius came
  from the model span, so zooming close to a small element filled the screen
  with one orange sphere; markers now derive their world size from the camera
  distance every frame.
- Show a live measure preview: the point the click would land on (snapped
  features in blue, plain surface in gray), the rubber band from the last
  click, and the running length beside the cursor. The X/Y/Z axis lock is a
  toggle now instead of a hold, colors the rubber band in the axis's color,
  and survives across clicks until released.
- Anchor navigation to what is in front of the camera: the orbit pivot slides
  along the view axis to the surface depth under every gesture (never turning
  the view), the wheel re-anchors after cursor zoom, a double-click frames the
  element under the cursor, and the zoom floor drops to model-span/100000 so
  small parts in large files are reachable.
- Lay the View tools popover out in two real columns. The old grid gave every
  section a full-width title and one column of content, so the panel was a
  full-height scroll; each section is one grid item now and the whole panel
  fits without scrolling.
- Add `open_viewer`: an always-registered tool that turns the viewer surface
  on and opens the tokenized page in the local browser, so an external MCP
  client (Claude Desktop, Claude Code) can launch the viewer itself and then
  use every viewer tool. Sessions on stdio get VIEWER_UNAVAILABLE with the
  `serve --http` hint; the token never enters the tool result.
- Add skill import: drop externally written markdown skills into the project
  with the Skills tab's Import button or
  `POST /api/agents/skills/import?name=<file>.md`. Front matter wins over the
  filename, imports never overwrite, and a taken name gets a numeric suffix.
- Add `analyze_element_geometry`: the full measurement probe for a few
  elements. It reads exact profile parameters from the IFC definition
  (parameterized I/U/Z/C/T/L, rectangle, circle and hollow profiles, arbitrary
  and centre-line outlines, material profile sets) and independently slices
  the triangle mesh across the element's long axis to measure the cross
  section: width, height, wall-thickness distribution with a flange/web
  two-group split, perimeter, and area from the assembled section loops. Both
  sources merge into one `dimensions` block with a named source per value;
  disagreement over 5% is flagged, and a wall-style element whose extrusion
  axis is not its long axis is flagged `profile_plane_differs` rather than
  force-compared. Works headless, without the viewer and without vision.
- Add `export_measurement_report`: an audited markdown measurement report for
  a few elements (identity, merged dimensions with sources, measured cross
  section, profile definition, bounding box, stored quantities), written to an
  allowed path and registered as a `measurement-report` artifact.
- Add agent skills: reusable procedures saved as markdown under
  `.ifc-console/agents/skills/`, listed to agents through
  `list_agent_skills`, loaded with `get_agent_skill`, and recorded with
  `save_agent_skill` (a `file:write` tool, so agent surfaces ask for approval
  before anything lands on disk). A new `skills` capability block teaches
  every preset that holds it the check-reuse-record loop; the general and
  measurement agents hold it by default, and the panel shows the files under
  Agent workspace > Skills.
- Add viewer focus tabs. `control_viewer` gains `focus` and `unfocus`: focus
  opens the given elements alone in a named tab on a strip under the top bar,
  one tab per analyzed object, with All restoring the whole model. Tabs are
  GlobalId-keyed so they survive rebuilds, and the strip stays in step with
  manual isolate and show-all. The viewer capability block now includes
  `control_viewer`, so panel agents can drive the viewport they describe.
- Report coordinates in the model's own axes. web-ifc hands geometry back
  y-up and slides the model to the origin so a georeferenced file keeps its
  precision; both are right for drawing and wrong for saying where something
  is, and the viewer had been reporting the viewport's numbers as the file's.
  One axis came out negated and one carried the origin shift, so every
  measured point, element centre, laser origin and section position was wrong
  by those amounts. The parser now ships web-ifc's coordination matrix and one
  conversion runs in each direction. Verified against `ifcopenshell`: a wall
  whose true centre is (1.5, 0.15, 1.5) used to read (1.5, -0.15, -1.5) and
  now reads (1.5, 0.15, 1.5).
- Measure elements on their own axes. A wall at forty degrees has a thickness;
  the world-axis box around it reports the diagonal instead. Length, width and
  thickness now come from the element's oriented box, which is the local box
  the parser already ships still attached to its placement.
- Report real surface area and volume, computed from the tessellation during
  the parse, deduplicated so it costs one pass per shape however many times it
  is placed. A 5.0 x 3.0 x 0.24 m wall reports 33.840 m2 and 3.600 m3 exactly.
- Snap to the element's own box rather than the world-axis one, and to any
  point along an edge as well as to corners, edge midpoints and face centres.
  Candidates come from every element near the cursor, found through a new
  uniform grid over element boxes, so the corner where two walls meet offers
  both walls instead of only the one in front.
- Show the snap before the click commits to it: a marker on the point and its
  name beside the cursor. Rate limited by what the pass actually costs on the
  model in front of it rather than by a fixed interval.
- Add angle and area measurement, on the same clicks and the same snapping as
  distance. Three points give the angle at the middle one; an outline closes
  on **Enter** or a double-click and reports area, perimeter and how far from
  flat it was. **A** and **R**.
- Add orthographic projection (**P**). A length read off a perspective view
  means nothing; in parallel projection it reads the same anywhere in the
  frame. The swap keeps the eye where it is and the same amount of model on
  screen, and picking and measuring work in both.
- Add a section slice depth. A cut answers "what is below this level"; keeping
  a slab beyond it is what a floor plan is.
- Address sections in real heights. `set-section` takes metres in the model's
  axes and reads back the same way, instead of a fraction of a bounding box
  nobody can see.
- Carry projection, selection, isolation and section in a saved viewpoint, not
  just the camera.
- Let the server drive the viewer and hear back. The viewer's command dispatch
  is one function reachable from the panel and from the socket, so `run_command`
  sends a command and returns its result, with the viewer's own refusal as the
  error. New `control_viewer` tool covers sectioning, orientation, projection,
  isolation, measurement and viewpoints.
- Deliver every measurement to the server, not just distances. `_clean_measurements`
  only recognised a distance, so an element size or a clearance was measured,
  sent, and dropped on arrival: `get_viewer_measurements` reported nothing had
  been measured. Angles, areas, sizes and clearances now arrive with their kind.
- Finish loading a model in a background tab. The build yields on an animation
  frame, and a hidden tab gets none, so a model opened in a background tab
  stopped at the first pause until someone switched to it.

- Remove the zoom limit, which was the near plane. `camera.near` was set once,
  when the model was framed, at a thousandth of the framing distance: on a
  200 metre site that puts it 200mm in front of the lens, so closing on a
  detail clipped it away and the camera appeared to stop. Near and far now
  follow the live pivot distance, which takes the usable zoom range on the
  demo model from roughly 40x to 17,000x.
- Fix panning and orbiting that crawl when zoomed in. Both are measured from
  the pivot, and zooming to the cursor can walk the pivot to within
  millimetres of the lens, at which point a full drag across the screen moves
  the camera almost nowhere. The pivot now has a model-relative floor.
- Snap measurements to element features. Clicking twice at the same corner
  used to give two points 143mm apart; it now gives the same corner twice.
  Corners, edge midpoints and face centres are matched in screen space, the
  way the person aiming does it, and each measurement records which feature
  each end landed on. **S** toggles it.
- Cast the clearance laser against element bounding boxes rather than the
  merged triangle buffers. The source geometry is released once it has been
  batched, which is the right call for a large model; the result names the
  method so nobody mistakes it for a mesh-exact distance.
- Put the measurement tools in **View tools > Measure**: size of selection,
  clearance around selection, snap toggle, and clear. They run the same code
  path as the assistant's commands, so a value produced from the panel and one
  quoted in an answer are the same measurement.
- Halve the height of the view-tools popover by laying its sections out in two
  columns, and bound its width.
- Report the failing frame when a viewer command throws. A command that dies
  inside three.js previously surfaced only its message.

- Fix an orbit that crawls once you are close in on a large file. Damping was
  applied per frame rather than per second, so the 0.12 factor that glides at
  60fps needed twenty frames to catch the mouse at the 8-15fps a 50 MB model
  gives while you are close enough to see millimetres. It is now scaled by each
  frame's own duration.
- Move the orbit pivot to what you are looking at. Zooming toward the pointer
  walks the camera away from wherever the model was framed, and orbiting a
  point 200 metres away while standing a metre from a wall sweeps the wall off
  screen before the view has turned. The pivot follows the surface under the
  cursor once the framed one is clearly stale.
- Give measurement the questions a model is actually asked. Hold **X**, **Y**
  or **Z** while placing the second point to lock the measurement to one axis;
  Alt-click an element for its length, width, thickness, diagonal and centre;
  and cast a clearance laser both ways along each axis from a picked point,
  which reports each distance and the full span.
- Let the assistant drive the viewer rather than only read it: `isolate`,
  `show-all`, `set-view`, `measure-element`, `measure-laser`, `measure-points`
  and `clear-measurements` join the command contract. Measurements it makes
  land in the viewer's own list, so the number in an answer is the number on
  screen.
- Raise the panel type scale again, by 15 percent, with nothing left under
  10px. A third of the sizes were 9 or 10px, chosen for a 340px dock and never
  revisited for the monitors this is read on.

- Give the general assistant the `code` block. It held every capability except
  the one that answers a question the structured tools do not cover, so
  "measure this wall the way the manual defines it" had nowhere to go. The
  session gate is unchanged: a run is classified before it executes, ask mode
  refuses anything that would change the model, and read-only runs go to a
  worker process against a copy of the file.
- Split the session control in two, because it was answering two questions at
  once. **Ask/Edit** is what the assistant may touch; **Approval/Auto** is
  whether it stops and asks before touching it. All four combinations are
  reachable, and each is confirmed on the way in.
- Make approval real. The agent has always paused on a protected call and asked
  its host what to do; the panel never answered, so the deny-all default stood
  and the mechanism was invisible. A pause now puts a card in the transcript
  with the tool, its capabilities, and the arguments the model chose, and the
  decision resolves the future the run is blocked on. Denying returns a refusal
  the model can work around rather than failing the run.
- Take saving away from assistants entirely. No stance grants `allow_ai_save`;
  changes live in memory and a **Save** control appears in the composer while
  the model is dirty. `/save` does the same from the message box.
- Stop repainting a selected element flat blue in the 3D view. The fill
  replaced the shading that says what the shape is; selection now tints part
  way and puts the rest into a view-dependent rim, so the silhouette reads as
  an outline.
- Lead the agent page with what the assistant is and what it may do, and fold
  suggested questions, the stage map, and the instruction editor into three
  identical toggles beneath it. The standing "can propose AI-marked changes"
  card is gone; its guarantee moved onto the write-policy mark, which names the
  reserved property sets on hover.
- Give the agent workspace one vertical rhythm and symmetric insets. Blocks set
  their own margins, so the gaps ran 18 / 0 / 16 / 7 / 16 / 7 / 14 down a single
  column, and the scroller's gutter left every block 20px from the left and 28px
  from the right.
- Make a tool row a line rather than a card: 27px instead of 44px, so a 39-tool
  agent shows 17 names at once instead of 10.
- Centre session notes in the column. They are the panel talking about the
  session, not a turn, so they no longer align with either speaker.

- Zoom toward the pointer in the 3D view. The camera dollied toward the orbit
  target, which on a site-scale model sits in the middle of the whole file, so
  looking at a corner meant zooming into the centre and panning back out.
  Double-click now re-pivots the orbit on the point under the cursor, and
  Shift joins Ctrl and Cmd as a modifier for adding to the selection.
- Stop the 3D viewport drawing a focus ring around the model on every click.
  It takes focus so the arrow keys pan, which `:focus-visible` could not
  distinguish from a Tab press.
- Move the viewer selection into the composer as a context chip beside the
  attached files, with a control to frame it and one to clear it. The old rail
  chip reported "Whole model" when nothing was selected, which stated a
  constant as if it were state.
- Gather everything that belongs to one message behind a **+** control in the
  composer: attach a file, attach the current 3D view, mention project
  content, frame the 3D selection. The paperclip and camera used to sit in the
  rail beside the model and mode selectors, mixing one-off context in with
  standing configuration.
- Add `@` mentions for project documents and `/` commands for panel settings,
  sharing one keyboard-navigable popup. Accepting a mention writes the file
  name into the prompt and attaches it to that message.
- Fold **Pipeline** into the agent it describes. Which stages an agent can
  reach follows from the blocks it holds, so a separate page described no
  agent in particular; each agent's overview now carries **How this assistant
  works** with its strategy and reachable stages.
- Put a conversation turn's mark in the gutter beside its first line. A 24px
  mark on a row of its own left an empty band next to every turn.
- Raise the panel type scale by about 10 percent. The sizes were chosen for a
  narrow dock and read small on a large monitor.
- Restyle Ask/Edit so ask reads as the calm default and edit carries the
  warning wash and border, rather than two saturated colours on a transparent
  field. All four states clear WCAG AA in both themes.

- Centre **Agent workspace** on the window and let a click outside it close
  the sheet, alongside Escape and its own control. It used to sit pinned to
  the right edge with no way out but the close button. An unsaved Agent setup
  draft still requires a deliberate dismissal.
- Paint **Content** from the workspace payload the panel already holds. Both
  endpoints return the same shape, so "Reading workspace content..." was a
  wait for data that had already arrived. Managed-file digests are also cached
  by size and mtime, which removes the repeat hashing that made listing a
  library of large manuals slow: a 20 MB reference went from 48 ms per listing
  to 0.3 ms, and it still rehashes the moment the file changes.
- Stop programmatic focus from wearing the keyboard focus ring. Opening a
  surface with the mouse outlined whichever control focus landed on, because
  `:focus-visible` cannot tell a script's `focus()` from a keypress. Keyboard
  navigation keeps the ring unchanged.
- Mark a conversation turn with one icon instead of a repeated name and role
  word. "IFC workbench" and "Request" said the same thing on every turn; the
  assistant's name now lives on the mark's tooltip and accessible name.
- Replace the agent overview's labelled tiles and separate limits block with
  one row of marks: tools, content, viewer, write policy, tool rounds, and
  timeout, each naming itself on hover. Suggested questions lost their third
  line and their **Use** label to the tooltip and an arrow.
- Grant project content in bulk. **Select shown** and **Clear shown** act on
  the filtered set and shift-click extends a range, so granting a folder of
  manuals is one gesture rather than one request per file.
- Add `set-selection`, `clear-selection`, and `focus-selection` to the viewer
  command contract, and make the composer's selection chip operate them:
  click to frame the selected elements, Alt-click to clear them.

- Fix every panel icon rendering at one size. The icons carried their size in
  a `style` attribute, which the panel's own `style-src 'self'` policy blocks,
  so all of them silently fell back to 16px and each page load logged around
  forty CSP violations. Sizes now travel as a `data-size` attribute.
- Decode the agent event stream with the shared AI SDK transport decoder. The
  panel carried a second, stricter decoder that dropped every event when a
  frame arrived with CRLF endings or without the space after `data:`. Proposal
  cards read the same normalized wire shape, so the card and the AI SDK
  boundary can no longer disagree about a field name.
- Arm a conversation or custom-assistant delete before it acts. Both used to
  delete irreversibly on one click of a control that sits beside the one that
  opens the row, while bulk **Delete all** already asked first.
- Show composer uploads while they index. A chip appears immediately with a
  progress state, send waits for it, and a failed upload removes its own chip
  instead of leaving an attachment with no path.
- Filter the **Tools** view by name or description, and build a tool row's
  argument detail only when it is opened. Listing an agent's tools built about
  1,400 nodes up front; it now builds about 175.
- Give the block reorder controls distinct up and down arrows. Both rendered
  the same upward icon.
- Apply one disabled treatment across the panel. Only the send button had one,
  so every other disabled control still lit up on hover.
- Keep a copy control's resting label across rapid clicks, and label each one
  by what it copies.
- Remove the unreachable workspace Files view, its card helpers and CSS, the
  unused history restore path, one duplicated initials helper, and a dead
  icon.

- Consolidate assistant inspection, Agent setup, provider and model settings,
  shared Content, and appearance into one bounded **Agent workspace** modal.
  Its left rail separates Agents, Pipeline, Capabilities, Tools, Content,
  Models, and App while the conversation remains visible. Expandable tool rows
  show the real argument contract, and each assistant can be inspected,
  duplicated, or edited there.
- Add a searchable shared Content library with persisted per-agent access.
  Workspace uploads become standing references, while composer uploads and
  viewer screenshots live in a hidden turn-only area and are available only to
  the message that attached them.
- Move the active AI model, Ask/Edit mode, open IFC model, current selection,
  file attachment, and screenshot evidence controls into a compact composer
  rail. Viewer model and properties panels now use quiet midpoint edge tabs
  when closed and an internal close action when open.
- Add a dependency-free AI SDK UI-compatible message, part, stream, and
  transport boundary plus an optional LangGraph adapter behind the existing
  agent event contract. The local browser harness remains provider-free and
  exercises the same integrated viewer and chat surface.

- Rework the browser chat into an IFC agent workbench. Requests and assistant
  turns now have distinct headers, tool calls remain in execution order, only
  newly arriving turns animate, and a tool-heavy stream follows its final
  answer while still allowing the reader to scroll upward.
- Replace assistant-name navigation with a dedicated **Inspect** control. The
  overview, tools, files, and instructions inspector overlays the conversation
  without changing its geometry and becomes a bottom sheet on compact panels.
- Move the agent workbench to a resizable left workspace so the conversation
  and IFC viewer remain visible together. The cleaner toolbar, aligned
  conversation sidebar, and model-aware composer keep the active file,
  selection, mode, and provider visible without duplicating chrome.
- Add compact **Agent setup** for profile, ordered capabilities, instructions,
  starters, and bounded adaptive, evidence-first, or fast-scan workflows.
  Advanced tool budgets stay collapsed until needed. Custom blueprint
  workflows round-trip through the workspace API and enforce their configured
  tool-round and tool-call limits.
- Add dependency-free root `npm run dev`, `npm run harness`, `npm run check`,
  and `npm test` commands. The fresh-check path can delete only its exact
  disposable harness project and refuses every explicit project directory.

- Rebuild conversation state around immutable run and conversation identities.
  Late stream events can no longer attach a thread, answer, or busy-state
  finalizer to the assistant or conversation opened after it. Stops and errors
  survive reloads, tool-only turns remain visible, uploads cannot cross an
  assistant switch, and plain-chat request history is bounded.
- Add a confirmed **Delete all** action to chat Settings and a race-safe
  `POST /api/agents/threads/clear` endpoint. It fences new runs, cancels active
  ones, removes browser transcripts plus owned project thread files, and cannot
  be undone by a late stream save. The rebuilt panel performs a one-time clean
  start from the incomplete v1/v2 state.
- Scope chat history to an opaque project id plus the open model fingerprint,
  and fork context when provider, model, base URL, tools, or standing
  instructions change. A visually fresh configuration no longer inherits
  hidden context from another project or configuration.
- Fix viewer CSS leaking fixed `aside` geometry into the nested chat sidebar
  and workspace. Closed inline panels now leave their zero-width tracks,
  compactness follows the remaining conversation width, touch actions use
  discoverable 44px targets, and the dock reserves a usable 3D canvas while
  reconciling open/close races with the panel lifecycle.

- Show every tool call where it ran. A run is now an ordered list of blocks:
  the model's text, then the tool it reached for, then the next sentence. Each
  tool is a card carrying the arguments the model chose and the envelope the
  console returned, folded away until asked for and opened automatically when
  the call failed, where it prints the console's own message and hint instead
  of the word "failed". `/api/chat/stream` and `/api/agents/stream` carry the
  arguments and a bounded, image-free preview to make this possible.
- One control per surface. The sidebar toggle opens the assistant and
  conversation list, the assistant's name opens its workspace, and the gear
  opens settings. Settings had four doors, two of which looked like something
  else; the provider line is now a status readout, not a third way in. The
  hidden assistant `<select>` is gone with them: it duplicated the sidebar and
  put an invisible control in the tab order.
- Fix the conversation collapsing to a 52px sliver whenever the sidebar
  opened. An absolutely positioned sidebar leaves the grid flow, so the
  conversation fell into the sidebar's track; each panel now owns its grid
  column by name and an overlay can never displace it. Opening settings no
  longer reopens the sidebar over the transcript on the way out.
- Rebuild the sidebar as one plain list: no pin, no hover reveal, no 52px icon
  stub with clipped labels. It is open or closed, remembers which, and becomes
  a drawer only when the panel is too narrow for a column. Plain chat is listed
  like any other assistant, so there is a way back to it.
- Fix reopening a saved conversation, which threw on a control that had been
  deleted from the markup, and lost the tool trail even when it did not.
  Transcripts now store what was drawn, so a reopened conversation still shows
  which tools ran and what they returned.
- Fix the composer's instructions button, which opened Settings and focused a
  hidden textarea. It opens the workspace on the Instructions tab.
- Describe plain chat in `GET /api/agents/workspace?agent=`, so the panel's
  default surface can answer "what can this reach" like every other assistant.
- Fix the landing assistant depending on which fetch finished first: a
  background settings save turned "never chosen" into plain chat before the
  agent list arrived.
- Fix `content: "\\203A"` printing the six characters `\203A` beside the tool
  disclosure instead of a chevron.
- Markdown: keep a wrapped bullet in its list item, render heading levels
  instead of making everything an `h3`, and support blockquotes.
- Accumulate token usage across tool rounds instead of reporting only the last
  round's, and report what a tool preview left out instead of silently
  stopping at 50 rows.
- Read `artifact_writes` from the workspace payload under the name the server
  sends it.

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
