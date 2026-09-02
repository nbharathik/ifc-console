# 3D viewer

The bundled browser viewer shows geometry, properties, selections, highlights,
and measurements. It runs on localhost and needs no LLM, provider account, or
API key.

## Install and open

```bash
uv tool install ifc-console
# or: pip install ifc-console
```

Open a model, then run `/viewer`.

| command | use |
| ------- | --- |
| `/viewer` | enable the viewer and open the browser |
| `ifc-console --viewer` | enable it at startup |

`/viewer` has no subcommands: close the browser tab when you are finished. If
another tool needs the address, `/copy viewer` copies it without expanding the
viewer command itself.

`/viewer` is always the viewer-only product surface. It does not restore or
load an Agent panel, even when `ifc-console-agents` is installed. `/agent`
opens the Agent workspace and attaches it beside this same viewer.

The browser assets, Three.js, web-ifc, and WASM ship in `ifc-console` itself.
`ifc-console doctor` treats missing viewer assets as a damaged installation.
The legacy `ifc-console[viewer]` extra and `ifc-console-viewer` distribution are
one-release compatibility no-ops/shims; do not use them for new installs.

stdio-only sessions have no HTTP server and therefore no viewer. Use the
interactive console or `--no-tui`.

## Layout

```text
+-- IFC file tabs -------------------- viewer tools / settings --+
| spatial tree  | 3D canvas                              | properties   |
| and search    | persistent floating tool panels        | psets, qtos  |
+---------------+----------------------------------------+--------------+
```

With `ifc-console-agents` installed, `/agent` asks the extension to mount its
panel beside the viewer. Its JavaScript and CSS load lazily only on that
explicit Agent URL; a normal `/viewer` tab has no Agent code, layout, or memory
cost.

## Shared viewer component

The canvas and all viewport behavior form one reusable component. The viewer
owns selection, camera, visibility, sections, measurements, saved views, and
evidence capture. An attached panel receives a small component facade for
reading context, executing those commands, and subscribing to asynchronous
results. A compatibility DOM-event adapter remains for older integrations.

This boundary prevents feature drift: a measurement or section feature added
to the normal viewer is immediately the same feature used from `/agent`.
Server WebSocket commands use the same command dispatcher, so MCP clients also
operate the exact viewport visible to the user.

| action | control |
| ------ | ------- |
| frame model | ++f++ |
| select | click; ++ctrl++ + click for multiple |
| search | name, IFC class, storey, type, or selector |
| isolate or hide | Visibility icon, or ++i++ / ++h++ |
| transparent context | ++t++; selected elements stay solid and faded elements remain clickable |
| restore the default view | Filters icon, then **Clear all filters** |
| measure a length | ++m++, then two points |
| measure a path | choose Path, click each turn, then Finish or ++enter++ |
| measure an angle | ++a++, then three points: end, corner, end |
| measure an area | ++r++, click an outline, then Finish or ++enter++ |
| navigate while measuring | right-drag orbits; middle-drag pans; scroll zooms |
| element size | ++alt++ + click an element |
| snap on or off | ++s++ |
| parallel projection | ++p++ |
| section | enable X, Y, or Z; its colored plane appears while dragging; set a slice to keep only a slab |
| mesh outlines | Display icon; off shows the default IFC surfaces |
| save a camera | named saved views |
| change panels | drag dividers or use panel buttons |

The viewer help button lists all mouse and keyboard controls.

GlobalIds in optional Agent answers and tool results are live links into the model.
Clicking one opens the IFC surface, finds the attached model that contains it,
selects the element, and frames it. Drag-select a GlobalId in the transcript
and press ++i++ to open its IFC and isolate it; ++i++ also isolates the most
recently clicked GlobalId when focus is outside a text field.

When the agent extension is installed, its panel can remain open while every
IFC tab closes. Click the `>_ ifc-console` mark to return to that panel-only
view, **Open active IFC** to restore the console's active model, or `+` to
choose any attached IFC file. Without the extension, IFC tabs remain the whole
workspace. Viewer settings sit at the right end of the viewer tool rail, so
they apply to the viewer surface rather than an optional panel.

