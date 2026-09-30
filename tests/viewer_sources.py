"""The viewer's application script: app.js and the modules it was split into."""

from __future__ import annotations

import re

from ifc_console.viewer import assets

# Modules with their own fixtures or Node tests; they are read separately.
_SEPARATE = {
    "delta.js",
    "measure_math.js",
    "parser.js",
    "viewer_component.js",
    "worker.js",
}


def application_modules() -> list[str]:
    """app.js first, then every local module it imports, breadth first."""
    static = assets.require_static_dir()
    found = ["app.js"]
    queue = ["app.js"]
    while queue:
        text = (static / queue.pop(0)).read_text(encoding="utf-8")
        for name in re.findall(r'from "\./(\w+\.js)"', text):
            if name not in found and name not in _SEPARATE:
                found.append(name)
                queue.append(name)
    return found


def application_script() -> str:
    static = assets.require_static_dir()
    return "\n".join((static / name).read_text(encoding="utf-8") for name in application_modules())
