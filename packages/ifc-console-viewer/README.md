# ifc-console viewer

Static browser assets for the local 3D viewer and chat panel provided by
[`ifc-console`](https://pypi.org/project/ifc-console/).

Install both the core application and this matching asset bundle through the
public extra:

```bash
pip install "ifc-console[viewer]"
```

This asset-only package contains the browser application, Three.js, and the
web-ifc JavaScript/WASM parser. MCP, SDK, agents, chat, and workflows remain in
`ifc-console`. This wheel makes no network requests by itself and is not a
standalone application.

The package is Apache-2.0. Its `static/vendor` directory carries the upstream
license and provenance files for the vendored third-party components.