Search accepts ordinary text or IfcOpenShell selectors:

```text
IfcDoor
Pset_WallCommon.FireRating=F30
```

Results use the live in-memory model, including unsaved edits.

## Review tools

- **Properties:** attributes, type, container, materials, properties, and
  quantities for the selected element.
- **Sections:** combine axis planes for storey slices or corner cuts. A slice
  depth keeps only a slab beyond each cut, which is what a floor plan is.
- **Projection:** perspective to understand a model, orthographic to check
  one. In parallel projection a length reads the same anywhere in the frame.
- **Measurements:** length, path, angle, area, element size, and clearance. All of
  them report metres in the model's own axes, and the model can read them back
  with `get_viewer_measurements`. Each measurement also carries anchors naming
  the GlobalIds, snap kinds, and feature relationship it touched. The Agent
  panel pins those records to the source tab's `model_id`, fingerprint, and
  revision, then transforms direction evidence into an object-local or semantic
  intent when it records a reusable skill. World coordinates are not replayed
  on another object.
- **Selected-object geometry:** **Agent workspace > Skills > Analyze selection**
  runs a bounded standard-detail `analyze_element_geometry` call in the
  semantic frame with automatic section stations. It renders the semantic axis
  triad with world vectors, source, confidence, handedness, and ambiguity;
  adaptive profile regions and a representative-station slider; grouped
  measurements with exact IFC or measured badges; and expandable alternatives
  with source deltas, tolerance, uncertainty, and conflict reasons. Unavailable,
  ambiguous, and conflicting coverage remains explicit. The call is read-only
  and is pinned to the selection's model and revision.
- **Measurement skill review:** a structured version 2 skill can be dry-run on
  selected candidates from the same workspace. The table shows applicability
  score, match and mismatch reasons, extracted values, skipped targets, and
  unresolved results. Property proposal is a separate follow-up control after
  review and never occurs during the dry-run.
- **Snapping:** measured points land on real mesh corners, edge midpoints,
  anywhere along an edge, or the exact visible surface, chosen by which is
  nearest the cursor on screen and guarded by the visible surface depth.
  A marker names the feature before the click commits to it. Candidates come
  from every element near the cursor, so the corner where two walls meet
  offers both walls.
- **Saved views:** named viewpoints stored in the browser, carrying the
  camera, projection, selection, isolation, and section.
- **Color themes:** labeled groups with a colorblind-safe legend.
- **Grid and axes:** local visual aids that never modify the IFC model.

The semantic triad and section browser are workspace visualizations of the
version 2 analysis record. They do not draw temporary objects into the Three.js
canvas. A true in-canvas axis or section overlay needs a versioned viewer
component command, renderer-owned geometry, disposal on model or revision
change, and the same model-scoped selection fence. That rendering subsystem is
kept separate from the optional Agent panel instead of coupling panel DOM code
to viewer internals.

When several models are resident, Chrome-like tabs switch which IFC file is
displayed. Tabs can be closed and reopened from the plus button, and each tab
remembers its camera, selection, cuts, visibility, transparency, and
measurements for the session. Up to two recent parsed IFC revisions stay in a
device-aware 64–160 MB cache, so a normal tab switch skips the download and
WebAssembly parse without letting typed arrays grow once per open model. The
cache is given back after a minute out of sight, and the Agent panel's memory
pill or a `release-memory` viewer command drops it on demand. The viewer
renders one model at a time; it does not create a federated overlay.

The Agent panel's workflow library subscribes to this same context. A workflow
that supports `scope: either` offers **Viewer selection** as soon as any IFC
tab has selected elements. The live count stays visible while the library is
open, **Show in 3D** frames the selection, and pressing Run captures the
model-scoped GlobalIds for the workflow agent. Clearing the selection returns
the run sheet to whole-model scope. **Run workflow** in the status bar appears
whenever something is selected and an agent panel is available; it opens the
panel on the workflow library with the selection scope already chosen.

