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
+----------------+----------------------+----------------+
| spatial tree   | 3D canvas            | properties     |
| and search     | select, view, cut    | attributes,    |
|                | measure, highlight   | psets, qtos    |
+----------------+----------------------+----------------+
```

| action | control |
| ------ | ------- |
| frame model | ++f++ |
| select | click; ++ctrl++ + click for multiple |
| search | name, IFC class, storey, type, or selector |
| isolate or hide | view menu |
| measure a distance | ++m++, then two points |
| measure an angle | ++a++, then three points: end, corner, end |
| measure an area | ++r++, click an outline, ++enter++ to close it |
| element size | ++alt++ + click an element |
| snap on or off | ++s++ |
| parallel projection | ++p++ |
| section | enable X, Y, or Z plane; set a slice to keep only a slab |
| save a camera | named saved views |
| change panels | drag dividers or use panel buttons |

The viewer help button lists all mouse and keyboard controls.

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
- **Measurements:** distance, angle, area, element size, and clearance. All of
  them report metres in the model's own axes, and the model can read them back
  with `get_viewer_measurements`.
- **Snapping:** measured points land on corners, edge midpoints, face centres,
  and anywhere along an edge, chosen by which is nearest the cursor on screen.
  A marker names the feature before the click commits to it. Candidates come
  from every element near the cursor, so the corner where two walls meet
  offers both walls.
- **Saved views:** named viewpoints stored in the browser, carrying the
  camera, projection, selection, isolation, and section.
- **Color themes:** labeled groups with a colorblind-safe legend.
- **Grid and axes:** local visual aids that never modify the IFC model.

When several models are resident, a picker switches which one is displayed.
The viewer renders one model at a time; it does not create a federated overlay.

## AI tools

Six tools exist only while the viewer is enabled:

| tool | use |
| ---- | --- |
| `get_viewer_selection` | read the user's selected elements |
| `get_viewer_measurements` | read everything measured, by anyone |
| `highlight_elements` | color, isolate, and frame elements |
| `apply_color_theme` | show labeled groups and a legend |
| `get_viewer_screenshot` | capture a preset or current view |
| `control_viewer` | operate the viewport and read the result back |

They require a connected browser tab. Clients may need to refresh their tool
list after `/viewer` or `/viewer off`. An external MCP client does not need
the user to run `/viewer` first: `open_viewer` is always registered, turns the
surface on, and opens the tokenized page in the local browser (stdio sessions
get a clear error instead, since they serve no web pages).

`control_viewer` takes one `action`:

| action | what it does |
| ------ | ------------ |
| `context` | the whole viewer state, including its coordinate frame |
| `set_view` | look from top, front, back, left, right, or iso |
| `set_projection` | perspective or orthographic |
| `section` | cut axes at real heights, with an optional slice depth |
| `select` | select elements for the user: frames them and opens their properties |
| `isolate`, `hide`, `show_all` | narrow the view, take elements out of the way, or restore |
| `focus`, `unfocus` | open elements alone in a named tab under the top bar, or close tabs |
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

`focus` is the per-object analysis view: it isolates the given elements, zooms
to them, and adds a named tab to a strip under the top bar. Each analyzed
object gets its own tab; switching tabs switches the isolation, All restores
the whole model, and the x on a tab closes it. Tabs are GlobalId-keyed, so
they survive model refreshes. Agents use it to keep what they are measuring
and what the user is seeing in step.

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
tabs are supported; the latest selection and most recently active screenshot
tab win.

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
- Snapping finds the features of an element's own oriented box: corners, edge
  midpoints, face centres, and points along an edge. For a curved or diagonal
  member the box is not the member, and the measurement falls back to the
  surface point, which the readout says. Mesh-exact snapping would mean
  keeping the source geometry after batching, which is a memory decision worth
  taking on its own terms.
- Clearance is cast against element bounding boxes, and says so in `method`.
- Attached models can be switched, not overlaid.

See [Troubleshooting](troubleshooting.md) for missing assets, authorization, or
model-size errors.
