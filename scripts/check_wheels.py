"""Release guard: the split between the two distributions holds.

The base wheel must stay small and asset free; the viewer wheel must carry the
whole bundle. Run after `uv build` and `uv build --package ifc-console-viewer`.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

DIST = Path(__file__).parent.parent / "dist"
BASE_LIMIT_MB = 1.0
REQUIRED_ASSETS = (
    "index.html",
    "app.js",
    "worker.js",
    "chat.html",
    "chat.js",
    "chat.css",
    # the standalone page boots from this; without it /chat is a blank screen
    "chat-page.js",
    "vendor/web-ifc.wasm",
)


def _wheel(prefix: str) -> Path:
    matches = sorted(DIST.glob(f"{prefix}-*.whl"))
    if not matches:
        raise SystemExit(f"no {prefix} wheel in {DIST}; run uv build first")
    return matches[-1]


def main() -> int:
    base = _wheel("ifc_console")
    viewer = _wheel("ifc_console_viewer")

    base_names = zipfile.ZipFile(base).namelist()
    stray = [n for n in base_names if "/static/" in n or n.endswith(".wasm")]
    if stray:
        print(f"FAIL: viewer assets leaked into {base.name}: {stray[:5]}")
        return 1
    size_mb = base.stat().st_size / 1e6
    if size_mb > BASE_LIMIT_MB:
        print(f"FAIL: {base.name} is {size_mb:.2f} MB, over the {BASE_LIMIT_MB} MB budget")
        return 1

    viewer_names = zipfile.ZipFile(viewer).namelist()
    missing = [
        asset
        for asset in REQUIRED_ASSETS
        if not any(n.endswith(f"static/{asset}") for n in viewer_names)
    ]
    if missing:
        print(f"FAIL: {viewer.name} is missing {missing}")
        return 1

    print(f"ok: {base.name} {size_mb:.2f} MB (no assets), {viewer.name} carries the bundle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
