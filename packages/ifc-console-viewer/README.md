# ifc-console-viewer compatibility shim

The local 3D viewer, Three.js, and web-ifc assets are now bundled directly with
[`ifc-console`](https://pypi.org/project/ifc-console/). New installations only
need:

```console
pip install ifc-console
```

This package is a temporary compatibility shim for applications that installed
`ifc-console-viewer` or imported `ifc_console_viewer.static_dir()` directly.
It depends on a compatible `ifc-console` and forwards `static_dir()` to the
main package. It contains no browser assets and is not a standalone
application.

Existing `pip install "ifc-console[viewer]"` commands remain valid because the
main distribution retains an empty compatibility extra. This shim is intended
for one release cycle and may then be removed.