The workflow **Runs** launcher holds this scope beside common key/value prompt
settings. Each execution then gets its own chat-style activity history with
the captured scope, LLM output, tool calls and results, and token usage.
Several workflows can run concurrently against the same captured selection.

## MCP viewer tools

Six viewport tools plus the launcher are always present in the core MCP
catalog. External MCP clients can use them without an LLM inside IFC Console;
installed agent packs consume the same tools:

| tool | use |
| ---- | --- |
| `get_viewer_selection` | read the user's selected elements |
| `get_viewer_measurements` | read everything measured, by anyone |
| `highlight_elements` | color, isolate, and frame elements |
| `apply_color_theme` | show labeled groups and a legend |
| `get_viewer_screenshot` | capture a preset or current view |
| `control_viewer` | operate the viewport and read the result back |

With several IFC files attached, `get_viewer_selection` returns a `selections`
row for every IFC that has selected elements, while its compatibility fields
describe the tab currently on screen. The optional Agent panel shows one selection chip
per IFC and sends all of those model-scoped GlobalIds with the next message.
Pass a selected `model_id` to `control_viewer`, `highlight_elements`,
`apply_color_theme`, `get_viewer_screenshot`, `analyze_element_geometry`, or
`apply_measurement_skill` when the target must be explicit. Geometry and skill
calls do not infer that a GlobalId belongs to whichever file is currently
active. Selection-based viewer actions infer this id when it is omitted, so a
hide, isolate, focus, fit, or measurement cannot be sent to a different active
IFC by mistake. Every command frame carries its model id, and a tab refuses the
command if it switched files while the call was in flight.

The viewport tools require a connected browser tab. An external MCP client does
not need the user to run `/viewer` first: `open_viewer` turns the surface on,
opens the tokenized page in the local browser, waits briefly for its WebSocket,
and returns `ready=true` when an IFC is loaded and the tab is connected. The
catalog stays stable whether a viewer tab is open or closed, which avoids stale
tool caches in Codex, Claude Code, and other MCP hosts. Standalone stdio
sessions get a clear error because they serve no web pages; use the shared
console bridge or HTTP transport for visual work.

A Claude or Codex MCP session can therefore call `open_viewer`, use
`control_viewer` for the camera, sections, visibility, and measurements, and
call `get_viewer_screenshot` for visual evidence. It gains viewer access only;
it does not load the built-in Agent panel or its provider runtime.

## Performance behavior

- Rendering is demand-driven. The viewer requests frames while the camera is
  moving, damping, resizing, or content is dirty, then sleeps when idle.
- Model downloads fill the known-size response buffer directly instead of
  retaining all stream chunks plus a second full-size allocation.
- Parsed-model caching is bounded by count and estimated typed-array bytes.
- Normals stay in normalized 16-bit buffers from the parser worker to the GPU,
  halving their transfer, cache, and graphics-memory footprint. Surface-area
  and volume preprocessing also runs in the worker rather than blocking the UI.
- Repeated high-detail geometry is instanced from its second copy, and instance
  groups below two drawing-buffer pixels are omitted from display frames.
  Picking and measurement probes temporarily restore them for exact results.
- The web-ifc worker is kept briefly for a fast follow-up parse, then terminated
  after 30 seconds idle or when a hidden tab no longer needs it, releasing its
  WebAssembly high-water memory. The inline web-ifc fallback is released on the
  same schedule instead of keeping its heap for the life of the page.
- The viewer publishes its memory footprint (parsed cache bytes, products,
  triangles, whether a parser worker is resident) with every context frame,
  and answers a `release-memory` command by dropping the parsed cache and any
  idle parser. The Agent panel uses both to keep a long run from swapping.
- Completed parser callbacks are detached immediately so model buffers and
  geometry chunks are not kept alive by stale closures.

