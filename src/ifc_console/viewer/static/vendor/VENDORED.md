# Vendored viewer assets

The viewer is intentionally build-free: plain ES modules served as-is, no
bundler, no CDN. These files were copied from npm on 2026-07-16.

| File | Package | Version | License |
| ---- | ------- | ------- | ------- |
| `three.module.min.js`, `three.core.min.js` | `three` | 0.180.0 | MIT (`LICENSE.three.txt`) |
| `OrbitControls.js` | `three` (examples/jsm/controls) | 0.180.0 | MIT |
| `web-ifc-api.js`, `web-ifc.wasm` | `web-ifc` | 0.0.71 | MPL-2.0 (`LICENSE.web-ifc.md`) |

Local modifications:

- `OrbitControls.js`: the single bare `from 'three'` import specifier was
  rewritten to `from './three.module.min.js'` so the file resolves without an
  import map (import maps would force a CSP exception for inline scripts).
  No other changes.
- All other files are byte-identical to their npm distribution.

To upgrade: `npm pack three@<ver> web-ifc@<ver>`, copy the files listed above,
re-apply the OrbitControls import rewrite, and update this table. Then open
the viewer against `tests/fixtures/generated/minimal_ifc4.ifc` and check
load, click-select, highlight, and screenshot.

Note on web-ifc (MPL-2.0): the files are distributed unmodified with their
license text; MPL file-level copyleft is satisfied. Do not edit
`web-ifc-api.js` or `web-ifc.wasm` in place.

`web-ifc-api.js` carries a multithreaded runtime (~200 KB) that never runs
here: it needs `crossOriginIsolated`, which the viewer does not set, and
`web-ifc-mt.wasm` is deliberately not vendored. Stripping it would break the
byte-identical guarantee the MPL note above depends on, so it stays.

`three.core.min.js` is not dead weight either: `three.module.min.js` re-exports
from it, so both files are required.
