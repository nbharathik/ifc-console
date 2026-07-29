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
- **View tools** (the stack icon, top left of the canvas): isolate or hide the
  selection, show all, zoom to the selection or the whole model, and jump to
  view presets (top, front, iso, ...).
- **Properties** (right): attributes, type, container chain, and property sets of
  the last clicked element, straight from the server (same source as
  `get_element`). Each section folds.
- **Arrange the layout.** Drag the divider next to a panel to resize it
  (double-click resets), or hide either panel with the two toggle buttons. Sizes
  persist per browser.
- **Viewer settings** (the gear, top right): toggle the ground grid (++g++) and
  the origin axes. The grid is infinite, fades with distance, and sits at the
  model's lowest level.

## What the LLM can do

- `highlight_elements`: color any set of elements, optionally isolating them and
  fitting the camera. Its way of pointing at things for you.
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
- Geometry is built per product; very large models will be draw-call-bound.
  Section boxes, measurements, and clipping planes are out of scope for v1.
- The viewer is deliberately unprivileged: it can read the model and report
  selection. There is no edit surface and no way to change the session mode from
  the browser.
