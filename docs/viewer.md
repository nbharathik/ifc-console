# 3D viewer

The optional browser viewer shows geometry, properties, selections, and AI
highlights. It runs on localhost.

## Install and open

```bash
uv tool install "ifc-console[viewer]"
# or: pip install "ifc-console[viewer]"
```

Open a model, then run `/viewer`.

| command | use |
| ------- | --- |
| `/viewer` | enable the viewer and open the browser |
| `/viewer url` | print its URL |
| `/viewer off` | close tabs and remove viewer tools |
| `ifc-console --viewer` | enable it at startup |

The core console works without the viewer package. `ifc-console doctor` reports
whether its assets are installed.

stdio-only sessions have no HTTP server and therefore no viewer. Use the
interactive console or `--no-tui`.

## Layout

```text
+-- Agent --+-- IFC file tabs --+-- viewer tools / settings --+
| spatial tree  | 3D canvas                                      | properties   |
| and search    | persistent floating tool panels                | psets, qtos  |
+---------------+------------------------------------------------+--------------+
```

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

GlobalIds in Agent answers and tool results are live links into the model.
Clicking one opens the IFC surface, finds the attached model that contains it,
selects the element, and frames it. Drag-select a GlobalId in the transcript
and press ++i++ to open its IFC and isolate it; ++i++ also isolates the most
recently clicked GlobalId when focus is outside a text field.

Agent is the fixed first workspace tab and cannot be closed. Every IFC tab can
close, including the last one: the Agent then uses the full workspace without a
hidden 3D surface. Click the `>_ ifc-console` mark to return to that Agent-only
view, **Open active IFC** to restore the console's active model, or `+` to choose
any attached IFC file. Viewer settings sit at the right end of the viewer tool
rail, so settings apply to the viewer surface rather than the agent window.

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
  with `get_viewer_measurements`.
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

When several models are resident, Chrome-like tabs switch which IFC file is
displayed. Tabs can be closed and reopened from the plus button, and each tab
remembers its camera, selection, cuts, visibility, transparency, and
measurements for the session. Each parsed IFC revision stays in browser memory,
so returning to a tab skips the download and WebAssembly parse. The viewer
renders one model at a time; it does not create a federated overlay.

## AI tools

Six viewport tools plus the launcher are always present in the shared agent and
MCP catalog:

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
describe the tab currently on screen. The Agent panel shows one selection chip
per IFC and sends all of those model-scoped GlobalIds with the next message.
Pass a selected `model_id` to `control_viewer`, `highlight_elements`,
`apply_color_theme`, or `get_viewer_screenshot` when the target must be
explicit. Selection-based actions also infer this id when it is omitted, so a
hide, isolate, focus, fit, or measurement cannot be sent to a different active
IFC by mistake. Every command frame carries its model id, and a tab refuses the
command if it switched files while the call was in flight.

The viewport tools require a connected browser tab. An external MCP client does
not need the user to run `/viewer` first: `open_viewer` turns the surface on,
opens the tokenized page in the local browser, waits briefly for its WebSocket,
and returns `ready=true` when an IFC is loaded and the tab is connected. The
catalog stays stable through `/viewer` and `/viewer off`, which avoids stale
tool caches in Codex, Claude Code, and other MCP hosts. Standalone stdio
sessions get a clear error because they serve no web pages; use the shared
console bridge or HTTP transport for visual work.

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