`control_viewer` takes one `action`:

| action | what it does |
| ------ | ------------ |
| `context` | the whole viewer state, including its coordinate frame |
| `set_view` | look from top, front, back, left, right, or iso |
| `set_camera` | place the camera and target directly in model coordinates |
| `set_projection` | perspective or orthographic |
| `section` | cut axes at real heights, with an optional slice depth |
| `select` | select elements for the user: frames them and opens their properties |
| `isolate`, `hide`, `show_all` | narrow the view, take elements out of the way, or restore |
| `focus`, `unfocus` | isolate and frame elements directly, or return from that focused view |
| `measure_elements` | length, width, thickness, surface area, volume |
| `measure_clearance` | distance to the nearest element each way along each axis |
| `measure_points` | two points measure a distance, three an angle, four or more an area |
| `clear_measurements` | remove them all |
| `save_view`, `restore_view`, `list_views` | named viewpoints |

Measuring in the viewer uses the tessellated geometry on screen, so it answers
questions the schema does not: a wall's real thickness whatever angle it sits
at, the clear distance between two elements, the area inside an outline.
Surface area and volume come from the triangles themselves. Every result also
lands in the viewer's measurement list, where the user can see the number the
model just quoted.

`focus` is a direct per-object analysis view: it isolates the given elements
and zooms to them. It does not create or retain per-object tabs. `unfocus`
returns from that focused view, while Show all remains the single control that
clears every visibility filter.

Navigation is built for close work. The wheel zooms toward the cursor, the
orbit pivot follows the surface depth under every gesture without ever turning
the view, a double-click frames the element you clicked, and the zoom floor
and near plane scale with the model, so a bolt in a site-scale file is
reachable.

Measuring reads like a CAD tool. The pointer carries a live preview: the point
the click would land on wears its snap glyph (square corner, triangle
midpoint, circle face centre, diamond along an edge), and the rubber band from
the last click shows the running length beside the cursor. A band within a few
degrees of a model axis sticks to it and turns that axis's color, SketchUp
style; `Shift` hardens the stick, and `X`, `Y` or `Z` toggles a hard lock
directly. `Backspace` takes back the last click, `Escape` sheds pending points
and the lock before it sheds the mode. A committed measurement carries its
value as a floating tag in 3D, and hovering its row in the measurement list
lights it up; the row's x removes exactly that one. Markers keep a constant
on-screen size at any zoom.

Coordinates in and out are metres in the model's own axes (z up), the same
numbers `ifcopenshell` reports. The viewer draws in a different frame -- web-ifc
hands geometry back y-up, and slides the model to the origin so a georeferenced
file keeps its precision -- and converts on the way in and out.

## Live updates

```text
model edit -> console memory -> WebSocket -> viewer refresh
selection  <- shared session state <- browser tab
```

Edits, saves, reloads, modes, selections, and highlights update live. Multiple
browser tabs are supported. Selections remain distinct per IFC model; the most
recently active compatible tab answers screenshots and direct viewport commands.

## Security

Three.js, web-ifc, and the application are installed locally. The page makes no
non-localhost requests.

The initial token is placed in the URL fragment, which is not sent in HTTP
requests. The page removes it from the address bar and authenticates later API
and WebSocket calls. The server also rejects non-loopback Host and Origin
values.

The viewer can read model data and report selection. It cannot edit the model
or change the session mode.

## Limits

- `viewer.max_model_mb` defaults to 200 MB.
- Large models may require significant browser memory and parsing time.
- Section planes and slices are supported; an arbitrary oblique cut is not.
- Snapping retains a bounded set of the tessellation's real crease and boundary
  edges for each product. Very large geometry falls back to the exact visible
  surface rather than inventing a feature from a proxy box.
- Clearance is cast against element bounding boxes, and says so in `method`.
- Attached models use separate tabs and can be switched, not overlaid.

See [Troubleshooting](troubleshooting.md) for missing assets, authorization, or
model-size errors.
