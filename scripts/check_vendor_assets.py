"""Verify the exact viewer assets reviewed for the release."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

VENDOR = (
    Path(__file__).parent.parent
    / "packages"
    / "ifc-console-viewer"
    / "src"
    / "ifc_console_viewer"
    / "static"
    / "vendor"
)

EXPECTED_SHA256 = {
    "OrbitControls.js": "6c860c6b342200f8aef65493319c12bfb2d652107355b1d25eb2154371128391",
    "three.core.min.js": "61ba0df005b05991361d040d8ff670e1aadfd0ce7aeebd1fdb0725957a8957de",
    "three.module.min.js": "e2b5ee6bccd38fd6d8a2428546b83c5f2426d84b152ef82be8055556e3b40eb6",
    "web-ifc-api.js": "1f07c3cfcad8309ac92556aa8e6811f7f6c331e9cbd1dfe5037dad5a36c0fe03",
    "web-ifc.wasm": "f466be51ae179fdcbe55e36116bd1dc233f73e64b5a446af761f711c2ded4c3d",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    failures: list[str] = []
    for name, expected in EXPECTED_SHA256.items():
        path = VENDOR / name
        if not path.is_file():
            failures.append(f"missing {name}")
            continue
        actual = _sha256(path)
        if actual != expected:
            failures.append(f"{name}: expected {expected}, got {actual}")
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print(f"ok: verified {len(EXPECTED_SHA256)} vendored viewer assets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
