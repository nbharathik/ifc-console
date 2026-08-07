# ifc-console-viewer

Static assets for the [ifc-console](https://github.com/nbharathik/ifc-console)
3D web viewer: the viewer SPA, a vendored three.js, and the web-ifc WASM
parser.

You do not install this directly. It arrives with the viewer extra:

```bash
uv tool install "ifc-console[viewer]"
# or: pip install "ifc-console[viewer]"
```

Then type `/viewer` in the console.

Keeping the bundle out of the base package keeps `pip install ifc-console`
small for people who only use the MCP tools, the CLI, or the SDK.

## Licenses

Apache-2.0 for the packaging. The bundled assets keep their own licenses:
three.js (MIT) and web-ifc (MPL-2.0, unmodified). See `VENDORED.md` and the
license files inside `static/vendor/`.
