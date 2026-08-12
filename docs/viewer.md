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
| measure | ++m++, then two points |
| section | enable X, Y, or Z plane |
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
- **Sections:** combine axis planes for storey slices or corner cuts.
- **Measurements:** total distance plus X, Y, and Z components.
- **Saved views:** named camera positions stored in the browser.
- **Color themes:** labeled groups with a colorblind-safe legend.
- **Grid and axes:** local visual aids that never modify the IFC model.

When several models are resident, a picker switches which one is displayed.
The viewer renders one model at a time; it does not create a federated overlay.

## AI tools

Four tools exist only while the viewer is enabled:

| tool | use |
| ---- | --- |
| `get_viewer_selection` | read the user's selected elements |
| `highlight_elements` | color, isolate, and frame elements |
| `apply_color_theme` | show labeled groups and a legend |
| `get_viewer_screenshot` | capture a preset or current view |

They require a connected browser tab. Clients may need to refresh their tool
list after `/viewer` or `/viewer off`.

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
- Section planes are supported; a full 3D section box is not.
- Attached models can be switched, not overlaid.

See [Troubleshooting](troubleshooting.md) for missing assets, authorization, or
model-size errors.
