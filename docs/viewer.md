# 3D viewer

The viewer gives sessions the three things a terminal cannot: **see** the model,
**point** at elements (both directions), and **capture** screenshots the LLM can
look at.

The viewer ships with every install: three.js and the web-ifc parser are
bundled in the package, and `ifc-console doctor` verifies them on its
`viewer assets` line. It stays off until you start it.

## Starting it

- In the console: `/viewer` (enables it if needed, opens your default browser,
  copies the URL).
- At launch: `ifc-console --viewer ...` or `viewer.enabled_default true`.
- Headless: `ifc-console --no-tui --viewer` prints the viewer URL.

The URL carries the session token in its fragment
(`http://127.0.0.1:8383/viewer#t=<token>`). The fragment never leaves the
browser, so the token stays out of server logs and referrers; the page reads
it, scrubs it from the address bar, and then authenticates every API call and
the live WebSocket with it. Without the token the data APIs answer 401. The
server additionally rejects any request whose Host or Origin is not loopback,
so a malicious website cannot reach the session even from your own machine.
Everything is served from your machine: three.js and the web-ifc WASM parser
ship inside the package, and the page makes zero non-localhost requests
(enforced by its Content-Security-Policy).

stdio sessions have no HTTP server, so no viewer. Run the console or `--no-tui`
if you want it.

## Tool registration follows the viewer

The four viewer tools (`get_viewer_selection`, `highlight_elements`,
`apply_color_theme`, `get_viewer_screenshot`) are an optional category. They
appear in the MCP tool list only while the viewer is enabled. `/viewer` adds
them live, `/viewer off` removes them, and sessions that never touch the
viewer keep the lean core set. Clients that connected before the toggle see
the change on their next tool refresh or reconnect.

The color theme is the review headline: the LLM computes any grouping (by
storey, type, material, a property value, validation pass/fail), the viewer
paints it with colorblind-safe colors, and a legend with labels and counts
appears in the corner of the canvas. The console theme (`/theme dark|light`)
also restyles the viewer live, 3D canvas included.

## What you can do

- **Orbit / pan / zoom** with the mouse. Press ++f++ to frame the whole model
  (it also auto-fits on load). The `?` button lists every mouse and keyboard
  control.
- **Select.** Click an element (ctrl+click multi-select). The selection appears
  in the footer, the properties panel loads attributes and psets, and the server
  remembers it: the LLM reads it with `get_viewer_selection`. "Delete this wall"
  becomes unambiguous.
- **Spatial tree** (left): project > site > building > storeys, with checkboxes
  toggling whole branches. Clicking a node selects its elements without moving
  the camera. Long names never truncate: the panel scrolls and shows a hover
  tooltip.
- **Search** (the box above the tree): type a name, or an IFC class such as
  `IfcDoor`. Two or more characters replace the tree with a result list showing
  name, class, storey, and type. Click a hit to select it, double-click to zoom
  to it, or use **Select** and **Isolate** to act on the whole result set.
  ++esc++ or the × clears it and brings the tree back. The matching happens on
  the server against the live model, so results include unsaved edits. Anything
  with an `=` in it, or a bare `IfcSomething`, goes through the full IfcOpenShell
  selector grammar, so `Pset_WallCommon.FireRating=F30` works too.
- **View tools** (the stack icon, top left of the canvas): isolate or hide the
  selection, show all, zoom to the selection or the whole model, and jump to
  view presets (top, front, iso, ...).
- **Saved views.** Park the camera somewhere useful, name it, and press Save.
  The view reappears in the same panel and restores the exact camera position
  and target on one click. Saving under an existing name replaces it; the ×
  deletes it. Views are stored per browser (up to 12) and survive reloads and
  model edits, so "the entrance from the north" stays one click away.
- **Section planes.** Cut the model on X, Y, or Z. Tick an axis and a slider
  appears; drag it to move the cut, or use the flip button to keep the other
  side. Axes combine, so X plus Z gives a corner cut and Z alone gives the
  storey slice you usually want. "Clear sections" turns every cut off, and the
  cuts persist per browser.
- **Measure.** Press ++m++ or pick "Measure distance", then click two points on
  the model. Each measurement shows the straight-line distance plus the X, Y and
  Z components in the model's own axes, and stays on screen until you clear it.
  Sectioned-away surfaces cannot be measured, so a cut behaves like the real
  edge. ++esc++ leaves the tool.
- **Properties** (right): attributes, type, container chain, material (including
  layer sets with thicknesses), property sets, quantities, and the element's
  parts, all for the last clicked element and straight from the server (same
  source as `get_element`). Each section folds; a part with geometry selects when
  clicked.
- **Arrange the layout.** Drag the divider next to a panel to resize it
  (double-click resets), or hide either panel with the two toggle buttons. Sizes
  persist per browser.
- **Viewer settings** (the gear, top right): toggle the ground grid (++g++) and
  the origin axes. The grid is infinite, fades with distance, and sits at the
  model's lowest level.
- **Switch model.** When the console holds more than one model (`/workspace`,
  `/attach`), a picker appears next to the model name listing every resident
  model, the active one first and marked. Choosing another shows it instead;
  the viewer follows the active model again as soon as you pick it back. One
  model is drawn at a time: this is a switcher, not an overlay.

## What the LLM can do

- `get_viewer_selection`: read the elements you have click-selected, including
  which resident model owns them.
- `highlight_elements`: color any set of elements, optionally isolating them and
  fitting the camera. Its way of pointing at things for you.
- `apply_color_theme`: paint labeled element groups and show a matching legend.
- `get_viewer_screenshot`: set a view preset (top, front, iso, ...), fit, and
  capture the canvas. The image returns inline in the conversation, so the model
  can verify visual claims it just made.

## Live sync

The viewer holds a WebSocket to the server. Model edits (edit-mode
`execute_ifc_code` runs, saves, reloads) push a refresh. The tab refetches the
**in-memory** model, so you watch unsaved changes appear live, with your
selection and highlights preserved. The mode badge and unsaved-changes chip stay
current. Disconnects retry with backoff.

Multiple tabs are fine. Selection is last-writer-wins, and screenshots go to the
most recently active tab.

## Limits

- `viewer.max_model_mb` (default 200) guards the model download. Beyond it the
  viewer shows "model too large" (raise the setting if you mean it).
- Parsing and geometry generation run in a worker. Repeated geometry is
  deduplicated and instanced, while hide and highlight state stays on the GPU.
  Section planes and measurement are built in; section boxes (a full 3D crop)
  are out of scope for v1.
- One model is rendered at a time. Attached models are shown by switching to
  them, not overlaid; federated overlay is a later change.
- The viewer is deliberately unprivileged: it can read the model and report
  selection. There is no edit surface and no way to change the session mode from
  the browser.
